#!/usr/bin/env python3
"""Pruebas de funciones para el scorecard (tarea 6), en Baserow y Teable.

Crea vistas con prefijo "PoC" (formulario, kanban, filtro por responsable) y prueba
el formulario con adjunto obligatorio, la papelera y deshacer. Las filas de prueba
van a la tabla ficticia "Prueba n8n" y se borran al final. Sobre CPP solo crea vistas.

Uso (CMD, desde la raiz del repo):
    python scripts\\feature_checks.py

Escribe export/feature_checks.json y no imprime datos de CPP, solo conteos.
"""
import json
import uuid
from collections import Counter
from pathlib import Path

from create_test_tables import TABLE as TEST_TABLE
from create_test_tables import fake_pdf
from import_baserow import Baserow
from import_common import ITEMS_TABLE, MONDAY_ID, ApiError, HttpClient, read_env, run_main
from import_teable import Teable

FORM_VIEW = "PoC Solicitud CPP"
KANBAN_VIEW = "PoC Kanban por Estado"
FILTER_VIEW = "PoC Filtro por responsable"
RESPONSIBLE_FIELD = "Creado por"
STATUS_FIELD = "Estado"


def expected_counts():
    """Conteos de referencia salidos del export de Monday."""
    items = json.loads(Path("export/18393961251/items.json").read_text(encoding="utf-8"))
    status = Counter(next(c["text"] for c in item["column_values"] if c["id"] == "estado7") or "" for item in items)
    responsible = Counter((item.get("creator") or {}).get("email") for item in items)
    email, count = responsible.most_common(1)[0]
    return status, email, count


def attempt(results, key, function):
    """Ejecuta una prueba y guarda el resultado o el error, sin detener las demas."""
    try:
        results[key] = function()
    except ApiError as error:
        results[key] = {"ok": False, "error": str(error)[:300]}
    status = results[key].get("ok")
    print(f"  {key}: {'OK' if status else 'FALLA' if status is False else 'ver detalle'}")


# --- Baserow -------------------------------------------------------------------------


def baserow_checks(status_ref, email, email_count):
    env = read_env("BASEROW_PUBLIC_URL", "BASEROW_EMAIL", "BASEROW_PASSWORD")
    client = Baserow(env["BASEROW_PUBLIC_URL"], env["BASEROW_EMAIL"], env["BASEROW_PASSWORD"])
    client.connect(False)
    http = client.http
    database_id = client.database(client.workspace())
    test_table = client.table(database_id, TEST_TABLE, "Name")
    cpp_table = client.table(database_id, ITEMS_TABLE, "Name", MONDAY_ID)
    fields = lambda table: {f["name"]: f for f in http.request("GET", f"/api/database/fields/table/{table}/")}
    test_fields = fields(test_table)
    cpp_fields = fields(cpp_table)

    def view(table, name, body):
        for existing in http.request("GET", f"/api/database/views/table/{table}/"):
            if existing["name"] == name:
                return existing
        return http.request("POST", f"/api/database/views/table/{table}/", {"name": name, **body})

    results = {}

    def form():
        form_view = view(test_table, FORM_VIEW, {"type": "form", "public": True})
        options = {
            str(test_fields["Name"]["id"]): {"enabled": True, "required": True},
            str(test_fields["Archivo"]["id"]): {"enabled": True, "required": True},
        }
        http.request("PATCH", f"/api/database/views/{form_view['id']}/field-options/", {"field_options": options})
        slug = form_view["slug"]
        anonymous = HttpClient(http.base_url)
        name_key = f"field_{test_fields['Name']['id']}"
        file_key = f"field_{test_fields['Archivo']['id']}"
        try:
            anonymous.request("POST", f"/api/database/views/form/{slug}/submit/", {name_key: "FORM SIN ARCHIVO"}, retries=1)
            rejected = False
            reason = None
        except ApiError as error:
            rejected = True
            reason = str(error)[:160]
        temporary = Path("export/_form_prueba.pdf")
        temporary.write_bytes(fake_pdf("FORMULARIO"))
        try:
            uploaded = anonymous.upload(f"/api/database/views/form/{slug}/upload-file/", temporary, "formulario.pdf")
        finally:
            temporary.unlink()
        anonymous.request(
            "POST",
            f"/api/database/views/form/{slug}/submit/",
            {name_key: "FORM CON ARCHIVO", file_key: [{"name": uploaded["name"]}]},
        )
        created = [r for r in client.rows(test_table) if r["Name"] in ("FORM SIN ARCHIVO", "FORM CON ARCHIVO")]
        stored = [(r["Name"], len(r["Archivo"])) for r in created]
        for row in created:
            http.request("DELETE", f"/api/database/rows/table/{test_table}/{row['id']}/")
        return {
            "ok": rejected and stored == [("FORM CON ARCHIVO", 1)],
            "public_url": f"{http.base_url}/form/{slug}",
            "sin_archivo_rechazado": rejected,
            "motivo_rechazo": reason,
            "con_archivo_guardado": stored,
            "subida_anonima": True,
        }

    def kanban():
        kanban_view = view(cpp_table, KANBAN_VIEW, {"type": "kanban", "single_select_field": cpp_fields[STATUS_FIELD]["id"]})
        data = http.request("GET", f"/api/database/views/kanban/{kanban_view['id']}/", query={"limit": 1})
        options = {str(o["id"]): o["value"] for o in cpp_fields[STATUS_FIELD]["select_options"]}
        counts = {options.get(key, "(vacio)"): value["count"] for key, value in data["rows"].items() if value["count"]}
        want = {k or "(vacio)": v for k, v in status_ref.items()}
        return {"ok": counts == want, "conteo_por_estado": counts, "esperado": want}

    def responsible_filter():
        grid = view(cpp_table, FILTER_VIEW, {"type": "grid", "filter_type": "AND"})
        if not http.request("GET", f"/api/database/views/{grid['id']}/filters/"):
            body = {"field": cpp_fields[RESPONSIBLE_FIELD]["id"], "type": "equal", "value": email}
            http.request("POST", f"/api/database/views/{grid['id']}/filters/", body)
        data = http.request("GET", f"/api/database/views/grid/{grid['id']}/", query={"count": "true"})
        return {"ok": data["count"] == email_count, "filas": data["count"], "esperado": email_count}

    def trash():
        row = http.request(
            "POST", f"/api/database/rows/table/{test_table}/", {"Name": "BORRAR Y RECUPERAR"}, query={"user_field_names": "true"}
        )
        http.request("DELETE", f"/api/database/rows/table/{test_table}/{row['id']}/")
        gone = all(r["id"] != row["id"] for r in client.rows(test_table))
        body = {"trash_item_type": "row", "trash_item_id": row["id"], "parent_trash_item_id": test_table}
        http.request("PATCH", "/api/trash/restore/", body)
        back = any(r["id"] == row["id"] for r in client.rows(test_table))
        http.request("DELETE", f"/api/database/rows/table/{test_table}/{row['id']}/")
        return {"ok": gone and back, "borrado": gone, "recuperado_de_papelera": back}

    def undo():
        session = str(uuid.uuid4())
        row = client.rows(test_table)[0]
        before = row["Estado"]["value"]
        http.headers["ClientSessionId"] = session
        try:
            http.request(
                "PATCH",
                f"/api/database/rows/table/{test_table}/{row['id']}/",
                {"Estado": "Enviado"},
                query={"user_field_names": "true"},
            )
            http.request("PATCH", "/api/user/undo/", {"scopes": {"table": test_table}})
        finally:
            http.headers.pop("ClientSessionId", None)
        after = next(r for r in client.rows(test_table) if r["id"] == row["id"])["Estado"]["value"]
        return {"ok": after == before, "antes": before, "despues_de_deshacer": after}

    for key, function in [("formulario", form), ("kanban", kanban), ("filtro_responsable", responsible_filter), ("papelera", trash), ("deshacer", undo)]:
        attempt(results, key, function)
    return results


# --- Teable --------------------------------------------------------------------------


def teable_checks(status_ref, email, email_count):
    env = read_env("TEABLE_PUBLIC_ORIGIN", "TEABLE_EMAIL", "TEABLE_PASSWORD")
    client = Teable(env["TEABLE_PUBLIC_ORIGIN"], env["TEABLE_EMAIL"], env["TEABLE_PASSWORD"])
    client.connect(False)
    http = client.http
    base_id = client.base(client.space())
    test_table = client.table(base_id, TEST_TABLE, "Name")
    cpp_table = client.table(base_id, ITEMS_TABLE, "Name")
    fields = lambda table: {f["name"]: f for f in http.request("GET", f"/api/table/{table}/field")}
    test_fields = fields(test_table)
    cpp_fields = fields(cpp_table)

    def view(table, name, body):
        for existing in http.request("GET", f"/api/table/{table}/view"):
            if existing["name"] == name:
                return existing
        return http.request("POST", f"/api/table/{table}/view", {"name": name, **body})

    results = {}

    def form():
        form_view = view(test_table, FORM_VIEW, {"type": "form"})
        meta = [
            {"fieldId": test_fields["Name"]["id"], "columnMeta": {"visible": True, "required": True, "order": 0}},
            {"fieldId": test_fields["Archivo"]["id"], "columnMeta": {"visible": True, "required": True, "order": 1}},
        ]
        http.request("PUT", f"/api/table/{test_table}/view/{form_view['id']}/column-meta", meta)
        share_id = form_view.get("shareId") or http.request(
            "POST", f"/api/table/{test_table}/view/{form_view['id']}/enable-share"
        )["shareId"]
        anonymous = HttpClient(http.base_url)
        name_id = test_fields["Name"]["id"]
        try:
            anonymous.request("POST", f"/api/share/{share_id}/view/form-submit", {"fields": {name_id: "FORM SIN ARCHIVO"}}, retries=1)
            rejected = False
            reason = None
        except ApiError as error:
            rejected = True
            reason = str(error)[:160]
        # Subida anonima como lo hace la UI: firma, subida y aviso con el encabezado Tea-Share-Id.
        anonymous.headers["Tea-Share-Id"] = share_id
        content = fake_pdf("FORMULARIO")
        upload_steps = {}
        try:
            signature = anonymous.request(
                "POST",
                "/api/attachments/signature",
                {"contentType": "application/pdf", "contentLength": len(content), "type": 1},
                retries=1,
            )
            upload_steps["firma"] = True
            anonymous.request(
                signature.get("uploadMethod", "PUT").upper(),
                signature["url"],
                raw_body=content,
                content_type="application/pdf",
                retries=1,
            )
            notified = anonymous.request("POST", f"/api/attachments/notify/{signature['token']}", query={"filename": "formulario.pdf"}, retries=1)
            attachment = {"name": "formulario.pdf", "token": notified["token"]}
            upload_steps["subida"] = True
        except ApiError as error:
            upload_steps["error"] = str(error)[:200]
            attachment = None
        if attachment:
            fields_body = {test_fields["Name"]["id"]: "FORM CON ARCHIVO", test_fields["Archivo"]["id"]: [attachment]}
            anonymous.request("POST", f"/api/share/{share_id}/view/form-submit", {"fields": fields_body}, retries=1)
        created = [r for r in client.records(test_table) if r["fields"].get("Name") in ("FORM SIN ARCHIVO", "FORM CON ARCHIVO")]
        stored = [(r["fields"]["Name"], len(r["fields"].get("Archivo") or [])) for r in created]
        for record in created:
            http.request("DELETE", f"/api/table/{test_table}/record/{record['id']}")
        return {
            "ok": rejected and stored == [("FORM CON ARCHIVO", 1)],
            "public_url": f"{http.base_url}/share/{share_id}/view",
            "sin_archivo_rechazado": rejected,
            "motivo_rechazo": reason,
            "con_archivo_guardado": stored,
            "subida_anonima": upload_steps,
        }

    def kanban():
        kanban_view = view(cpp_table, KANBAN_VIEW, {"type": "kanban", "options": {"stackFieldId": cpp_fields[STATUS_FIELD]["id"]}})
        counts = Counter()
        for record in client.records(cpp_table):
            counts[record["fields"].get(STATUS_FIELD) or "(vacio)"] += 1
        want = {k or "(vacio)": v for k, v in status_ref.items()}
        return {"ok": kanban_view["type"] == "kanban" and dict(counts) == want, "vista": kanban_view["type"], "conteo_por_estado": dict(counts), "esperado": want}

    def responsible_filter():
        grid = view(cpp_table, FILTER_VIEW, {"type": "grid"})
        condition = {"fieldId": cpp_fields[RESPONSIBLE_FIELD]["id"], "operator": "is", "value": email}
        http.request("PUT", f"/api/table/{cpp_table}/view/{grid['id']}/filter", {"filter": {"conjunction": "and", "filterSet": [condition]}})
        count = 0
        while True:
            data = http.request("GET", f"/api/table/{cpp_table}/record", query={"viewId": grid["id"], "take": 1000, "skip": count})
            count += len(data["records"])
            if len(data["records"]) < 1000:
                break
        return {"ok": count == email_count, "filas": count, "esperado": email_count}

    def trash():
        created = http.request("POST", f"/api/table/{test_table}/record", {"fieldKeyType": "name", "records": [{"fields": {"Name": "BORRAR Y RECUPERAR"}}]})
        record_id = created["records"][0]["id"]
        http.request("DELETE", f"/api/table/{test_table}/record/{record_id}")
        gone = all(r["id"] != record_id for r in client.records(test_table))
        items = http.request("GET", "/api/trash/items", query={"resourceType": "table", "resourceId": test_table, "pageSize": 20})
        entries = items.get("trashItems") or []
        target = next((e for e in entries if record_id in json.dumps(e)), None)
        if not target:
            return {"ok": False, "borrado": gone, "error": "no aparece en la papelera", "items": len(entries)}
        http.request("POST", f"/api/trash/restore/{target['id']}", query={"tableId": test_table})
        back = any(r["id"] == record_id for r in client.records(test_table))
        http.request("DELETE", f"/api/table/{test_table}/record/{record_id}")
        return {"ok": gone and back, "borrado": gone, "recuperado_de_papelera": back}

    def undo():
        window = str(uuid.uuid4())
        record = client.records(test_table)[0]
        before = record["fields"].get(STATUS_FIELD)
        http.headers["x-window-id"] = window
        try:
            http.request(
                "PATCH",
                f"/api/table/{test_table}/record/{record['id']}",
                {"fieldKeyType": "name", "record": {"fields": {STATUS_FIELD: "Enviado"}}},
            )
            http.request("POST", f"/api/table/{test_table}/undo-redo/undo")
        finally:
            http.headers.pop("x-window-id", None)
        after = next(r for r in client.records(test_table) if r["id"] == record["id"])["fields"].get(STATUS_FIELD)
        return {"ok": after == before, "antes": before, "despues_de_deshacer": after}

    for key, function in [("formulario", form), ("kanban", kanban), ("filtro_responsable", responsible_filter), ("papelera", trash), ("deshacer", undo)]:
        attempt(results, key, function)
    return results


def main():
    status_ref, email, email_count = expected_counts()
    print("Baserow:")
    baserow = baserow_checks(status_ref, email, email_count)
    print("Teable:")
    teable = teable_checks(status_ref, email, email_count)
    report = {"baserow": baserow, "teable": teable}
    Path("export/feature_checks.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print("Detalle en export/feature_checks.json")
    return 0 if all(r.get("ok") for tool in report.values() for r in tool.values()) else 2


if __name__ == "__main__":
    run_main(main)
