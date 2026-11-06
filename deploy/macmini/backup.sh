#!/bin/bash
# Respalda el volumen de Baserow en backups/baserow-<fecha>/ y conserva los ultimos BACKUP_KEEP.
# Uso (desde esta carpeta):  ./backup.sh
# Detiene Baserow mientras copia (unos 2 minutos con los datos de CPP) para que la BD quede consistente.
# Compatible con el bash 3.2 de macOS.
set -euo pipefail
cd "$(dirname "$0")"

PROJECT="${COMPOSE_PROJECT_NAME:-baserow}"
VOLUME="${PROJECT}_baserow_data"
IMAGE="baserow/baserow:2.4.1"   # se usa como imagen auxiliar: ya trae tar y sha256sum
KEEP="$(grep -E '^BACKUP_KEEP=' .env 2>/dev/null | cut -d= -f2 | tr -d '\r')"
KEEP="${KEEP:-14}"

docker volume inspect "$VOLUME" >/dev/null 2>&1 || { echo "ERROR: no existe el volumen $VOLUME"; exit 1; }

STAMP="$(date +%Y%m%d-%H%M%S)"
mkdir -p backups
DEST="$(cd backups && pwd)/baserow-$STAMP"
mkdir "$DEST"

restart() { docker compose start baserow >/dev/null; }
echo "Deteniendo Baserow..."
docker compose stop -t 60 baserow
trap restart EXIT   # vuelve a arrancar aunque falle la copia

echo "Copiando el volumen..."
docker run --rm --entrypoint sh -v "$VOLUME":/data:ro -v "$DEST":/backup "$IMAGE" \
  -c 'tar czf /backup/baserow_data.tar.gz --numeric-owner -C /data . && cd /backup && sha256sum baserow_data.tar.gz > SHA256SUMS'

restart
trap - EXIT
echo "Respaldo listo: $DEST"

# Retencion: borra los respaldos mas viejos, solo carpetas creadas por este script.
# (head -n -N no existe en macOS, por eso awk)
ls -1d backups/baserow-* 2>/dev/null | sort | awk -v keep="$KEEP" '{ all[NR] = $0 } END { for (i = 1; i <= NR - keep; i++) print all[i] }' | while read -r old; do
  echo "Borrando respaldo viejo: $old"
  rm -rf "$old"
done
