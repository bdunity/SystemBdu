#!/usr/bin/env python3
"""Exporta un board de Monday a JSON y descarga sus archivos. SOLO LECTURA.

Uso (CMD, desde la raiz del repo):
    python scripts\\export_monday_board.py 18393961251
    python scripts\\export_monday_board.py 18393961251 --max-items 5 --out export-prueba

Genera en <out>/<board_id>/:
    board.json       columnas y grupos del board y de sus subitems
    items.json       items, valores, subitems, updates y replies
    <item_id>/...    archivos del item, sus subitems y sus updates
    manifest.json    conteos, SHA-256 por archivo y diferencias encontradas

El token se toma de MONDAY_API_TOKEN (variable de entorno o .env en la raiz).
Se puede volver a correr: los archivos ya descargados y completos no se bajan de nuevo.
Codigo de salida: 0 sin diferencias, 2 con diferencias, 1 error fatal.

La consola solo muestra conteos e IDs, nunca valores: el board tiene datos bancarios.
"""
import argparse
import hashlib
import json
import os
import random
import re
import sys
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

API_URL = "https://api.monday.com/v2"
API_VERSION = "2026-10"
USER_AGENT = "monday-poc-export/1.0"
MAX_ATTEMPTS = 8
MAX_RATE_LIMIT_WAITS = 30
UPDATES_PAGE_SIZE = 100

ASSET_FIELDS = "id name file_size file_extension url public_url created_at"
UPDATE_FIELDS = f"""
  id body text_body created_at updated_at
  creator {{ id name email }}
  assets {{ {ASSET_FIELDS} }}
  replies {{ id body text_body created_at creator {{ id name email }} assets {{ {ASSET_FIELDS} }} }}
"""
ITEM_FIELDS = f"""
  id name state created_at updated_at
  creator {{ id name email }}
  group {{ id title }}
  column_values {{
    id type text value
    ... on BoardRelationValue {{ display_value linked_item_ids }}
  }}
  assets {{ {ASSET_FIELDS} }}
  updates(limit: {UPDATES_PAGE_SIZE}) {{ {UPDATE_FIELDS} }}
"""
PAGE_FIELDS = f"""
  cursor
  items {{
    {ITEM_FIELDS}
    subitems {{ {ITEM_FIELDS} board {{ id }} }}
  }}
"""

BOARD_QUERY = """
query ($ids: [ID!]) {
  boards(ids: $ids) {
    id name description state board_kind items_count updated_at
    workspace { id name }
    groups { id title color position archived deleted }
    columns { id title type description archived settings_str }
  }
}"""
FIRST_PAGE_QUERY = f"""
query ($ids: [ID!], $limit: Int!) {{
  boards(ids: $ids) {{ items_page(limit: $limit) {{ {PAGE_FIELDS} }} }}
}}"""
NEXT_PAGE_QUERY = f"""
query ($cursor: String!, $limit: Int!) {{
  next_items_page(cursor: $cursor, limit: $limit) {{ {PAGE_FIELDS} }}
}}"""
MORE_UPDATES_QUERY = f"""
query ($ids: [ID!], $limit: Int!, $page: Int!) {{
  items(ids: $ids) {{ updates(limit: $limit, page: $page) {{ {UPDATE_FIELDS} }} }}
}}"""
REFRESH_ASSET_QUERY = """
query ($ids: [ID!]!) { assets(ids: $ids) { id public_url } }"""


class FatalError(Exception):
    pass


class QueryTooComplex(Exception):
    pass


def load_token():
    token = os.environ.get("MONDAY_API_TOKEN", "").strip()
    env_path = Path(__file__).resolve().parent.parent / ".env"
    if not token and env_path.exists():
        for line in env_path.read_text(encoding="utf-8-sig").splitlines():
            if line.startswith("MONDAY_API_TOKEN="):
                token = line.split("=", 1)[1].strip().strip("\"'")
    if not token:
        raise FatalError("Falta MONDAY_API_TOKEN (variable de entorno o .env).")
    return token


def backoff(attempt):
    return min(60, 2**attempt) + random.uniform(0, 1)


class MondayClient:
    def __init__(self, token):
        self._token = token

    def execute(self, query, variables=None):
        # Regla de la PoC: Monday es solo lectura.
        if not query.lstrip().startswith("query"):
            raise FatalError("Este script solo puede enviar consultas de lectura.")
        body = json.dumps({"query": query, "variables": variables or {}}).encode()
        attempts = 0
        rate_waits = 0
        while True:
            wait, reason = self._attempt(body)
            if reason is None:
                return wait  # en exito, _attempt devuelve los datos
            if reason == "limite":
                rate_waits += 1
                if rate_waits > MAX_RATE_LIMIT_WAITS:
                    raise FatalError("Monday sigue limitando las consultas tras muchas esperas.")
            else:
                attempts += 1
                if attempts >= MAX_ATTEMPTS:
                    raise FatalError(f"Monday no respondio tras {attempts} intentos ({reason}).")
                wait = backoff(attempts)
            print(f"  esperando {wait:.0f}s ({reason})", flush=True)
            time.sleep(wait)

    def _attempt(self, body):
        """Devuelve (datos, None) en exito o (segundos, motivo) para reintentar."""
        request = urllib.request.Request(
            API_URL,
            data=body,
            headers={
                "Authorization": self._token,
                "Content-Type": "application/json",
                "API-Version": API_VERSION,
                "User-Agent": USER_AGENT,
            },
        )
        retry_after = None
        try:
            with urllib.request.urlopen(request, timeout=120) as response:
                raw = response.read()
        except urllib.error.HTTPError as error:
            raw = error.read()
            retry_after = error.headers.get("Retry-After")
            if error.code in (401, 403):
                raise FatalError(f"Monday rechazo el token (HTTP {error.code}).") from None
            if error.code >= 500:
                return 0, f"HTTP {error.code}"
            if error.code == 429 and not raw:
                return float(retry_after or 60), "limite"
        except OSError as error:  # URLError, timeouts y conexiones cortadas
            return 0, type(error).__name__

        try:
            payload = json.loads(raw)
        except ValueError:
            return 0, "respuesta no es JSON"
        errors = payload.get("errors") or []
        if payload.get("error_code") or payload.get("error_message"):
            errors = errors + [
                {
                    "message": payload.get("error_message", ""),
                    "extensions": {"code": payload.get("error_code")},
                }
            ]
        if not errors:
            return payload["data"], None
        return self._classify(errors, retry_after)

    @staticmethod
    def _classify(errors, retry_after):
        for error in errors:
            extensions = error.get("extensions") or {}
            code = str(extensions.get("code") or "")
            message = str(error.get("message") or "")
            if code in ("REQUEST_MAX_COMPLEXITY_EXCEEDED", "maxComplexityExceeded"):
                raise QueryTooComplex()
            if code == "DAILY_LIMIT_EXCEEDED":
                raise FatalError("Se agoto el limite diario de Monday; se reinicia a medianoche UTC.")
            if code in ("COMPLEXITY_BUDGET_EXHAUSTED", "ComplexityException", "RATE_LIMIT_EXCEEDED") or re.search(
                r"rate limit|concurrency limit|complexity budget", message, re.I
            ):
                wait = extensions.get("retry_in_seconds") or retry_after or 60
                return float(wait) + 1, "limite"
            if code in ("INTERNAL_SERVER_ERROR", "API_TEMPORARILY_BLOCKED"):
                return 0, code
        summary = "; ".join(str(e.get("message"))[:200] for e in errors[:3])
        raise FatalError(f"Error de la API de Monday: {summary}")


def fetch_pages(client, board_id, page_size):
    """Genera paginas de items, reduciendo el tamano si la consulta es muy compleja."""
    cursor = None
    first = True
    while first or cursor:
        try:
            if first:
                data = client.execute(FIRST_PAGE_QUERY, {"ids": [board_id], "limit": page_size})
                boards = data["boards"]
                if not boards:
                    raise FatalError(f"No existe el board {board_id} o el token no tiene acceso.")
                page = boards[0]["items_page"]
            else:
                page = client.execute(NEXT_PAGE_QUERY, {"cursor": cursor, "limit": page_size})["next_items_page"]
        except QueryTooComplex:
            if page_size == 1:
                raise FatalError("La consulta excede el limite de complejidad incluso con 1 item.") from None
            page_size = max(1, page_size // 2)
            print(f"  consulta muy compleja, paginas de {page_size} items", flush=True)
            continue
        first = False
        cursor = page.get("cursor")
        yield page["items"]


def complete_updates(client, item):
    """Trae las updates restantes si el item llego al tope de la primera pagina."""
    updates = item.get("updates") or []
    page = 2
    last_size = len(updates)
    while last_size == UPDATES_PAGE_SIZE:
        data = client.execute(
            MORE_UPDATES_QUERY, {"ids": [item["id"]], "limit": UPDATES_PAGE_SIZE, "page": page}
        )
        more = data["items"][0]["updates"] if data["items"] else []
        updates.extend(more)
        last_size = len(more)
        page += 1
    item["updates"] = updates


WINDOWS_RESERVED = {"CON", "PRN", "AUX", "NUL"} | {f"COM{i}" for i in range(1, 10)} | {f"LPT{i}" for i in range(1, 10)}


def safe_filename(asset):
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", str(asset.get("name") or "")).strip().rstrip(". ")
    extension = str(asset.get("file_extension") or "")
    if not name:
        name = str(asset["id"])
    if extension and not name.lower().endswith(extension.lower()):
        name += extension
    stem, suffix = os.path.splitext(name)
    if stem.upper() in WINDOWS_RESERVED:
        stem = "_" + stem
    return stem[:150] + suffix[:30]


def long_path(path):
    """Evita el limite de 260 caracteres de Windows."""
    resolved = os.path.abspath(path)
    if os.name == "nt" and not resolved.startswith("\\\\?\\"):
        return "\\\\?\\" + resolved
    return resolved


def sha256_of(path):
    digest = hashlib.sha256()
    with open(long_path(path), "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def iter_owned_assets(item, parent_id=None):
    """Genera (dueno, item padre, origen, asset) del item, sus updates y sus subitems."""
    owner = item["id"]
    for asset in item.get("assets") or []:
        yield owner, parent_id, "subitem" if parent_id else "item", asset
    for update in item.get("updates") or []:
        for asset in update.get("assets") or []:
            yield owner, parent_id, f"update:{update['id']}", asset
        for reply in update.get("replies") or []:
            for asset in reply.get("assets") or []:
                yield owner, parent_id, f"reply:{reply['id']}", asset
    for subitem in item.get("subitems") or []:
        yield from iter_owned_assets(subitem, item["id"])


class Downloader:
    def __init__(self, client, board_dir):
        self._client = client
        self._board_dir = Path(board_dir)
        self._seen = {}  # (dueno, asset) -> ya planeado
        self._names = {}  # dueno -> nombres en uso

    def plan(self, items):
        """Asigna a cada asset una ruta unica, sin repetir el mismo archivo por dueno."""
        jobs = []
        for item in items:
            owned = sorted(iter_owned_assets(item), key=lambda entry: int(entry[3]["id"]))
            for owner, parent_id, source, asset in owned:
                key = (owner, asset["id"])
                if key in self._seen:
                    continue
                self._seen[key] = True
                name = safe_filename(asset)
                used = self._names.setdefault(owner, set())
                if name.casefold() in used:
                    stem, suffix = os.path.splitext(name)
                    name = f"{stem}__{asset['id']}{suffix}"
                used.add(name.casefold())
                jobs.append(
                    {
                        "owner": owner,
                        "parent_item_id": parent_id,
                        "source": source,
                        "asset": asset,
                        "relative": f"{owner}/{name}",
                    }
                )
        return jobs

    def run(self, job):
        asset = job["asset"]
        destination = self._board_dir / job["relative"]
        expected = asset.get("file_size")
        record = {
            "path": job["relative"],
            "asset_id": asset["id"],
            "item_id": job["parent_item_id"] or job["owner"],
            "subitem_id": job["owner"] if job["parent_item_id"] else None,
            "source": job["source"],
            "name": asset.get("name"),
            "api_file_size": expected,
        }
        try:
            os.makedirs(long_path(destination.parent), exist_ok=True)
            reused = os.path.exists(long_path(destination)) and (
                expected is None or os.path.getsize(long_path(destination)) == expected
            )
            if not reused:
                self._download(asset, destination)
            record["size"] = os.path.getsize(long_path(destination))
            record["sha256"] = sha256_of(destination)
            record["reused"] = reused
        except Exception as error:  # se reporta en el manifiesto y no detiene el export
            record["error"] = f"{type(error).__name__}: {str(error)[:200]}"
        return record

    def _download(self, asset, destination):
        url = asset.get("public_url")
        temporary = str(destination) + ".part"
        last_error = "sin public_url"
        for attempt in range(1, MAX_ATTEMPTS + 1):
            if not url:
                url = self._refresh_url(asset["id"])
            if url:
                try:
                    # Sin Authorization: la URL firmada no debe recibir el token.
                    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
                    with urllib.request.urlopen(request, timeout=300) as response, open(
                        long_path(temporary), "wb"
                    ) as handle:
                        while chunk := response.read(1024 * 1024):
                            handle.write(chunk)
                    os.replace(long_path(temporary), long_path(destination))
                    return
                except urllib.error.HTTPError as error:
                    last_error = f"HTTP {error.code}"
                    if error.code in (400, 401, 403, 404):
                        url = None  # URL caducada: pedir una nueva
                except OSError as error:
                    last_error = type(error).__name__
            time.sleep(backoff(attempt))
        if os.path.exists(long_path(temporary)):
            os.remove(long_path(temporary))
        raise RuntimeError(f"no se pudo descargar: {last_error}")

    def _refresh_url(self, asset_id):
        data = self._client.execute(REFRESH_ASSET_QUERY, {"ids": [asset_id]})
        assets = data.get("assets") or []
        return assets[0].get("public_url") if assets else None


def strip_signed_urls(node):
    """public_url da acceso sin sesion durante 1 hora: no se guarda en disco."""
    if isinstance(node, dict):
        node.pop("public_url", None)
        for value in node.values():
            strip_signed_urls(value)
    elif isinstance(node, list):
        for value in node:
            strip_signed_urls(value)


def walk_items(items):
    for item in items:
        yield item
        yield from item.get("subitems") or []


def check_file_columns(items):
    """Cruza las columnas de archivos con los assets; separa los enlaces externos."""
    missing = []
    external = []
    for item in walk_items(items):
        asset_ids = {str(asset["id"]) for asset in item.get("assets") or []}
        for column in item.get("column_values") or []:
            if column.get("type") != "file" or not column.get("value"):
                continue
            for entry in json.loads(column["value"]).get("files") or []:
                if entry.get("assetId"):
                    if str(entry["assetId"]) not in asset_ids:
                        missing.append(
                            {"item_id": item["id"], "column_id": column["id"], "asset_id": str(entry["assetId"])}
                        )
                else:
                    external.append(
                        {"item_id": item["id"], "column_id": column["id"], "file_type": entry.get("fileType")}
                    )
    return missing, external


def subitem_board_ids(board):
    ids = set()
    for column in board.get("columns") or []:
        if column["type"] == "subtasks" and column.get("settings_str"):
            ids.update(str(i) for i in json.loads(column["settings_str"]).get("boardIds") or [])
    return sorted(ids)


def write_json(path, data):
    temporary = str(path) + ".tmp"
    with open(long_path(temporary), "w", encoding="utf-8") as handle:
        json.dump(data, handle, ensure_ascii=False, indent=2)
    os.replace(long_path(temporary), long_path(path))


def export_board(args):
    client = MondayClient(load_token())
    board_id = str(args.board_id)
    board_dir = Path(args.out) / board_id
    os.makedirs(long_path(board_dir), exist_ok=True)

    boards = client.execute(BOARD_QUERY, {"ids": [board_id]})["boards"]
    if not boards:
        raise FatalError(f"No existe el board {board_id} o el token no tiene acceso.")
    board = boards[0]
    sub_ids = subitem_board_ids(board)
    sub_boards = client.execute(BOARD_QUERY, {"ids": sub_ids})["boards"] if sub_ids else []
    write_json(board_dir / "board.json", {"board": board, "subitem_boards": sub_boards})
    print(f"Board {board_id}: {board.get('items_count')} items segun Monday", flush=True)

    downloader = Downloader(client, board_dir)
    items = []
    files = []
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        for page in fetch_pages(client, board_id, args.page_size):
            if args.max_items:
                page = page[: args.max_items - len(items)]
            for item in page:
                complete_updates(client, item)
                for subitem in item.get("subitems") or []:
                    complete_updates(client, subitem)
            # Se descarga antes de pedir otra pagina porque public_url caduca en 1 hora.
            files.extend(pool.map(downloader.run, downloader.plan(page)))
            strip_signed_urls(page)
            items.extend(page)
            failed = sum(1 for record in files if "error" in record)
            print(f"  items {len(items)} | archivos {len(files)} | fallidos {failed}", flush=True)
            if args.max_items and len(items) >= args.max_items:
                break

    write_json(board_dir / "items.json", items)

    all_items = list(walk_items(items))
    updates = [update for item in all_items for update in item.get("updates") or []]
    replies = sum(len(update.get("replies") or []) for update in updates)
    missing, external = check_file_columns(items)
    failed = [record for record in files if "error" in record]
    ok_files = [record for record in files if "error" not in record]
    size_mismatch = [
        record["path"]
        for record in ok_files
        if record["api_file_size"] is not None and record["api_file_size"] != record["size"]
    ]
    partial = bool(args.max_items)
    item_diff = None if partial else len(items) - int(board.get("items_count") or 0)
    manifest = {
        "board_id": board_id,
        "board_name": board.get("name"),
        "exported_at": datetime.now(timezone.utc).isoformat(),
        "api_version": API_VERSION,
        "partial": partial,
        "counts": {
            "items_reported_by_monday": board.get("items_count"),
            "items": len(items),
            "subitems": len(all_items) - len(items),
            "updates": len(updates),
            "replies": replies,
            "files_ok": len(ok_files),
            "files_failed": len(failed),
            "bytes": sum(record["size"] for record in ok_files),
            "external_links": len(external),
        },
        "differences": {
            "items_vs_monday": item_diff,
            "failed_files": failed,
            "size_mismatches": size_mismatch,
            "file_column_assets_missing": missing,
        },
        "external_links": external,
        "files": files,
    }
    write_json(board_dir / "manifest.json", manifest)

    counts = manifest["counts"]
    print("Resumen:", json.dumps(counts))
    has_differences = bool(item_diff or failed or size_mismatch or missing)
    if has_differences:
        print(
            f"DIFERENCIAS: items {item_diff}, fallidos {len(failed)}, "
            f"tamano {len(size_mismatch)}, columnas sin archivo {len(missing)}. Ver manifest.json"
        )
    else:
        print("Sin diferencias.")
    return 2 if has_differences else 0


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description="Exporta un board de Monday (solo lectura).")
    parser.add_argument("board_id", type=int)
    parser.add_argument("--out", default="export", help="carpeta de salida (default: export)")
    parser.add_argument("--page-size", type=int, default=100, help="items por pagina (max 500)")
    parser.add_argument("--workers", type=int, default=4, help="descargas simultaneas")
    parser.add_argument("--max-items", type=int, default=0, help="exportar solo N items, para pruebas")
    args = parser.parse_args()
    try:
        return export_board(args)
    except FatalError as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
