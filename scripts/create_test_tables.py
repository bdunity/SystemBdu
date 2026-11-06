#!/usr/bin/env python3
"""Crea la tabla "Prueba n8n" con datos FICTICIOS en Baserow y Teable (tarea 4).

Los flujos de n8n trabajan solo sobre esta tabla, nunca sobre los datos reales de CPP.
Se puede volver a correr: no duplica la tabla ni los registros.

Uso (CMD, desde la raiz del repo):
    python scripts\\create_test_tables.py

Imprime los IDs que necesitan los flujos.
"""
import json
import tempfile
from pathlib import Path

from import_baserow import Baserow
from import_common import FILES, FieldSpec, FileRef, read_env, run_main
from import_teable import Teable

TABLE = "Prueba n8n"
FIELDS = [
    FieldSpec("Name", "text"),
    FieldSpec("Estado", "single_select", options=["Pendiente", "Enviar", "Enviado"]),
    FieldSpec("Archivo", FILES),
]
RECORDS = ["PRUEBA 001", "PRUEBA 002", "PRUEBA 003"]


def fake_pdf(text):
    """PDF minimo valido, sin datos reales."""
    stream = f"BT /F1 18 Tf 72 720 Td ({text} - ARCHIVO FICTICIO) Tj ET".encode()
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R "
        b"/Resources << /Font << /F1 5 0 R >> >> >>",
        b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out = b"%PDF-1.4\n"
    offsets = []
    for number, body in enumerate(objects, 1):
        offsets.append(len(out))
        out += str(number).encode() + b" 0 obj\n" + body + b"\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode()
    for offset in offsets:
        out += f"{offset:010d} 00000 n \n".encode()
    out += f"trailer << /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    return out


def setup_baserow(folder):
    env = read_env("BASEROW_PUBLIC_URL", "BASEROW_EMAIL", "BASEROW_PASSWORD")
    client = Baserow(env["BASEROW_PUBLIC_URL"], env["BASEROW_EMAIL"], env["BASEROW_PASSWORD"])
    client.connect(False)
    database_id = client.database(client.workspace())
    table_id = client.table(database_id, TABLE, "Name")
    client.ensure_fields(table_id, FIELDS)
    existing = {row["Name"] for row in client.rows(table_id)}
    payloads = []
    for name in RECORDS:
        if name not in existing:
            ref = FileRef(f"{name}.pdf", f"{name}.pdf", name, 0)  # el nombre basta como llave de la cache
            payloads.append({"Name": name, "Estado": "Pendiente", "Archivo": [client.upload(ref, folder)]})
    if payloads:
        client.insert(table_id, payloads)
    fields = {field["name"]: field["id"] for field in client.http.request("GET", f"/api/database/fields/table/{table_id}/")}
    rows = {row["Name"]: row["id"] for row in client.rows(table_id)}
    return {"table_id": table_id, "database_id": database_id, "fields": fields, "rows": rows}


def setup_teable(folder):
    env = read_env("TEABLE_PUBLIC_ORIGIN", "TEABLE_EMAIL", "TEABLE_PASSWORD")
    client = Teable(env["TEABLE_PUBLIC_ORIGIN"], env["TEABLE_EMAIL"], env["TEABLE_PASSWORD"])
    client.connect(False)
    base_id = client.base(client.space())
    table_id = client.table(base_id, TABLE, "Name")
    fields = client.ensure_fields(table_id, FIELDS)
    existing = {record["fields"].get("Name") for record in client.records(table_id)}
    client.insert(table_id, [{"Name": name, "Estado": "Pendiente"} for name in RECORDS if name not in existing])
    rows = {}
    for record in client.records(table_id):
        name = record["fields"].get("Name")
        rows[name] = record["id"]
        if not record["fields"].get("Archivo"):
            path = f"/api/table/{table_id}/record/{record['id']}/{fields['Archivo']}/uploadAttachment"
            client.http.upload(path, Path(folder) / f"{name}.pdf", f"{name}.pdf")
    return {"table_id": table_id, "base_id": base_id, "fields": fields, "rows": rows}


def main():
    with tempfile.TemporaryDirectory() as folder:
        for name in RECORDS:
            (Path(folder) / f"{name}.pdf").write_bytes(fake_pdf(name))
        result = {"baserow": setup_baserow(Path(folder)), "teable": setup_teable(Path(folder))}
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    run_main(main)
