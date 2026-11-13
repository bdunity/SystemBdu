#!/usr/bin/env python3
"""Deja la tabla "Cuentas por pagar 2026" (espacio Finanzas) lista para usarse en Baserow.

- Colores de Estado, Prioridad, Departamento, Metodo de pago y EMPRESA como en Monday.
- Vista "Todas": columnas en orden util, las tecnicas ocultas, lo mas reciente primero.
- Vista "Por grupo": agrupada como el tablero de Monday (Solicitudes, Pagadas, Canceladas).
- Vista "Pendientes de pago": ni pagadas ni canceladas, por fecha programada de pago.
- Formulario "Solicitud CPP" con campos obligatorios. NO se publica: se comparte a mano.

No toca los datos. Se puede volver a correr.

Uso (CMD, desde la raiz del repo):
    python scripts\\vistas_cpp.py
    python scripts\\vistas_cpp.py --mac --url http://100.93.138.115:8081 --host-header <dominio>
"""
import argparse
import json
from pathlib import Path

from import_baserow import Baserow
from import_common import FatalError, base_name, read_env, run_main

WORKSPACE = "Finanzas"
TABLE = "Cuentas por pagar 2026"
BOARD_EXPORT = Path("export/18393961251/board.json")

# Monday usa colores hexadecimales; Baserow, una paleta con nombre.
MONDAY_TO_BASEROW = {
    "#00c875": "green", "#037f4c": "dark-green", "#9cd326": "light-green",
    "#df2f4a": "red", "#ffcb00": "yellow", "#fdab3d": "orange",
    "#66ccff": "light-blue", "#579bfc": "blue", "#007eb5": "dark-blue",
    "#4eccc6": "cyan", "#a1e3f6": "light-cyan",
    "#9d50dd": "purple", "#784bd1": "dark-purple",
    "#faa1f1": "light-pink", "#ff5ac4": "pink", "#ff007f": "dark-pink",
    "#7f5347": "brown", "#c4c4c4": "light-gray",
}
SPARE_COLORS = ["blue", "green", "orange", "purple", "cyan", "pink", "yellow", "brown", "red", "gray", "dark-blue"]

# Orden de columnas en las vistas de cuadricula; lo que no este aqui queda oculto.
VISIBLE = [
    "Name", "Estado", "Prioridad", "EMPRESA", "Departamento", "Presupuesto Neto",
    "Fecha programada de pago", "Método de pago", "Factura / Payroll", "No de Factura",
    "Autorizado", "Fecha de pago", "Comprobante de pago", "Fecha de solicitud", "Solicitante",
    "Correo enviar el comprobante de pago", "Carta Bancaria", "Datos bancarios", "Registro",
    "Aviso de Privacidad", "Grupo",
]
WIDTHS = {"Name": 300, "Datos bancarios": 260, "Correo enviar el comprobante de pago": 240}

# Formulario: (campo, etiqueta, obligatorio, descripcion)
FORM_FIELDS = [
    ("Name", "Concepto del pago", True, "Ej. Pago de nómina quincena 1, factura proveedor X."),
    ("Solicitante", "Solicitante", True, ""),
    ("Correo enviar el comprobante de pago", "Correo para enviar el comprobante", True, ""),
    ("Departamento", "Departamento", True, ""),
    ("EMPRESA", "Empresa que paga", True, ""),
    ("Método de pago", "Método de pago", True, ""),
    ("Prioridad", "Prioridad", True, ""),
    ("Presupuesto Neto", "Monto neto", True, ""),
    ("Fecha programada de pago", "Fecha en que debe pagarse", False, ""),
    ("No de Factura", "Número de factura", False, ""),
    ("Factura / Payroll", "Factura o payroll", True, "PDF y XML de la factura, o el archivo de nómina."),
    ("Datos bancarios", "Datos bancarios", False, "Banco, cuenta y CLABE si el pago es por transferencia."),
    ("Carta Bancaria", "Carta bancaria", False, "Solo si es un proveedor nuevo o cambió su cuenta."),
    ("Aviso de Privacidad", "Acepto el aviso de privacidad", True, ""),
]


def monday_colors():
    """{columna: {opcion: color de Baserow}} a partir del export de Monday."""
    board = json.loads(BOARD_EXPORT.read_text(encoding="utf-8"))["board"]
    out = {}
    for column in board["columns"]:
        if column["type"] != "status":
            continue
        settings = json.loads(column.get("settings_str") or "{}")
        labels = settings.get("labels") or {}
        colors = settings.get("labels_colors") or {}
        out[column["title"]] = {
            name: MONDAY_TO_BASEROW.get(((colors.get(key) or {}).get("color") or "").lower())
            for key, name in labels.items() if name
        }
    out["Grupo"] = {g["title"]: MONDAY_TO_BASEROW.get((g.get("color") or "").lower()) for g in board["groups"]}
    return out


def main():
    parser = argparse.ArgumentParser(description="Vistas y colores de CPP en Baserow.")
    parser.add_argument("--mac", action="store_true", help="Usar MAC_BASEROW_EMAIL / MAC_BASEROW_PASSWORD")
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

    ws = next((w for w in http.request("GET", "/api/workspaces/") if base_name(w["name"]) == WORKSPACE), None)
    if not ws:
        raise FatalError(f"No existe el espacio {WORKSPACE}; corre primero migrate_area.py FINANZAS.")
    db = next(a for a in http.request("GET", f"/api/applications/workspace/{ws['id']}/") if a["type"] == "database")
    table = next((t for t in http.request("GET", f"/api/database/tables/database/{db['id']}/") if base_name(t["name"]) == TABLE), None)
    if not table:
        raise FatalError(f"No existe la tabla {TABLE}.")
    tid = table["id"]
    fields = {f["name"]: f for f in http.request("GET", f"/api/database/fields/table/{tid}/")}

    # 1. Colores
    colors = monday_colors()
    for name, field in fields.items():
        if field["type"] != "single_select":
            continue
        wanted = colors.get(name, {})
        options = []
        for i, option in enumerate(field["select_options"]):
            color = wanted.get(option["value"]) or (SPARE_COLORS[i % len(SPARE_COLORS)] if name not in colors else option["color"])
            options.append({"id": option["id"], "value": option["value"], "color": color})
        http.request("PATCH", f"/api/database/fields/{field['id']}/", {"select_options": options})
    print("colores aplicados")

    # 2. Vistas de cuadricula
    views = {v["name"]: v for v in http.request("GET", f"/api/database/views/table/{tid}/")}
    if "Todas" not in views:
        first_grid = next((v for v in views.values() if v["type"] == "grid"), None)
        if first_grid:
            views["Todas"] = http.request("PATCH", f"/api/database/views/{first_grid['id']}/", {"name": "Todas"})
        else:
            views["Todas"] = http.request("POST", f"/api/database/views/table/{tid}/", {"name": "Todas", "type": "grid"})
    for name in ("Por grupo", "Pendientes de pago"):
        if name not in views:
            views[name] = http.request("POST", f"/api/database/views/table/{tid}/", {"name": name, "type": "grid", "filter_type": "AND"})

    def field_options(hide_group=False):
        options = {}
        for name, field in fields.items():
            visible = name in VISIBLE and not (hide_group and name == "Grupo")
            options[str(field["id"])] = {
                "hidden": not visible,
                "order": VISIBLE.index(name) if name in VISIBLE else 999,
                "width": WIDTHS.get(name, 180),
            }
        return options

    def ensure(view, kind, body):
        path = f"/api/database/views/{view['id']}/{kind}/"
        current = http.request("GET", path)
        if not any(all(c.get(k) == v for k, v in body.items()) for c in current):
            http.request("POST", path, body)

    newest = {"field": fields["Fecha de solicitud"]["id"], "order": "DESC"}
    for name in ("Todas", "Por grupo", "Pendientes de pago"):
        http.request("PATCH", f"/api/database/views/{views[name]['id']}/field-options/",
                     {"field_options": field_options(hide_group=name == "Por grupo")})
    ensure(views["Todas"], "sortings", newest)
    ensure(views["Por grupo"], "group_bys", {"field": fields["Grupo"]["id"], "order": "ASC"})
    ensure(views["Por grupo"], "sortings", newest)
    status = {o["value"]: o["id"] for o in fields["Estado"]["select_options"]}
    group = {o["value"]: o["id"] for o in fields["Grupo"]["select_options"]}
    if "Pagada" in status:
        ensure(views["Pendientes de pago"], "filters",
               {"field": fields["Estado"]["id"], "type": "single_select_not_equal", "value": str(status["Pagada"])})
    if "Canceladas" in group:
        ensure(views["Pendientes de pago"], "filters",
               {"field": fields["Grupo"]["id"], "type": "single_select_not_equal", "value": str(group["Canceladas"])})
    ensure(views["Pendientes de pago"], "sortings", {"field": fields["Fecha programada de pago"]["id"], "order": "ASC"})
    print("vistas: Todas, Por grupo, Pendientes de pago")

    # 3. Formulario (no publico)
    form = views.get("Solicitud CPP") or http.request("POST", f"/api/database/views/table/{tid}/", {
        "name": "Solicitud CPP", "type": "form",
    })
    http.request("PATCH", f"/api/database/views/{form['id']}/", {
        "title": "Solicitud de pago",
        "description": "Llena este formulario para solicitar un pago a Cuentas por pagar. "
                       "Los campos marcados con * son obligatorios.",
        "submit_text": "Enviar solicitud",
        "submit_action": "MESSAGE",
        "submit_action_message": "Gracias, tu solicitud fue recibida. Finanzas la revisará.",
        "public": False,
    })
    options = {}
    form_names = [f[0] for f in FORM_FIELDS]
    for name, field in fields.items():
        if name in form_names:
            _, label, required, help_text = FORM_FIELDS[form_names.index(name)]
            options[str(field["id"])] = {"enabled": True, "required": required, "name": label,
                                         "description": help_text, "order": form_names.index(name)}
        else:
            options[str(field["id"])] = {"enabled": False}
    http.request("PATCH", f"/api/database/views/{form['id']}/field-options/", {"field_options": options})
    print("formulario 'Solicitud CPP' listo (no publico)")
    return 0


if __name__ == "__main__":
    run_main(main)
