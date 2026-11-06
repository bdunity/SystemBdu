#!/usr/bin/env python3
"""Importa un export de Monday a Baserow y valida contra manifest.json.

Uso (CMD, desde la raiz del repo):
    python scripts\\import_baserow.py 18393961251
    python scripts\\import_baserow.py 18393961251 --max-items 20
    python scripts\\import_baserow.py 18393961251 --validate-only

Se puede volver a correr: las filas con un monday_id ya existente no se duplican.
Codigo de salida: 0 sin diferencias, 2 con diferencias, 1 error fatal.
"""
import time
from concurrent.futures import ThreadPoolExecutor

from import_common import (
    COMMENTS_TABLE,
    FILES,
    ITEM_LINK,
    ITEMS_TABLE,
    MONDAY_ID,
    ApiError,
    FatalError,
    HttpClient,
    build_parser,
    finish,
    load_model,
    read_env,
    run_main,
    validate_rows,
)

WORKSPACE = "Monday PoC"
DATABASE = "CPP"
BATCH = 200
COLORS = ["light-blue", "light-green", "light-orange", "light-red", "light-gray", "blue", "green", "orange", "red", "dark-gray"]


class Baserow:
    def __init__(self, url, email, password):
        self.http = HttpClient(url)
        self.http.on_unauthorized = self.login
        self._credentials = (email, password)
        self._uploaded = {}

    def login(self):
        email, password = self._credentials
        self.http.headers.pop("Authorization", None)
        data = self.http.request("POST", "/api/user/token-auth/", {"email": email, "password": password}, retries=2, reauth=False)
        self.http.headers["Authorization"] = f"JWT {data['access_token']}"

    def connect(self, create_account):
        try:
            self.login()
        except ApiError as error:
            if not create_account or error.status not in (400, 401):
                raise FatalError("No se pudo iniciar sesion en Baserow; revisa BASEROW_EMAIL y BASEROW_PASSWORD.") from None
            email, password = self._credentials
            self.http.request("POST", "/api/user/", {"name": "Admin PoC", "email": email, "password": password})
            self.login()

    def workspace(self):
        for workspace in self.http.request("GET", "/api/workspaces/"):
            if workspace["name"] == WORKSPACE:
                return workspace["id"]
        return self.http.request("POST", "/api/workspaces/", {"name": WORKSPACE})["id"]

    def database(self, workspace_id):
        for application in self.http.request("GET", f"/api/applications/workspace/{workspace_id}/"):
            if application["name"] == DATABASE and application["type"] == "database":
                return application["id"]
        path = f"/api/applications/workspace/{workspace_id}/"
        return self.http.request("POST", path, {"type": "database", "name": DATABASE})["id"]

    def table(self, database_id, name, primary, second=None):
        path = f"/api/database/tables/database/{database_id}/"
        for table in self.http.request("GET", path):
            if table["name"] == name:
                return table["id"]
        header = [primary] + ([second] if second else [])
        return self.http.request("POST", path, {"name": name, "data": [header], "first_row_header": True})["id"]

    def ensure_fields(self, table_id, specs, link_table_id=None):
        path = f"/api/database/fields/table/{table_id}/"
        existing = {field["name"] for field in self.http.request("GET", path)}
        for spec in specs:
            if spec.name in existing:
                continue
            self.http.request("POST", path, {"name": spec.name, **self._field_body(spec, link_table_id)})

    @staticmethod
    def _field_body(spec, link_table_id):
        if spec.kind == "text":
            return {"type": "text"}
        if spec.kind == "long_text":
            return {"type": "long_text"}
        if spec.kind == "number":
            return {"type": "number", "number_decimal_places": spec.decimals, "number_negative": True}
        if spec.kind == "date":
            return {"type": "date", "date_format": "ISO", "date_include_time": False}
        if spec.kind == "datetime":
            return {
                "type": "date",
                "date_format": "ISO",
                "date_include_time": True,
                "date_time_format": "24",
                "date_show_tzinfo": True,
            }
        if spec.kind == "checkbox":
            return {"type": "boolean"}
        if spec.kind == "single_select":
            options = [{"value": value, "color": COLORS[i % len(COLORS)]} for i, value in enumerate(spec.options)]
            return {"type": "single_select", "select_options": options}
        if spec.kind == FILES:
            return {"type": "file"}
        if spec.kind == "link":
            return {"type": "link_row", "link_row_table_id": link_table_id, "has_related_field": True}
        raise FatalError(f"Tipo de campo no soportado: {spec.kind}")

    def upload(self, ref, board_dir):
        if ref.sha256 not in self._uploaded:
            data = self.http.upload("/api/user-files/upload-file/", board_dir / ref.path, ref.name)
            self._uploaded[ref.sha256] = data["name"]
        return {"name": self._uploaded[ref.sha256], "visible_name": ref.name}

    def rows(self, table_id):
        rows = []
        page = 1
        while True:
            data = self.http.request(
                "GET",
                f"/api/database/rows/table/{table_id}/",
                query={"user_field_names": "true", "size": BATCH, "page": page},
            )
            rows.extend(data["results"])
            if not data["next"]:
                return rows
            page += 1

    def insert(self, table_id, payloads):
        for start in range(0, len(payloads), BATCH):
            self.http.request(
                "POST",
                f"/api/database/rows/table/{table_id}/batch/",
                {"items": payloads[start : start + BATCH]},
                query={"user_field_names": "true", "send_webhook_events": "false"},
            )


def import_rows(baserow, table_id, specs, rows, board_dir, workers, link_ids=None):
    existing = {row[MONDAY_ID] for row in baserow.rows(table_id)}
    pending = [row for row in rows if row.monday_id not in existing]
    print(f"  {len(existing)} filas ya existen, {len(pending)} por crear")

    def payload(row):
        body = {}
        for spec in specs:
            if spec.kind == FILES:
                body[spec.name] = [baserow.upload(ref, board_dir) for ref in row.files.get(spec.name) or []]
            elif spec.kind == "link":
                body[spec.name] = [link_ids[row.values[spec.name]]]
            elif row.values.get(spec.name) is not None:
                body[spec.name] = row.values[spec.name]
        return body

    # Sube primero todos los archivos: una fila nunca queda creada a medias.
    done = 0
    for start in range(0, len(pending), BATCH):
        chunk = pending[start : start + BATCH]
        with ThreadPoolExecutor(max_workers=workers) as pool:
            payloads = list(pool.map(payload, chunk))
        baserow.insert(table_id, payloads)
        done += len(chunk)
        print(f"  filas creadas {done}/{len(pending)}", flush=True)


def read_back(baserow, table_id, specs, id_to_monday=None):
    actual = {}
    for row in baserow.rows(table_id):
        values = {}
        files = {}
        for spec in specs:
            raw = row.get(spec.name)
            if spec.kind == FILES:
                files[spec.name] = [
                    {"name": entry["visible_name"], "url": entry["url"], "size": entry["size"]} for entry in raw or []
                ]
            elif spec.kind == "single_select":
                values[spec.name] = raw["value"] if raw else None
            elif spec.kind == "link":
                values[spec.name] = id_to_monday.get(raw[0]["id"]) if raw else None
            else:
                values[spec.name] = raw
        actual[row[MONDAY_ID]] = {"values": values, "files": files}
    return actual


def main():
    args = build_parser("Importa un export de Monday a Baserow.").parse_args()
    env = read_env("BASEROW_PUBLIC_URL", "BASEROW_EMAIL", "BASEROW_PASSWORD")
    model = load_model(args.board_id, args.export_dir, args.max_items)
    started = time.time()

    baserow = Baserow(env["BASEROW_PUBLIC_URL"], env["BASEROW_EMAIL"], env["BASEROW_PASSWORD"])
    baserow.connect(args.create_account and not args.validate_only)
    database_id = baserow.database(baserow.workspace())
    items_table = baserow.table(database_id, ITEMS_TABLE, "Name", MONDAY_ID)
    comments_table = baserow.table(database_id, COMMENTS_TABLE, MONDAY_ID)
    baserow.ensure_fields(items_table, model.item_fields)
    baserow.ensure_fields(comments_table, model.comment_fields, link_table_id=items_table)

    if not args.validate_only:
        print(f"Items ({ITEMS_TABLE}):")
        import_rows(baserow, items_table, model.item_fields, model.items, model.board_dir, args.workers)
    item_rows = baserow.rows(items_table)
    monday_to_id = {row[MONDAY_ID]: row["id"] for row in item_rows}
    id_to_monday = {row["id"]: row[MONDAY_ID] for row in item_rows}
    if not args.validate_only:
        print(f"Comentarios ({COMMENTS_TABLE}):")
        import_rows(baserow, comments_table, model.comment_fields, model.comments, model.board_dir, args.workers, monday_to_id)

    print("Validando:")
    items = validate_rows(
        "items",
        model.item_fields,
        model.items,
        read_back(baserow, items_table, model.item_fields),
        baserow.http,
        args.workers,
    )
    comments = validate_rows(
        "comentarios",
        model.comment_fields,
        model.comments,
        read_back(baserow, comments_table, model.comment_fields, id_to_monday),
        baserow.http,
        args.workers,
    )
    return finish("baserow", model, items, comments, time.time() - started)


if __name__ == "__main__":
    run_main(main)
