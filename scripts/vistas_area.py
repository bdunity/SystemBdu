#!/usr/bin/env python3
"""Ordena y colorea las tablas de un area migrada (IT, COMERCIAL) en Baserow.

Por cada tablero de Monday:
- Colores de los estados y de los grupos, como en Monday.
- Vista "Todas": columnas en el orden de Monday, nombre primero; ocultas monday_id,
  "Creado por" y las columnas vacias en todas las filas.
- Vista "Por grupo" si el tablero usa mas de un grupo.
- Tablas de Subelementos y Comentarios: solo lo util a la vista.

No toca los datos. Se puede volver a correr.

Uso (CMD, desde la raiz del repo):
    python scripts\\vistas_area.py IT
    python scripts\\vistas_area.py COMERCIAL --mac --url http://100.93.138.115:8081 --host-header <dominio>
"""
import argparse
import json
from collections import Counter
from pathlib import Path

from import_baserow import Baserow
from import_common import MONDAY_ID, FatalError, base_name, read_env, run_main
from migrate_area import AREA_NAMES, PLAN_FILE
from vistas_cpp import MONDAY_TO_BASEROW, SPARE_COLORS

ALWAYS_HIDDEN = {MONDAY_ID, "Creado por", "HTML", "Responde a"}
WIDTHS = {"Name": 300, "Texto": 420}


def is_empty(value):
    return value in (None, "", [], False) or (isinstance(value, str) and not value.strip())


def board_info(board_id):
    board = json.loads((Path("export") / board_id / "board.json").read_text(encoding="utf-8"))["board"]
    colors, order = {}, []
    for column in board["columns"]:
        order.append(column["title"])
        if column["type"] != "status":
            continue
        settings = json.loads(column.get("settings_str") or "{}")
        labels = settings.get("labels") or {}
        palette = settings.get("labels_colors") or {}
        colors[column["title"]] = {
            name: MONDAY_TO_BASEROW.get(((palette.get(key) or {}).get("color") or "").lower())
            for key, name in labels.items() if name
        }
    colors["Grupo"] = {g["title"]: MONDAY_TO_BASEROW.get((g.get("color") or "").lower()) for g in board.get("groups") or []}
    return colors, order


def base_title(field_name):
    """'Fecha (inicio)' -> 'Fecha'; 'Total (valor de Monday)' -> 'Total'."""
    for suffix in (" (valor de Monday)", " (inicio)", " (fin)", " (horas)", " (enlaces externos)", " (Monday)"):
        if field_name.endswith(suffix):
            return field_name[: -len(suffix)]
    return field_name


def style_table(http, client, table, colors, order, kind):
    tid = table["id"]
    fields = {f["name"]: f for f in http.request("GET", f"/api/database/fields/table/{tid}/")}
    rows = client.rows(tid)
    filled = Counter()
    for row in rows:
        for name, value in row.items():
            if not is_empty(value):
                filled[name] += 1

    # Colores
    for name, field in fields.items():
        if field["type"] not in ("single_select", "multiple_select"):
            continue
        wanted = colors.get(base_title(name), {})
        options = [
            {"id": o["id"], "value": o["value"],
             "color": wanted.get(o["value"]) or (o["color"] if wanted else SPARE_COLORS[i % len(SPARE_COLORS)])}
            for i, o in enumerate(field["select_options"])
        ]
        http.request("PATCH", f"/api/database/fields/{field['id']}/", {"select_options": options})

    # Orden: Name primero, luego el de Monday; Grupo y lo propio de la migracion al final.
    def position(name):
        if name in ("Name", MONDAY_ID) and kind == "main":
            return -1
        title = base_title(name)
        return order.index(title) if title in order else 900

    def hidden(name):
        primary = fields[name].get("primary")
        return not primary and (name in ALWAYS_HIDDEN or not filled[name])

    def options(hide_group=False):
        return {
            str(f["id"]): {
                "hidden": hidden(name) or (hide_group and name == "Grupo"),
                "order": position(name),
                "width": WIDTHS.get(name, 200),
            }
            for name, f in fields.items()
        }

    views = {v["name"]: v for v in http.request("GET", f"/api/database/views/table/{tid}/")}
    todas = views.get("Todas")
    if not todas:
        grid = next((v for v in views.values() if v["type"] == "grid"), None)
        todas = (http.request("PATCH", f"/api/database/views/{grid['id']}/", {"name": "Todas"}) if grid
                 else http.request("POST", f"/api/database/views/table/{tid}/", {"name": "Todas", "type": "grid"}))
    http.request("PATCH", f"/api/database/views/{todas['id']}/field-options/", {"field_options": options()})

    groups_used = len({(r.get("Grupo") or {}).get("value") for r in rows if r.get("Grupo")})
    made = ["Todas"]
    if kind == "main" and groups_used > 1:
        por_grupo = views.get("Por grupo") or http.request(
            "POST", f"/api/database/views/table/{tid}/", {"name": "Por grupo", "type": "grid"})
        http.request("PATCH", f"/api/database/views/{por_grupo['id']}/field-options/", {"field_options": options(True)})
        current = http.request("GET", f"/api/database/views/{por_grupo['id']}/group_bys/")
        if not any(g["field"] == fields["Grupo"]["id"] for g in current):
            http.request("POST", f"/api/database/views/{por_grupo['id']}/group_bys/",
                         {"field": fields["Grupo"]["id"], "order": "ASC"})
        made.append("Por grupo")
    hidden_count = sum(1 for n in fields if hidden(n))
    return made, len(fields) - hidden_count, hidden_count


def main():
    parser = argparse.ArgumentParser(description="Vistas y colores de un area migrada.")
    parser.add_argument("area", choices=[a for a in sorted(AREA_NAMES) if a != "FINANZAS"])
    parser.add_argument("--mac", action="store_true")
    parser.add_argument("--url")
    parser.add_argument("--host-header")
    args = parser.parse_args()
    if args.mac:
        if not (args.url and args.host_header):
            raise FatalError("Con --mac hay que indicar --url y --host-header.")
        env = read_env("MAC_BASEROW_EMAIL", "MAC_BASEROW_PASSWORD")
        client = Baserow(args.url, env["MAC_BASEROW_EMAIL"], env["MAC_BASEROW_PASSWORD"])
        client.http.headers["Host"] = args.host_header
    else:
        env = read_env("BASEROW_PUBLIC_URL", "BASEROW_EMAIL", "BASEROW_PASSWORD")
        client = Baserow(env["BASEROW_PUBLIC_URL"], env["BASEROW_EMAIL"], env["BASEROW_PASSWORD"])
    client.connect(False)
    http = client.http

    name = AREA_NAMES[args.area]
    ws = next((w for w in http.request("GET", "/api/workspaces/") if base_name(w["name"]) == name), None)
    if not ws:
        raise FatalError(f"No existe el espacio {name}.")
    db = next(a for a in http.request("GET", f"/api/applications/workspace/{ws['id']}/")
              if a["type"] == "database" and base_name(a["name"]) == name)
    tables = {base_name(t["name"]): t for t in http.request("GET", f"/api/database/tables/database/{db['id']}/")}

    plan = [p for p in json.loads(PLAN_FILE.read_text(encoding="utf-8")) if p["area"] == args.area]
    names = Counter(p["name"] for p in plan)
    for p in plan:
        main_name = p["name"] if names[p["name"]] == 1 else f"{p['name']} ({p['id']})"
        colors, order = board_info(p["id"])
        for suffix, kind in (("", "main"), (" · Subelementos", "sub"), (" · Comentarios", "comments")):
            table = tables.get(base_name(main_name + suffix))
            if not table:
                continue
            made, shown, hid = style_table(http, client, table, colors, order, kind)
            print(f"  {table['name'][:60]}: vistas {made} | {shown} columnas visibles, {hid} ocultas", flush=True)
    return 0


if __name__ == "__main__":
    run_main(main)
