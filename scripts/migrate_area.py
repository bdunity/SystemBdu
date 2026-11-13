#!/usr/bin/env python3
"""Migra un area completa de Monday (varios tableros) a un espacio de trabajo de Baserow.

Lee los exports de export_monday_board.py y la lista export/migracion_it_comercial.json.
Por area crea: un espacio de trabajo, una base de datos y, por tablero, una tabla, su tabla
de subelementos y su tabla de comentarios (si existen). Las relaciones entre tableros se
reconstruyen como vinculos de Baserow. Al final valida valores, archivos (SHA-256) y vinculos.

Uso (CMD, desde la raiz del repo):
    python scripts\\migrate_area.py COMERCIAL
    python scripts\\migrate_area.py IT --validate-only

Se puede volver a correr: no duplica filas (usa monday_id). La consola solo muestra conteos.
Codigo de salida: 0 sin diferencias, 2 con diferencias, 1 error fatal.
"""
import argparse
import json
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from pathlib import Path

from import_baserow import BATCH, COLORS, Baserow
from import_common import (
    FILES,
    MONDAY_ID,
    FatalError,
    FieldSpec,
    FileRef,
    Row,
    read_env,
    run_main,
    to_utc_iso,
    validate_rows,
)

AREA_NAMES = {"COMERCIAL": "Comercial", "IT": "IT"}
# Tipos que no se copian: el nombre va aparte, los subelementos van a su tabla, el ID ya esta
# en monday_id, y botones/integraciones/progreso no tienen un valor propio en la API.
SKIP_TYPES = {"name", "subtasks", "item_id", "button", "integration", "progress"}
RESERVED_NAMES = {"id", "order"}
LINK = "link"
MULTI = "multiple_select"
PLAN_FILE = Path("export/migracion_it_comercial.json")


def parse(cv):
    raw = cv.get("value")
    if raw in (None, "", "null"):
        return None
    try:
        return json.loads(raw)
    except ValueError:
        return raw


def labels_of(column):
    """Opciones de status/dropdown como {id: nombre}."""
    settings = json.loads(column.get("settings_str") or "{}")
    labels = settings.get("labels") or {}
    if isinstance(labels, dict):
        return {str(k): v for k, v in labels.items() if v}
    return {str(label["id"]): label["name"] for label in labels if label.get("name")}


class TableModel:
    """Una tabla destino: campos, filas y vinculos pendientes."""

    def __init__(self, name, primary, export_dir):
        self.name = name
        self.primary = primary
        self.export_dir = export_dir  # de aqui se suben los archivos
        self.fields = [FieldSpec(primary, "text")]  # FieldSpec; los link llevan .target
        self.rows = []
        self.notes = Counter()

    def add_field(self, spec):
        taken = {f.name.casefold() for f in self.fields}
        base = spec.name
        if base.casefold() in RESERVED_NAMES:
            spec.name = base = f"{base} (Monday)"
        n = 2
        while spec.name.casefold() in taken:
            spec.name = f"{base} ({n})"
            n += 1
        self.fields.append(spec)
        return spec


def link_spec(name, target):
    spec = FieldSpec(name, LINK)
    spec.target = target
    return spec


def build_columns(table, columns, items, users, board_tables):
    """Decide el campo destino de cada columna y devuelve convertidores por columna."""
    converters = []
    for column in columns:
        ctype = column["type"]
        if ctype in SKIP_TYPES or column.get("archived"):
            if ctype not in ("name", "subtasks", "item_id"):
                table.notes[f"omitida:{ctype}"] += 1
            continue
        title = column["title"]
        values = [cv for item in items for cv in item["column_values"] if cv["id"] == column["id"]]

        if ctype in ("text", "email", "phone", "country", "link", "location", "tags", "hour", "world_clock", "week", "color_picker", "rating", "vote", "dependency", "auto_number", "creation_log_text"):
            spec = table.add_field(FieldSpec(title, "text"))

            def conv(cv, item, spec=spec, ctype=ctype):
                value = parse(cv)
                if ctype == "email" and isinstance(value, dict):
                    return {spec.name: value.get("email") or cv.get("text") or None}
                if ctype == "phone" and isinstance(value, dict):
                    return {spec.name: value.get("phone") or cv.get("text") or None}
                if ctype == "link" and isinstance(value, dict):
                    return {spec.name: value.get("url") or cv.get("text") or None}
                return {spec.name: cv.get("text") or None}

        elif ctype == "long_text":
            spec = table.add_field(FieldSpec(title, "long_text"))

            def conv(cv, item, spec=spec):
                return {spec.name: cv.get("text") or None}

        elif ctype == "numbers":
            nums = [str(parse(cv)) for cv in values if parse(cv) is not None]
            decimals = max((len(n.split(".")[1]) if "." in n else 0 for n in nums), default=0)
            spec = table.add_field(FieldSpec(title, "number", decimals=min(decimals, 10)))

            def conv(cv, item, spec=spec):
                value = parse(cv)
                return {spec.name: None if value in (None, "") else str(value)}

        elif ctype == "date":
            with_time = any(isinstance(parse(cv), dict) and parse(cv).get("time") for cv in values)
            spec = table.add_field(FieldSpec(title, "datetime" if with_time else "date"))

            def conv(cv, item, spec=spec, with_time=with_time):
                value = parse(cv)
                if not isinstance(value, dict) or not value.get("date"):
                    return {spec.name: None}
                if with_time:
                    return {spec.name: to_utc_iso(f"{value['date']}T{value.get('time') or '00:00:00'}Z")}
                return {spec.name: value["date"]}

        elif ctype in ("creation_log", "last_updated"):
            spec = table.add_field(FieldSpec(title, "datetime"))

            def conv(cv, item, spec=spec, ctype=ctype):
                value = parse(cv) or {}
                key = "created_at" if ctype == "creation_log" else "updated_at"
                fallback = item.get("created_at") if ctype == "creation_log" else item.get("updated_at")
                stamp = value.get(key) if isinstance(value, dict) else None
                return {spec.name: to_utc_iso(stamp or fallback)}

        elif ctype == "checkbox":
            spec = table.add_field(FieldSpec(title, "checkbox"))

            def conv(cv, item, spec=spec):
                value = parse(cv)
                return {spec.name: bool(isinstance(value, dict) and value.get("checked"))}

        elif ctype in ("status", "dropdown"):
            options = labels_of(column)
            multi = ctype == "dropdown" and any(len((parse(cv) or {}).get("ids") or []) > 1 for cv in values if isinstance(parse(cv), dict))
            spec = table.add_field(FieldSpec(title, MULTI if multi else "single_select", options=list(dict.fromkeys(options.values()))))

            def conv(cv, item, spec=spec, ctype=ctype, options=options, multi=multi):
                value = parse(cv)
                if ctype == "dropdown":
                    names = [options.get(str(i)) for i in ((value or {}).get("ids") or [])] if isinstance(value, dict) else []
                    names = [n for n in names if n]
                    if not names and cv.get("text"):
                        names = [cv["text"]]
                else:
                    names = [cv["text"]] if cv.get("text") else []
                for n in names:
                    if n not in spec.options:
                        spec.options.append(n)
                if multi:
                    return {spec.name: "|".join(sorted(names)) or None}
                return {spec.name: names[0] if names else None}

        elif ctype == "people":
            spec = table.add_field(FieldSpec(title, "text"))

            def conv(cv, item, spec=spec):
                people = cv.get("persons_and_teams") or []
                emails = []
                for p in people:
                    if p.get("kind") == "person" and users.get(str(p["id"])):
                        emails.append(users[str(p["id"])])
                    else:
                        emails.append(f"{p.get('kind')}:{p.get('id')}")
                return {spec.name: ", ".join(emails) or (cv.get("text") or None)}

        elif ctype == "timeline":
            start = table.add_field(FieldSpec(f"{title} (inicio)", "date"))
            end = table.add_field(FieldSpec(f"{title} (fin)", "date"))
            spec = start  # el convertidor llena los dos campos

            def conv(cv, item, start=start, end=end):
                value = parse(cv) or {}
                frm = cv.get("from") or (value.get("from") if isinstance(value, dict) else None)
                to = cv.get("to") or (value.get("to") if isinstance(value, dict) else None)
                return {start.name: (frm or None) and str(frm)[:10], end.name: (to or None) and str(to)[:10]}

        elif ctype == "time_tracking":
            spec = table.add_field(FieldSpec(f"{title} (horas)", "number", decimals=2))

            def conv(cv, item, spec=spec):
                duration = cv.get("duration")
                return {spec.name: None if not duration else str((Decimal(duration) / 3600).quantize(Decimal("0.01")))}

        elif ctype in ("formula", "mirror", "lookup"):
            spec = table.add_field(FieldSpec(f"{title} (valor de Monday)", "long_text"))

            def conv(cv, item, spec=spec):
                value = cv.get("display_value")
                return {spec.name: (str(value) if value not in (None, "") else None)}

        elif ctype == "board_relation":
            settings = json.loads(column.get("settings_str") or "{}")
            targets = [str(t) for t in settings.get("boardIds") or []]
            target = next((board_tables[t] for t in targets if t in board_tables), None)
            if target and len(targets) == 1:
                spec = table.add_field(link_spec(title, target))

                def conv(cv, item, spec=spec):
                    ids = sorted(str(i) for i in (cv.get("linked_item_ids") or []))
                    return {spec.name: ",".join(ids) or None}

            else:
                spec = table.add_field(FieldSpec(f"{title} (valor de Monday)", "long_text"))
                table.notes["relacion_como_texto"] += 1

                def conv(cv, item, spec=spec):
                    return {spec.name: cv.get("display_value") or None}

        elif ctype == "file":
            spec = table.add_field(FieldSpec(title, FILES))
            converters.append((column, spec, None))  # los archivos se resuelven aparte
            external = any(
                f.get("fileType") != "ASSET" and not f.get("assetId")
                for cv in values if isinstance(parse(cv), dict)
                for f in parse(cv).get(FILES) or []
            )
            if external:
                links = table.add_field(FieldSpec(f"{title} (enlaces externos)", "long_text"))

                def conv(cv, item, links=links):
                    value = parse(cv) if isinstance(parse(cv), dict) else {}
                    found = [f"{f.get('name') or ''} {f.get('linkToFile') or f.get('url') or ''}".strip()
                             for f in value.get(FILES) or [] if not f.get("assetId")]
                    return {links.name: "\n".join(found) or None}

                converters.append((column, links, conv))
            continue

        else:
            spec = table.add_field(FieldSpec(title, "text"))
            table.notes[f"desconocido_como_texto:{ctype}"] += 1

            def conv(cv, item, spec=spec):
                return {spec.name: cv.get("text") or cv.get("display_value") or None}

        converters.append((column, spec, conv))
    return converters


def build_area(area, export_dir):
    plan = [p for p in json.loads(PLAN_FILE.read_text(encoding="utf-8")) if p["area"] == area]
    if not plan:
        raise FatalError(f"No hay tableros para el area {area} en {PLAN_FILE}")
    exports = {}
    for p in plan:
        d = Path(export_dir) / p["id"]
        try:
            exports[p["id"]] = (
                json.loads((d / "board.json").read_text(encoding="utf-8")),
                json.loads((d / "items.json").read_text(encoding="utf-8")),
                json.loads((d / "manifest.json").read_text(encoding="utf-8")),
                d,
            )
        except FileNotFoundError as error:
            raise FatalError(f"Falta el export del tablero {p['id']} ({p['name']}): {error.filename}") from None

    names = Counter(p["name"] for p in plan)
    board_tables = {p["id"]: (p["name"] if names[p["name"]] == 1 else f"{p['name']} ({p['id']})") for p in plan}
    tables = []
    problems = []
    for p in plan:
        board, items, manifest, d = exports[p["id"]]
        meta = board["board"]
        users = {str(u["id"]): u.get("email") for u in board.get("users") or []}
        files_by_owner = {}
        for entry in manifest["files"]:
            if "error" in entry:
                problems.append(f"{p['name']}: archivo {entry['asset_id']} fallo en el export")
                continue
            owner = entry.get("subitem_id") or entry["item_id"]
            files_by_owner[(str(owner), str(entry["asset_id"]))] = FileRef(entry["path"], entry["name"], entry["sha256"], entry["size"])
        problems += [f"{p['name']}: {k} {v}" for k, v in manifest["differences"].items() if v and k != "items_vs_monday"]
        if manifest["differences"].get("items_vs_monday"):
            problems.append(f"{p['name']}: el export difiere de Monday en {manifest['differences']['items_vs_monday']} items")

        def make_rows(table, source_items, converters, parent=None):
            for item in source_items:
                by_id = {cv["id"]: cv for cv in item["column_values"]}
                values = {"Name": item["name"], MONDAY_ID: item["id"], "Creado por": (item.get("creator") or {}).get("email")}
                if parent is None:
                    values["Grupo"] = (item.get("group") or {}).get("title")
                else:
                    values["Item padre"] = parent["id"]
                files = {}
                for column, spec, conv in converters:
                    cv = by_id.get(column["id"]) or {"id": column["id"], "type": column["type"]}
                    if conv is None:
                        refs = []
                        for f in ((parse(cv) or {}).get(FILES) or []) if isinstance(parse(cv), dict) else []:
                            if f.get("assetId"):
                                ref = files_by_owner.get((str(item["id"]), str(f["assetId"])))
                                if ref:
                                    refs.append(ref)
                                else:
                                    problems.append(f"{table.name}: item {item['id']} asset {f['assetId']} sin archivo")
                            else:
                                table.notes["enlace_externo_en_archivos"] += 1
                        files[spec.name] = refs
                    else:
                        values.update(conv(cv, item))
                table.rows.append(Row(item["id"], values, files))

        main = TableModel(board_tables[p["id"]], "Name", d)
        main.add_field(FieldSpec(MONDAY_ID, "text"))
        main.add_field(FieldSpec("Grupo", "single_select", options=[g["title"] for g in meta.get("groups") or []]))
        main.add_field(FieldSpec("Creado por", "text"))
        converters = build_columns(main, meta["columns"], items, users, board_tables)
        make_rows(main, items, converters)
        tables.append(main)

        subitems = [(s, item) for item in items for s in item.get("subitems") or []]
        if subitems:
            sub_meta = (board.get("subitem_boards") or [{}])[0]
            sub = TableModel(f"{main.name} · Subelementos", "Name", d)
            sub.add_field(FieldSpec(MONDAY_ID, "text"))
            sub.add_field(link_spec("Item padre", main.name))
            sub.add_field(FieldSpec("Creado por", "text"))
            sub_items = [s for s, _ in subitems]
            sub_conv = build_columns(sub, sub_meta.get("columns") or [], sub_items, users, board_tables)
            for s, parent in subitems:
                make_rows(sub, [s], sub_conv, parent=parent)
            tables.append(sub)

        comments = TableModel(f"{main.name} · Comentarios", MONDAY_ID, d)
        for spec in [
            link_spec("Item", main.name),
            FieldSpec("Subelemento", "text"),
            FieldSpec("Tipo", "single_select", options=["update", "reply"]),
            FieldSpec("Responde a", "text"),
            FieldSpec("Autor", "text"),
            FieldSpec("Fecha", "datetime"),
            FieldSpec("Texto", "long_text"),
            FieldSpec("HTML", "long_text"),
            FieldSpec("Archivos", FILES),
        ]:
            comments.add_field(spec)

        def add_comment(entry, kind, parent, owner, sub_id):
            refs = []
            for asset in entry.get("assets") or []:
                ref = files_by_owner.get((str(owner), str(asset["id"])))
                if ref:
                    refs.append(ref)
                else:
                    problems.append(f"{comments.name}: asset {asset['id']} sin archivo")
            comments.rows.append(Row(entry["id"], {
                MONDAY_ID: entry["id"], "Item": parent, "Subelemento": sub_id, "Tipo": kind,
                "Responde a": entry.get("_parent"), "Autor": (entry.get("creator") or {}).get("email"),
                "Fecha": to_utc_iso(entry.get("created_at")), "Texto": entry.get("text_body") or None,
                "HTML": entry.get("body") or None,
            }, {"Archivos": refs}))

        for item in items:
            for owner, sub_id in [(item, None)] + [(s, s["id"]) for s in item.get("subitems") or []]:
                for update in owner.get("updates") or []:
                    add_comment(update, "update", item["id"], owner["id"], sub_id)
                    for reply in update.get("replies") or []:
                        reply = dict(reply, _parent=update["id"])
                        add_comment(reply, "reply", item["id"], owner["id"], sub_id)
        if comments.rows:
            tables.append(comments)
    for t in tables:
        for spec in t.fields:
            if spec.kind == "single_select" or spec.kind == MULTI:
                spec.options = [o for o in dict.fromkeys(spec.options) if o]
    return tables, problems


# --- Baserow -----------------------------------------------------------------------


def field_body(spec, table_ids):
    if spec.kind == LINK:
        return {"type": "link_row", "link_row_table_id": table_ids[spec.target], "has_related_field": False}
    if spec.kind == MULTI:
        return {"type": "multiple_select", "select_options": [{"value": v, "color": COLORS[i % len(COLORS)]} for i, v in enumerate(spec.options)]}
    return Baserow._field_body(spec, None)


class Target:
    def __init__(self, area, url, email, password, host_header=None):
        self.b = Baserow(url, email, password)
        if host_header:
            self.b.http.headers["Host"] = host_header
        self.public = None
        self.area = area

    def connect(self):
        self.b.connect(False)
        name = AREA_NAMES[self.area]
        http = self.b.http
        ws = next((w for w in http.request("GET", "/api/workspaces/") if w["name"] == name), None)
        ws = ws or http.request("POST", "/api/workspaces/", {"name": name})
        apps = http.request("GET", f"/api/applications/workspace/{ws['id']}/")
        db = next((a for a in apps if a["type"] == "database" and a["name"] == name), None)
        db = db or http.request("POST", f"/api/applications/workspace/{ws['id']}/", {"type": "database", "name": name})
        self.database_id = db["id"]

    def tables(self, models):
        ids = {}
        existing = {t["name"]: t["id"] for t in self.b.http.request("GET", f"/api/database/tables/database/{self.database_id}/")}
        for t in models:
            if t.name in existing:
                ids[t.name] = existing[t.name]
            else:
                header = [t.primary] + ([MONDAY_ID] if t.primary != MONDAY_ID else [])
                ids[t.name] = self.b.http.request("POST", f"/api/database/tables/database/{self.database_id}/", {"name": t.name[:255], "data": [header], "first_row_header": True})["id"]
        return ids

    def ensure_fields(self, table_id, specs, table_ids):
        path = f"/api/database/fields/table/{table_id}/"
        existing = {f["name"] for f in self.b.http.request("GET", path)}
        for spec in specs:
            if spec.name not in existing:
                self.b.http.request("POST", path, {"name": spec.name, **field_body(spec, table_ids)})


def insert_rows(target, table_id, model, export_dir_of_row, workers):
    b = target.b
    existing = {r[MONDAY_ID] for r in b.rows(table_id)}
    pending = [r for r in model.rows if r.monday_id not in existing]
    plain = [s for s in model.fields if s.kind != LINK]

    def payload(row):
        body = {}
        for spec in plain:
            if spec.kind == FILES:
                body[spec.name] = [b.upload(ref, export_dir_of_row) for ref in row.files.get(spec.name) or []]
            elif spec.kind == MULTI:
                v = row.values.get(spec.name)
                body[spec.name] = v.split("|") if v else []
            elif row.values.get(spec.name) is not None:
                body[spec.name] = row.values[spec.name]
        return body

    for start in range(0, len(pending), BATCH):
        chunk = pending[start:start + BATCH]
        with ThreadPoolExecutor(max_workers=workers) as pool:
            payloads = list(pool.map(payload, chunk))
        b.insert(table_id, payloads)
    return len(existing), len(pending)


def read_rows(target, table_id, model, id_maps, public_origin, base_url):
    actual = {}
    for row in target.b.rows(table_id):
        values, files = {}, {}
        for spec in model.fields:
            raw = row.get(spec.name)
            if spec.kind == FILES:
                files[spec.name] = [
                    {"name": e["visible_name"], "url": e["url"].replace(public_origin, base_url, 1) if public_origin else e["url"], "size": e["size"]}
                    for e in raw or []
                ]
            elif spec.kind == "single_select":
                values[spec.name] = raw["value"] if raw else None
            elif spec.kind == MULTI:
                values[spec.name] = "|".join(sorted(o["value"] for o in raw or [])) or None
            elif spec.kind == LINK:
                m = id_maps.get(spec.target, {})
                values[spec.name] = ",".join(sorted(m.get(x["id"], f"?{x['id']}") for x in raw or [])) or None
            else:
                values[spec.name] = raw
        actual[row[MONDAY_ID]] = {"values": values, "files": files}
    return actual


def main():
    parser = argparse.ArgumentParser(description="Migra un area de Monday a Baserow.")
    parser.add_argument("area", choices=sorted(AREA_NAMES))
    parser.add_argument("--export-dir", default="export")
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--validate-only", action="store_true")
    parser.add_argument("--url", help="Entrar por otra direccion (ej. Tailscale); requiere --host-header")
    parser.add_argument("--host-header", help="Host publico que espera Baserow, ej. system.bdunity.com")
    parser.add_argument("--mac", action="store_true",
                        help="Usar la cuenta de la Mac (MAC_BASEROW_EMAIL / MAC_BASEROW_PASSWORD en .env)")
    args = parser.parse_args()
    if args.mac:
        mac = read_env("MAC_BASEROW_EMAIL", "MAC_BASEROW_PASSWORD")
        env = {"BASEROW_PUBLIC_URL": args.url or "", "BASEROW_EMAIL": mac["MAC_BASEROW_EMAIL"],
               "BASEROW_PASSWORD": mac["MAC_BASEROW_PASSWORD"]}
        if not (args.url and args.host_header):
            raise FatalError("Con --mac hay que indicar --url y --host-header.")
    else:
        env = read_env("BASEROW_PUBLIC_URL", "BASEROW_EMAIL", "BASEROW_PASSWORD")
    started = time.time()

    models, problems = build_area(args.area, args.export_dir)
    print(f"Area {args.area}: {len(models)} tablas, {sum(len(m.rows) for m in models)} filas en el export")

    url = args.url or env["BASEROW_PUBLIC_URL"]
    target = Target(args.area, url, env["BASEROW_EMAIL"], env["BASEROW_PASSWORD"], args.host_header)
    target.connect()
    table_ids = target.tables(models)
    for m in models:
        target.ensure_fields(table_ids[m.name], [s for s in m.fields if s.kind != LINK], table_ids)

    if not args.validate_only:
        for m in models:
            had, new = insert_rows(target, table_ids[m.name], m, m.export_dir, args.workers)
            print(f"  {m.name[:60]}: {had} ya existian, {new} creadas", flush=True)

    # Vinculos: se crean al final, cuando todas las filas existen.
    id_maps = {}
    for m in models:
        id_maps[m.name] = {r["id"]: r[MONDAY_ID] for r in target.b.rows(table_ids[m.name])}
    reverse = {name: {v: k for k, v in mp.items()} for name, mp in id_maps.items()}
    dangling = Counter()
    for m in models:
        links = [s for s in m.fields if s.kind == LINK]
        if not links:
            continue
        target.ensure_fields(table_ids[m.name], links, table_ids)
        for row in m.rows:
            for spec in links:
                wanted = [x for x in (row.values.get(spec.name) or "").split(",") if x]
                kept = [x for x in wanted if x in reverse.get(spec.target, {})]
                dangling[f"{m.name} / {spec.name}"] += len(wanted) - len(kept)
                row.values[spec.name] = ",".join(sorted(kept)) or None
        if not args.validate_only:
            current = {r[MONDAY_ID]: r for r in target.b.rows(table_ids[m.name])}
            updates = []
            for row in m.rows:
                body = {"id": reverse[m.name][row.monday_id]}
                for spec in links:
                    want = sorted(reverse[spec.target][x] for x in (row.values.get(spec.name) or "").split(",") if x)
                    have = sorted(x["id"] for x in current[row.monday_id].get(spec.name) or [])
                    if want != have:
                        body[spec.name] = want
                if len(body) > 1:
                    updates.append(body)
            for start in range(0, len(updates), BATCH):
                target.b.http.request("PATCH", f"/api/database/rows/table/{table_ids[m.name]}/batch/",
                                      {"items": updates[start:start + BATCH]}, query={"user_field_names": "true", "send_webhook_events": "false"})
            print(f"  vinculos {m.name[:55]}: {len(updates)} filas actualizadas", flush=True)

    print("Validando:")
    # Si se entra por otra direccion, las URLs de archivos (que usan el dominio publico) se
    # reescriben para descargarlas por la misma via y validar el hash.
    public_origin = f"https://{args.host_header}" if args.host_header else None
    report = {"area": args.area, "tables": {}, "problems": problems, "dangling_links": {k: v for k, v in dangling.items() if v}}
    bad = len(problems)
    for m in models:
        actual = read_rows(target, table_ids[m.name], m, id_maps, public_origin, url.rstrip("/"))
        diff, checked, spaces = validate_rows(m.name[:50], m.fields, m.rows, actual, target.b.http, args.workers)
        n = sum(len(v) for v in diff.values())
        bad += n
        report["tables"][m.name] = {"rows": len(m.rows), "files_checked": checked, "differences": diff,
                                    "whitespace_only": len(spaces), "notes": dict(m.notes)}
    report["seconds"] = round(time.time() - started, 1)
    out = Path(args.export_dir) / f"migration_report_{args.area}.json"
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Tablas: {len(models)} | filas: {sum(len(m.rows) for m in models)} | problemas del export: {len(problems)} | vinculos a items no exportados: {sum(dangling.values())}")
    print(f"Tiempo: {report['seconds']:.0f}s. Reporte: {out}")
    if bad:
        print(f"DIFERENCIAS: {bad}. Ver el reporte.")
        return 2
    print("Sin diferencias.")
    return 0


if __name__ == "__main__":
    run_main(main)
