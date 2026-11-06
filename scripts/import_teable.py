#!/usr/bin/env python3
"""Importa un export de Monday a Teable y valida contra manifest.json.

Uso (CMD, desde la raiz del repo):
    python scripts\\import_teable.py 18393961251
    python scripts\\import_teable.py 18393961251 --max-items 20
    python scripts\\import_teable.py 18393961251 --validate-only

Se puede volver a correr: no duplica registros y solo sube los archivos que falten.
Codigo de salida: 0 sin diferencias, 2 con diferencias, 1 error fatal.
"""
import time
from collections import Counter
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

SPACE = "Monday PoC"
BASE = "CPP"
BATCH = 200
PAGE = 1000
COLORS = ["blueLight1", "greenLight1", "orangeLight1", "redLight1", "grayLight1", "blue", "green", "orange", "red", "gray"]


class Teable:
    def __init__(self, url, email, password):
        self.http = HttpClient(url)
        self.http.on_unauthorized = self.login
        self._credentials = (email, password)

    def login(self):
        email, password = self._credentials
        self.http.request("POST", "/api/auth/signin", {"email": email, "password": password}, retries=2, reauth=False)

    def connect(self, create_account):
        try:
            self.login()
        except ApiError as error:
            if not create_account or error.status not in (400, 401, 404):
                raise FatalError("No se pudo iniciar sesion en Teable; revisa TEABLE_EMAIL y TEABLE_PASSWORD.") from None
            email, password = self._credentials
            self.http.request("POST", "/api/auth/signup", {"email": email, "password": password}, reauth=False)
            self.login()

    def space(self):
        for space in self.http.request("GET", "/api/space"):
            if space["name"] == SPACE:
                return space["id"]
        return self.http.request("POST", "/api/space", {"name": SPACE})["id"]

    def base(self, space_id):
        for base in self.http.request("GET", f"/api/space/{space_id}/base"):
            if base["name"] == BASE:
                return base["id"]
        return self.http.request("POST", "/api/base", {"spaceId": space_id, "name": BASE})["id"]

    def table(self, base_id, name, primary):
        for table in self.http.request("GET", f"/api/base/{base_id}/table"):
            if table["name"] == name:
                return table["id"]
        body = {"name": name, "fields": [{"name": primary, "type": "singleLineText"}], "records": []}
        return self.http.request("POST", f"/api/base/{base_id}/table/", body)["id"]

    def ensure_fields(self, table_id, specs, link_table_id=None):
        path = f"/api/table/{table_id}/field"
        existing = {field["name"]: field["id"] for field in self.http.request("GET", path)}
        for spec in specs:
            if spec.name not in existing:
                created = self.http.request("POST", path, {"name": spec.name, **self._field_body(spec, link_table_id)})
                existing[spec.name] = created["id"]
        return existing

    @staticmethod
    def _field_body(spec, link_table_id):
        if spec.kind == "text":
            return {"type": "singleLineText"}
        if spec.kind == "long_text":
            return {"type": "longText"}
        if spec.kind == "number":
            return {"type": "number", "options": {"formatting": {"type": "decimal", "precision": spec.decimals}}}
        if spec.kind == "date":
            formatting = {"date": "YYYY-MM-DD", "time": "None", "timeZone": "UTC"}
            return {"type": "date", "options": {"formatting": formatting}}
        if spec.kind == "datetime":
            formatting = {"date": "YYYY-MM-DD", "time": "HH:mm", "timeZone": "UTC"}
            return {"type": "date", "options": {"formatting": formatting}}
        if spec.kind == "checkbox":
            return {"type": "checkbox"}
        if spec.kind == "single_select":
            choices = [{"name": value, "color": COLORS[i % len(COLORS)]} for i, value in enumerate(spec.options)]
            return {"type": "singleSelect", "options": {"choices": choices}}
        if spec.kind == FILES:
            return {"type": "attachment"}
        if spec.kind == "link":
            return {"type": "link", "options": {"relationship": "manyOne", "foreignTableId": link_table_id}}
        raise FatalError(f"Tipo de campo no soportado: {spec.kind}")

    def records(self, table_id):
        records = []
        while True:
            data = self.http.request(
                "GET",
                f"/api/table/{table_id}/record",
                query={"fieldKeyType": "name", "take": PAGE, "skip": len(records)},
            )
            records.extend(data["records"])
            if len(data["records"]) < PAGE:
                return records

    def insert(self, table_id, payloads):
        for start in range(0, len(payloads), BATCH):
            self.http.request(
                "POST",
                f"/api/table/{table_id}/record",
                {"fieldKeyType": "name", "records": [{"fields": body} for body in payloads[start : start + BATCH]]},
            )


def import_rows(teable, table_id, specs, rows, link_ids=None):
    """Crea los registros que faltan, sin archivos."""
    existing = {record["fields"].get(MONDAY_ID) for record in teable.records(table_id)}
    pending = [row for row in rows if row.monday_id not in existing]
    print(f"  {len(existing)} registros ya existen, {len(pending)} por crear")
    payloads = []
    for row in pending:
        body = {}
        for spec in specs:
            value = row.values.get(spec.name)
            if spec.kind == FILES or value is None or value is False:
                continue
            if spec.kind == "link":
                body[spec.name] = {"id": link_ids[value]}
            elif spec.kind == "number":
                body[spec.name] = float(value)
            else:
                body[spec.name] = value
        payloads.append(body)
    teable.insert(table_id, payloads)
    print(f"  registros creados {len(payloads)}", flush=True)


def upload_files(teable, table_id, field_ids, specs, rows, board_dir, workers):
    """Sube por registro solo lo que falta; comparar por tamano permite reanudar."""
    file_specs = [spec for spec in specs if spec.kind == FILES]
    records = {record["fields"].get(MONDAY_ID): record for record in teable.records(table_id)}

    def upload_row(row):
        # Dentro de un registro va en serie: subir en paralelo al mismo campo podria pisarse.
        record = records[row.monday_id]
        count = 0
        for spec in file_specs:
            present = Counter(entry["size"] for entry in record["fields"].get(spec.name) or [])
            for ref in row.files.get(spec.name) or []:
                if present[ref.size] > 0:
                    present[ref.size] -= 1
                    continue
                path = f"/api/table/{table_id}/record/{record['id']}/{field_ids[spec.name]}/uploadAttachment"
                teable.http.upload(path, board_dir / ref.path, ref.name)
                count += 1
        return count

    uploaded = 0
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for index, count in enumerate(pool.map(upload_row, rows), 1):
            uploaded += count
            if index % 100 == 0 or index == len(rows):
                print(f"  registros {index}/{len(rows)} | archivos subidos {uploaded}", flush=True)


def read_back(teable, table_id, specs, id_to_monday=None):
    actual = {}
    for record in teable.records(table_id):
        fields = record["fields"]
        values = {}
        files = {}
        for spec in specs:
            raw = fields.get(spec.name)
            if spec.kind == FILES:
                files[spec.name] = [
                    {"name": entry["name"], "url": entry["presignedUrl"], "size": entry["size"]} for entry in raw or []
                ]
            elif spec.kind == "link":
                values[spec.name] = id_to_monday.get(raw["id"]) if raw else None
            else:
                values[spec.name] = raw
        actual[fields.get(MONDAY_ID)] = {"values": values, "files": files}
    return actual


def main():
    args = build_parser("Importa un export de Monday a Teable.").parse_args()
    env = read_env("TEABLE_PUBLIC_ORIGIN", "TEABLE_EMAIL", "TEABLE_PASSWORD")
    model = load_model(args.board_id, args.export_dir, args.max_items)
    started = time.time()

    teable = Teable(env["TEABLE_PUBLIC_ORIGIN"], env["TEABLE_EMAIL"], env["TEABLE_PASSWORD"])
    teable.connect(args.create_account and not args.validate_only)
    base_id = teable.base(teable.space())
    items_table = teable.table(base_id, ITEMS_TABLE, "Name")
    comments_table = teable.table(base_id, COMMENTS_TABLE, MONDAY_ID)
    item_fields = teable.ensure_fields(items_table, model.item_fields)
    comment_fields = teable.ensure_fields(comments_table, model.comment_fields, link_table_id=items_table)

    if not args.validate_only:
        print(f"Items ({ITEMS_TABLE}):")
        import_rows(teable, items_table, model.item_fields, model.items)
        upload_files(teable, items_table, item_fields, model.item_fields, model.items, model.board_dir, args.workers)
    records = teable.records(items_table)
    monday_to_id = {record["fields"].get(MONDAY_ID): record["id"] for record in records}
    id_to_monday = {record["id"]: record["fields"].get(MONDAY_ID) for record in records}
    if not args.validate_only:
        print(f"Comentarios ({COMMENTS_TABLE}):")
        import_rows(teable, comments_table, model.comment_fields, model.comments, monday_to_id)
        upload_files(teable, comments_table, comment_fields, model.comment_fields, model.comments, model.board_dir, args.workers)

    print("Validando:")
    items = validate_rows(
        "items", model.item_fields, model.items, read_back(teable, items_table, model.item_fields), teable.http, args.workers
    )
    comments = validate_rows(
        "comentarios",
        model.comment_fields,
        model.comments,
        read_back(teable, comments_table, model.comment_fields, id_to_monday),
        teable.http,
        args.workers,
    )
    return finish("teable", model, items, comments, time.time() - started)


if __name__ == "__main__":
    run_main(main)
