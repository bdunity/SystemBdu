@echo off
rem Respalda los volumenes de una herramienta de la PoC en backups\<herramienta>-<fecha>\
rem Uso:  scripts\backup.cmd baserow    o    scripts\backup.cmd teable
rem Detiene la herramienta mientras copia, para que la base de datos quede consistente.
setlocal EnableDelayedExpansion
cd /d "%~dp0.."

set "TOOL=%~1"
if /i "%TOOL%"=="baserow" (
    set "VOLUMES=baserow_data"
) else if /i "%TOOL%"=="teable" (
    set "VOLUMES=teable-db teable-data teable-cache"
) else (
    echo Uso: scripts\backup.cmd baserow^|teable
    exit /b 1
)
set "PROJECT=monday-poc-%TOOL%"
set "COMPOSE=docker compose --env-file .env -f infra\%TOOL%\docker-compose.yml"
rem Imagen auxiliar con tar y sha256sum; ya esta descargada por Teable.
set "HELPER=postgres:15.4"

for /f %%i in ('powershell -NoProfile -Command "Get-Date -Format yyyyMMdd-HHmmss"') do set "STAMP=%%i"
set "DEST=%cd%\backups\%TOOL%-%STAMP%"
mkdir "%DEST%" || exit /b 1

for %%v in (%VOLUMES%) do (
    docker volume inspect %PROJECT%_%%v >nul 2>&1 || (
        echo ERROR: no existe el volumen %PROJECT%_%%v
        exit /b 1
    )
)

echo Deteniendo %TOOL%...
%COMPOSE% stop -t 60 || exit /b 1

set "FAILED="
for %%v in (%VOLUMES%) do (
    echo Copiando %%v...
    docker run --rm -v %PROJECT%_%%v:/data:ro -v "%DEST%":/backup %HELPER% tar czf /backup/%%v.tar.gz --numeric-owner -C /data . || set "FAILED=1"
)

echo Iniciando %TOOL%...
%COMPOSE% start

if defined FAILED (
    echo ERROR: el respaldo fallo; revisa %DEST%
    exit /b 1
)
docker run --rm -v "%DEST%":/backup -w /backup %HELPER% sh -c "sha256sum *.tar.gz > SHA256SUMS" || exit /b 1
echo Respaldo listo: %DEST%
exit /b 0
