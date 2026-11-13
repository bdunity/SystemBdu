#!/usr/bin/env python3
"""Detalles visuales y vistas de trabajo en Baserow (despues de migrar y de vistas_*.py).

- Emojis en espacios, bases y tablas.
- Descripciones en las columnas clave (se ven al pasar el mouse por el encabezado).
- Vistas de trabajo: en CPP "Urgentes" y "Pagadas este mes"; en Tickets "Abiertos",
  "Urgentes" y "Cerrados".

No toca los datos. Se puede volver a correr. Los scripts encuentran los nombres con o
sin emoji, asi que volver a correr la migracion no duplica nada.

Uso (CMD, desde la raiz del repo):
    python scripts\\pulir.py FINANZAS IT
    python scripts\\pulir.py COMERCIAL --mac --url http://100.93.138.115:8081 --host-header <dominio>
"""
import argparse

from import_baserow import Baserow
from import_common import FatalError, base_name, read_env, run_main
from migrate_area import AREA_NAMES

AREA_EMOJI = {"FINANZAS": "💰", "IT": "💻", "COMERCIAL": "📈"}
# Primera coincidencia gana: (texto en el nombre del tablero, emoji)
TABLE_EMOJI = [
    ("Cuentas por pagar", "💰"), ("Tickets", "🎫"), ("Plantilla", "📋"), ("Checklist", "✅"),
    ("Activos", "🔐"), ("Actividades", "🗓️"), ("Leads", "🎯"), ("Cotizaciones", "🧾"),
    ("Cartera", "🤝"), ("Clientes", "🤝"), ("Cuentas", "🏢"), ("Google Ads", "📢"),
    ("Marcas", "🏷️"), ("NDA", "📝"), ("Onboarding", "🚀"), ("selección", "👥"),
    ("Reclutamiento", "👥"), ("vacante", "👥"), ("Diagnostico", "🩺"), ("Beneficiario", "⚖️"),
    ("Control Documental", "🗂️"), ("Seminario", "🎓"), ("App PLD", "📱"), ("pruebas", "🧪"),
]

DESCRIPTIONS = {
    "Cuentas por pagar 2026": {
        "Name": "Concepto del pago: qué se paga y a quién.",
        "Estado": "Etapa del pago. 'Pagada' cuando ya salió el dinero.",
        "Prioridad": "Urgente = debe pagarse antes de la fecha programada.",
        "EMPRESA": "Razón social que realiza el pago.",
        "Presupuesto Neto": "Monto neto a pagar, en pesos.",
        "Fecha programada de pago": "Fecha en la que debe hacerse el pago.",
        "Fecha de pago": "Fecha en la que el pago se realizó.",
        "Factura / Payroll": "PDF y XML de la factura, o archivo de nómina.",
        "Comprobante de pago": "Comprobante del pago realizado; se envía al correo indicado.",
        "Autorizado": "Marcado cuando dirección autorizó el pago.",
        "Datos bancarios": "Banco, cuenta y CLABE del beneficiario. Información sensible.",
        "Grupo": "Grupo del tablero en Monday: Solicitudes, Pagadas o Canceladas.",
    },
    "Plataforma de Tickets 2026": {
        "Name": "Resumen del problema o la solicitud.",
        "Status": "Nuevo → Trabajando → Cerrado.",
        "Prioridad": "Urgente: bloquea el trabajo de alguien. Alto: hoy. Medio: esta semana. Bajo: cuando se pueda.",
        "Asignado": "Correo de quien atiende el ticket.",
        "Departamento": "Área que pidió el ticket.",
        "Grupo": "Grupo del tablero en Monday: Abierto, Trabajando o Cerrados.",
    },
}
DESCRIPTIONS["Plataforma de Tickets 2025"] = DESCRIPTIONS["Plataforma de Tickets 2026"]


def table_emoji(name):
    for text, emoji in TABLE_EMOJI:
        if text.lower() in name.lower():
            return emoji
    return None


def with_emoji(emoji, name):
    clean = base_name(name)
    return f"{emoji} {clean}" if emoji else clean


class Area:
    def __init__(self, http, area):
        self.http = http
        name = AREA_NAMES[area]
        ws = next((w for w in http.request("GET", "/api/workspaces/") if base_name(w["name"]) == name), None)
        if not ws:
            raise FatalError(f"No existe el espacio {name}.")
        db = next(a for a in http.request("GET", f"/api/applications/workspace/{ws['id']}/")
                  if a["type"] == "database" and base_name(a["name"]) == name)
        emoji = AREA_EMOJI[area]
        http.request("PATCH", f"/api/workspaces/{ws['id']}/", {"name": with_emoji(emoji, ws["name"])})
        http.request("PATCH", f"/api/applications/{db['id']}/", {"name": with_emoji(emoji, db["name"])})
        self.tables = {}
        for t in http.request("GET", f"/api/database/tables/database/{db['id']}/"):
            clean = base_name(t["name"])
            new = with_emoji(table_emoji(clean.split(" · ")[0]), clean)
            if new != t["name"]:
                http.request("PATCH", f"/api/database/tables/{t['id']}/", {"name": new})
            self.tables[clean] = t["id"]

    def fields(self, table):
        return {f["name"]: f for f in self.http.request("GET", f"/api/database/fields/table/{self.tables[table]}/")}

    def describe(self, table, descriptions):
        fields = self.fields(table)
        for name, text in descriptions.items():
            if name in fields and fields[name].get("description") != text:
                self.http.request("PATCH", f"/api/database/fields/{fields[name]['id']}/", {"description": text})

    def view(self, table, name, filters, sort=None, filter_type="AND"):
        """Crea (o reutiliza) una vista de cuadricula con las columnas de 'Todas'."""
        tid = self.tables[table]
        http = self.http
        views = {v["name"]: v for v in http.request("GET", f"/api/database/views/table/{tid}/")}
        view = views.get(name) or http.request("POST", f"/api/database/views/table/{tid}/",
                                               {"name": name, "type": "grid", "filter_type": filter_type})
        if "Todas" in views:
            options = http.request("GET", f"/api/database/views/{views['Todas']['id']}/field-options/")["field_options"]
            http.request("PATCH", f"/api/database/views/{view['id']}/field-options/", {"field_options": options})
        current = http.request("GET", f"/api/database/views/{view['id']}/filters/")
        for body in filters:
            if not any(c["field"] == body["field"] and c["type"] == body["type"] and str(c["value"]) == str(body["value"]) for c in current):
                http.request("POST", f"/api/database/views/{view['id']}/filters/", body)
        if sort:
            sortings = http.request("GET", f"/api/database/views/{view['id']}/sortings/")
            if not any(s["field"] == sort["field"] for s in sortings):
                http.request("POST", f"/api/database/views/{view['id']}/sortings/", sort)
        count = http.request("GET", f"/api/database/views/grid/{view['id']}/", query={"count": "true"})["count"]
        print(f"    vista '{name}': {count} filas")
        return count


def option_ids(field, values):
    ids = [str(o["id"]) for o in field["select_options"] if o["value"] in values]
    return ",".join(ids)


def finanzas(area):
    table = "Cuentas por pagar 2026"
    area.describe(table, DESCRIPTIONS[table])
    f = area.fields(table)
    not_paid = {"field": f["Estado"]["id"], "type": "single_select_not_equal",
                "value": option_ids(f["Estado"], ["Pagada"])}
    not_cancelled = {"field": f["Grupo"]["id"], "type": "single_select_not_equal",
                     "value": option_ids(f["Grupo"], ["Canceladas"])}
    urgent = {"field": f["Prioridad"]["id"], "type": "single_select_is_any_of",
              "value": option_ids(f["Prioridad"], ["Urgente", "URGENTE"])}
    area.view(table, "Urgentes", [urgent, not_paid, not_cancelled],
              {"field": f["Fecha programada de pago"]["id"], "order": "ASC"})
    paid = {"field": f["Estado"]["id"], "type": "single_select_equal", "value": option_ids(f["Estado"], ["Pagada"])}
    this_month = {"field": f["Fecha de pago"]["id"], "type": "date_is", "value": "America/Mexico_City??this_month"}
    area.view(table, "Pagadas este mes", [paid, this_month], {"field": f["Fecha de pago"]["id"], "order": "DESC"})


def it(area):
    for table in ("Plataforma de Tickets 2026", "Plataforma de Tickets 2025"):
        if table not in area.tables:
            continue
        print(f"  {table}")
        area.describe(table, DESCRIPTIONS[table])
        f = area.fields(table)
        newest = {"field": f["Creación"]["id"], "order": "DESC"} if "Creación" in f else None
        closed = option_ids(f["Status"], ["Cerrado"])
        area.view(table, "Abiertos", [{"field": f["Status"]["id"], "type": "single_select_not_equal", "value": closed}], newest)
        area.view(table, "Urgentes", [
            {"field": f["Status"]["id"], "type": "single_select_not_equal", "value": closed},
            {"field": f["Prioridad"]["id"], "type": "single_select_equal", "value": option_ids(f["Prioridad"], ["Urgente"])},
        ], newest)
        area.view(table, "Cerrados", [{"field": f["Status"]["id"], "type": "single_select_equal", "value": closed}], newest)


def main():
    parser = argparse.ArgumentParser(description="Emojis, descripciones y vistas de trabajo.")
    parser.add_argument("areas", nargs="+", choices=sorted(AREA_NAMES))
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
    for name in args.areas:
        print(f"{name}:")
        area = Area(client.http, name)
        if name == "FINANZAS":
            finanzas(area)
        elif name == "IT":
            it(area)
    return 0


if __name__ == "__main__":
    run_main(main)
