"""Mapeo y validacion compartidos por import_baserow.py e import_teable.py.

Convierte el export de Monday a un modelo neutral y valida el destino contra el
export y manifest.json, con las mismas reglas para ambas herramientas. La consola
solo muestra conteos, IDs y nombres de campo, nunca valores: hay datos bancarios.
"""
import argparse
import hashlib
import http.cookiejar
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
MONDAY_ID = "monday_id"
ITEMS_TABLE = "Cuentas por pagar 2026"
COMMENTS_TABLE = "Comentarios"
ITEM_LINK = "Item"
FILES = "files"


class FatalError(Exception):
    pass


def read_env(*names):
    """Lee variables del entorno o del .env de la raiz; falla si falta alguna."""
    values = {}
    env_path = REPO_ROOT / ".env"
    if env_path.exists():
        for line in env_path.read_text(encoding="utf-8-sig").splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                key, value = line.split("=", 1)
                values[key.strip()] = value.strip().strip("\"'")
    result = {name: os.environ.get(name) or values.get(name) for name in names}
    missing = [name for name, value in result.items() if not value]
    if missing:
        raise FatalError(f"Faltan variables en .env: {', '.join(missing)}")
    return result


def long_path(path):
    resolved = os.path.abspath(path)
    if os.name == "nt" and not resolved.startswith("\\\\?\\"):
        return "\\\\?\\" + resolved
    return resolved


# --- Cliente HTTP -----------------------------------------------------------------


class ApiError(FatalError):
    def __init__(self, status, message):
        super().__init__(f"HTTP {status}: {message[:400]}")
        self.status = status


class HttpClient:
    """Cliente con cookies, reintentos y JSON/multipart, solo con libreria estandar."""

    def __init__(self, base_url):
        self.base_url = base_url.rstrip("/")
        self.headers = {}
        self.on_unauthorized = None
        self._opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar())
        )

    @staticmethod
    def _target(url, headers):
        """Python en Windows prueba ::1 primero y pierde 2 s por peticion: va directo a IPv4."""
        parts = urllib.parse.urlsplit(url)
        if parts.hostname != "localhost":
            return url, headers
        return (
            urllib.parse.urlunsplit(parts._replace(netloc=parts.netloc.replace("localhost", "127.0.0.1", 1))),
            {**headers, "Host": parts.netloc},
        )

    def request(self, method, path, payload=None, query=None, raw_body=None, content_type=None, retries=6, reauth=True):
        url = path if path.startswith("http") else self.base_url + path
        if query:
            url += "?" + urllib.parse.urlencode(query)
        data = raw_body
        headers = dict(self.headers)
        if payload is not None:
            data = json.dumps(payload).encode()
            headers["Content-Type"] = "application/json"
        elif content_type:
            headers["Content-Type"] = content_type
        refreshed = False
        for attempt in range(1, retries + 1):
            target, target_headers = self._target(url, headers)
            request = urllib.request.Request(target, data=data, method=method, headers=target_headers)
            try:
                with self._opener.open(request, timeout=300) as response:
                    body = response.read()
                return json.loads(body) if body else None
            except urllib.error.HTTPError as error:
                detail = error.read().decode("utf-8", "replace")
                if error.code == 401 and reauth and self.on_unauthorized and not refreshed:
                    self.on_unauthorized()
                    headers.update(self.headers)
                    refreshed = True
                    continue
                # 409 de Baserow por tabla bloqueada es temporal: se reintenta.
                locked = error.code == 409 and "FAILED_TO_LOCK" in detail
                if (error.code not in (408, 429, 500, 502, 503, 504) and not locked) or attempt == retries:
                    raise ApiError(error.code, detail) from None
            except OSError as error:
                if attempt == retries:
                    raise FatalError(f"Sin conexion con {self.base_url}: {type(error).__name__}") from None
            time.sleep(min(30, 2**attempt))
        raise FatalError("Reintentos agotados")

    def upload(self, path, file_path, filename, field_name="file"):
        boundary = uuid.uuid4().hex
        safe_name = filename.replace('"', "%22").replace("\r", "").replace("\n", "")
        head = (
            f'--{boundary}\r\nContent-Disposition: form-data; name="{field_name}"; filename="{safe_name}"\r\n'
            "Content-Type: application/octet-stream\r\n\r\n"
        ).encode()
        with open(long_path(file_path), "rb") as handle:
            body = head + handle.read() + f"\r\n--{boundary}--\r\n".encode()
        return self.request("POST", path, raw_body=body, content_type=f"multipart/form-data; boundary={boundary}")

    def download_hash(self, url):
        """Baja un archivo desde el destino y devuelve (sha256, bytes)."""
        target = url if url.startswith("http") else self.base_url + url
        target, target_headers = self._target(target, self.headers)
        request = urllib.request.Request(target, headers=target_headers)
        digest = hashlib.sha256()
        size = 0
        for attempt in range(1, 5):
            try:
                with self._opener.open(request, timeout=300) as response:
                    while chunk := response.read(1024 * 1024):
                        digest.update(chunk)
                        size += len(chunk)
                return digest.hexdigest(), size
            except OSError:
                digest = hashlib.sha256()
                size = 0
                if attempt == 4:
                    raise
                time.sleep(2**attempt)


# --- Modelo neutral ---------------------------------------------------------------


@dataclass
class FieldSpec:
    name: str
    kind: str  # text, long_text, number, date, datetime, checkbox, single_select, files, link
    options: list = field(default_factory=list)
    decimals: int = 0


@dataclass
class FileRef:
    path: str
    name: str
    sha256: str
    size: int


@dataclass
class Row:
    monday_id: str
    values: dict
    files: dict


@dataclass
class Model:
    board_dir: Path
    item_fields: list
    comment_fields: list
    items: list
    comments: list
    skipped_columns: list
    problems: list
    manifest: dict


def _parse_value(column):
    raw = column.get("value")
    if raw in (None, "", "null"):
        return None
    return json.loads(raw)


def _status_labels(column):
    settings = json.loads(column.get("settings_str") or "{}")
    labels = settings.get("labels") or {}
    if isinstance(labels, dict):  # status: {"indice": "etiqueta"}
        return [labels[key] for key in sorted(labels, key=int) if labels[key]]
    return [label["name"] for label in labels if label.get("name")]  # dropdown


def to_utc_iso(value):
    if not value:
        return None
    text = str(value).strip().replace(" UTC", "+00:00").replace("Z", "+00:00")
    parsed = datetime.fromisoformat(text.replace(" ", "T", 1))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def load_model(board_id, export_dir="export", max_items=0):
    board_dir = Path(export_dir) / str(board_id)
    try:
        board = json.loads((board_dir / "board.json").read_text(encoding="utf-8"))["board"]
        raw_items = json.loads((board_dir / "items.json").read_text(encoding="utf-8"))
        manifest = json.loads((board_dir / "manifest.json").read_text(encoding="utf-8"))
    except FileNotFoundError as error:
        raise FatalError(f"No existe el export: {error.filename}") from None
    if manifest.get("partial"):
        print("AVISO: el export es parcial (--max-items).")
    if max_items:
        raw_items = raw_items[:max_items]

    files_by_key = {(entry["item_id"], str(entry["asset_id"])): entry for entry in manifest["files"] if "error" not in entry}
    problems = []

    def file_ref(item_id, asset_id):
        entry = files_by_key.get((item_id, str(asset_id)))
        if not entry:
            problems.append(f"item {item_id}: asset {asset_id} no esta en manifest.json")
            return None
        return FileRef(entry["path"], entry["name"], entry["sha256"], entry["size"])

    # Columnas -> campos
    item_fields = []
    columns_used = []
    skipped = []
    filled = Counter()
    for item in raw_items:
        for column in item["column_values"]:
            if column.get("value") not in (None, "", "null") or column.get("text"):
                filled[column["id"]] += 1
    kind_by_type = {
        "text": "text",
        "email": "text",  # texto en ambas: no se pierde nada si Monday trae un correo invalido
        "long_text": "long_text",
        "numbers": "number",
        "date": "date",
        "creation_log": "datetime",
        "checkbox": "checkbox",
        "status": "single_select",
        "dropdown": "single_select",
        "file": FILES,
    }
    numbers = [
        str(_parse_value(column))
        for item in raw_items
        for column in item["column_values"]
        if column["type"] == "numbers" and _parse_value(column) is not None
    ]
    decimals = max((len(n.split(".")[1]) if "." in n else 0 for n in numbers), default=0)
    titles = Counter(column["title"] for column in board["columns"])
    for column in board["columns"]:
        if column["type"] == "name":
            continue
        kind = kind_by_type.get(column["type"])
        if not kind:
            skipped.append({"id": column["id"], "type": column["type"], "filled": filled[column["id"]]})
            if filled[column["id"]]:
                problems.append(f"columna {column['id']} ({column['type']}) tiene datos y no se importa")
            continue
        name = column["title"] if titles[column["title"]] == 1 else f"{column['title']} ({column['id']})"
        spec = FieldSpec(name, kind)
        if kind == "single_select":
            spec.options = _status_labels(column)
        if kind == "number":
            spec.decimals = decimals
        item_fields.append(spec)
        columns_used.append((column, spec))
    specs = {spec.name: spec for spec in item_fields}
    group_spec = FieldSpec("Grupo", "single_select", options=[g["title"] for g in board["groups"]])
    item_fields = [
        FieldSpec("Name", "text"),
        FieldSpec(MONDAY_ID, "text"),
        group_spec,
        FieldSpec("Creado por", "text"),
    ] + item_fields

    # Items y comentarios
    items = []
    comments = []
    for item in raw_items:
        if item.get("subitems"):
            problems.append(f"item {item['id']}: tiene subitems y no se importan")
        by_id = {column["id"]: column for column in item["column_values"]}
        values = {
            "Name": item["name"],
            MONDAY_ID: item["id"],
            "Grupo": item["group"]["title"],
            "Creado por": (item.get("creator") or {}).get("email"),
        }
        files = {}
        for column, spec in columns_used:
            current = by_id.get(column["id"]) or {}
            parsed = _parse_value(current)
            if spec.kind == FILES:
                refs = [file_ref(item["id"], entry["assetId"]) for entry in (parsed or {}).get(FILES) or [] if entry.get("assetId")]
                files[spec.name] = [ref for ref in refs if ref]
            elif column["type"] == "email":
                values[spec.name] = (parsed or {}).get("email")
            elif spec.kind == "number":
                values[spec.name] = None if parsed is None else str(parsed)
            elif spec.kind == "date":
                values[spec.name] = (parsed or {}).get("date")
            elif spec.kind == "datetime":
                values[spec.name] = to_utc_iso((parsed or {}).get("created_at") or item.get("created_at"))
            elif spec.kind == "checkbox":
                values[spec.name] = bool((parsed or {}).get("checked"))
            else:
                text = current.get("text") or None
                values[spec.name] = text
                if spec.kind == "single_select" and text and text not in spec.options:
                    spec.options.append(text)
        items.append(Row(item["id"], values, files))

        def add_comment(entry, kind, parent):
            refs = [file_ref(item["id"], asset["id"]) for asset in entry.get("assets") or []]
            comments.append(
                Row(
                    entry["id"],
                    {
                        MONDAY_ID: entry["id"],
                        ITEM_LINK: item["id"],
                        "Tipo": kind,
                        "Responde a": parent,
                        "Autor": (entry.get("creator") or {}).get("email"),
                        "Fecha": to_utc_iso(entry.get("created_at")),
                        "Texto": entry.get("text_body") or None,
                        "HTML": entry.get("body") or None,
                    },
                    {"Archivos": [ref for ref in refs if ref]},
                )
            )

        for update in item.get("updates") or []:
            add_comment(update, "update", None)
            for reply in update.get("replies") or []:
                add_comment(reply, "reply", update["id"])

    comment_fields = [
        FieldSpec(MONDAY_ID, "text"),
        FieldSpec(ITEM_LINK, "link"),
        FieldSpec("Tipo", "single_select", options=["update", "reply"]),
        FieldSpec("Responde a", "text"),
        FieldSpec("Autor", "text"),
        FieldSpec("Fecha", "datetime"),
        FieldSpec("Texto", "long_text"),
        FieldSpec("HTML", "long_text"),
        FieldSpec("Archivos", FILES),
    ]
    del specs
    return Model(board_dir, item_fields, comment_fields, items, comments, skipped, problems, manifest)


# --- Validacion -------------------------------------------------------------------


def normalize(kind, value, decimals=0):
    if kind == "checkbox":
        return bool(value)
    if value in (None, ""):
        return None
    if kind == "number":
        try:
            return Decimal(str(value)).quantize(Decimal(1).scaleb(-decimals))
        except InvalidOperation:
            return f"no numerico: {type(value).__name__}"
    if kind == "date":
        return str(value)[:10]
    if kind == "datetime":
        return to_utc_iso(value)
    return str(value)


def validate_rows(label, specs, expected, actual, client, workers=4):
    """Compara filas y archivos. `actual` = {monday_id: {values, files: {campo: [{name,url,size}]}}}."""
    differences = {
        "missing_rows": [],
        "extra_rows": [],
        "field_mismatches": [],
        "file_mismatches": [],
    }
    expected_ids = {row.monday_id for row in expected}
    differences["missing_rows"] = sorted(expected_ids - set(actual))
    differences["extra_rows"] = sorted(set(actual) - expected_ids)

    downloads = []
    whitespace_only = []
    for row in expected:
        current = actual.get(row.monday_id)
        if not current:
            continue
        for spec in specs:
            if spec.kind == FILES:
                downloads.append((row, spec, current["files"].get(spec.name) or []))
                continue
            want = normalize(spec.kind, row.values.get(spec.name), spec.decimals)
            have = normalize(spec.kind, current["values"].get(spec.name), spec.decimals)
            if want == have:
                continue
            entry = {"monday_id": row.monday_id, "field": spec.name}
            if isinstance(want, str) and isinstance(have, str) and want.strip() == have.strip():
                # La herramienta recorta espacios de los extremos: se reporta, no bloquea.
                whitespace_only.append(entry)
            else:
                differences["field_mismatches"].append(entry)

    def check_files(job):
        row, spec, stored = job
        want = Counter((ref.sha256, ref.size, ref.name) for ref in row.files.get(spec.name) or [])
        have = Counter()
        for entry in stored:
            try:
                sha, size = client.download_hash(entry["url"])
            except OSError as error:
                return {"monday_id": row.monday_id, "field": spec.name, "error": type(error).__name__}
            have[(sha, size, entry["name"])] += 1
        if want != have:
            want_content = Counter((sha, size) for (sha, size, _name), n in want.items() for _ in range(n))
            have_content = Counter((sha, size) for (sha, size, _name), n in have.items() for _ in range(n))
            return {
                "monday_id": row.monday_id,
                "field": spec.name,
                "expected": sum(want.values()),
                "stored": sum(have.values()),
                "only_name_differs": want_content == have_content,
            }
        return None

    with ThreadPoolExecutor(max_workers=workers) as pool:
        differences["file_mismatches"] = [result for result in pool.map(check_files, downloads) if result]
    files_checked = sum(len(job[2]) for job in downloads)
    print(
        f"  {label}: {len(actual)} filas (esperadas {len(expected)}), {files_checked} archivos descargados y comparados, "
        + ", ".join(f"{key}={len(value)}" for key, value in differences.items())
        + f", solo_espacios={len(whitespace_only)}"
    )
    return differences, files_checked, whitespace_only


def finish(tool, model, item_result, comment_result, seconds):
    """Compara contra manifest.json, escribe el reporte y devuelve el codigo de salida."""
    item_diff, item_files, item_spaces = item_result
    comment_diff, comment_files, comment_spaces = comment_result
    references = sum(len(refs) for row in model.items + model.comments for refs in row.files.values())
    unique_hashes = {ref.sha256 for row in model.items + model.comments for refs in row.files.values() for ref in refs}
    unique_assets = {ref.path for row in model.items + model.comments for refs in row.files.values() for ref in refs}
    full_run = len(model.items) == model.manifest["counts"]["items"]
    manifest_files = model.manifest["counts"]["files_ok"]
    manifest_gap = manifest_files - len(unique_assets) if full_run else None
    report = {
        "tool": tool,
        "finished_at": datetime.now(timezone.utc).isoformat(),
        "seconds": round(seconds, 1),
        "full_run": full_run,
        "counts": {
            "items": len(model.items),
            "comments": len(model.comments),
            "file_references": references,
            "unique_files": len(unique_assets),
            "unique_hashes": len(unique_hashes),
            "manifest_files": manifest_files,
            "files_verified_at_destination": item_files + comment_files,
        },
        "manifest_files_without_reference": manifest_gap,
        "skipped_columns": model.skipped_columns,
        "problems": model.problems,
        "items": item_diff,
        "comments": comment_diff,
        "whitespace_only": item_spaces + comment_spaces,
    }
    path = model.board_dir / f"import_report_{tool}.json"
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    bad = (
        sum(len(value) for value in item_diff.values())
        + sum(len(value) for value in comment_diff.values())
        + len(model.problems)
        + bool(manifest_gap)
        + (item_files + comment_files != references)
    )
    print("Resumen:", json.dumps(report["counts"]))
    print(f"Tiempo: {seconds:.0f}s. Reporte: {path}")
    if bad:
        print(f"DIFERENCIAS: {bad}. manifest sin referencia: {manifest_gap}. Ver el reporte.")
        return 2
    print(f"Sin diferencias. Campos que solo cambian en espacios de los extremos: {len(report['whitespace_only'])}.")
    return 0


def build_parser(description):
    parser = argparse.ArgumentParser(description=description)
    parser.add_argument("board_id", type=int)
    parser.add_argument("--export-dir", default="export")
    parser.add_argument("--max-items", type=int, default=0, help="importar solo N items, para pruebas")
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--create-account", action="store_true", help="crear la cuenta de .env si no existe")
    parser.add_argument("--validate-only", action="store_true", help="solo validar el destino")
    return parser


def run_main(main_function):
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    try:
        sys.exit(main_function())
    except FatalError as error:
        print(f"ERROR: {error}", file=sys.stderr)
        sys.exit(1)
