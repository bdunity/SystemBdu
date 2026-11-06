#!/bin/bash
# Restaura Baserow desde una carpeta de respaldo (de backup.sh o de scripts\backup.cmd de la PoC).
# Uso (desde esta carpeta):  ./restore.sh backups/baserow-20261106-104523
# Por seguridad NO borra nada: si el volumen ya existe, se detiene.
# Compatible con el bash 3.2 de macOS.
set -euo pipefail
cd "$(dirname "$0")"

[ $# -eq 1 ] || { echo "Uso: ./restore.sh carpeta_del_respaldo"; exit 1; }
[ -f "$1/SHA256SUMS" ] || { echo "ERROR: $1 no parece un respaldo; falta SHA256SUMS"; exit 1; }
SOURCE="$(cd "$1" && pwd)"

PROJECT="${COMPOSE_PROJECT_NAME:-baserow}"
VOLUME="${PROJECT}_baserow_data"
IMAGE="baserow/baserow:2.4.1"

if docker volume inspect "$VOLUME" >/dev/null 2>&1; then
  echo "ERROR: el volumen $VOLUME ya existe. Para reemplazarlo, primero (borra los datos actuales):"
  echo "  docker compose down -v"
  exit 1
fi

echo "Verificando el respaldo..."
docker run --rm --entrypoint sh -v "$SOURCE":/backup:ro "$IMAGE" -c 'cd /backup && sha256sum -c SHA256SUMS'

echo "Creando contenedores y volumen vacio..."
docker compose create

echo "Restaurando..."
docker run --rm --entrypoint sh -v "$VOLUME":/data -v "$SOURCE":/backup:ro "$IMAGE" \
  -c 'tar xzf /backup/baserow_data.tar.gz --numeric-owner -C /data'

echo "Iniciando..."
docker compose up -d
echo "Listo. Espera a que baserow este 'healthy' (unos 2 minutos):  docker compose ps"
