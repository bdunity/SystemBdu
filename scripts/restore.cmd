@echo off
rem Restaura una herramienta de la PoC desde una carpeta creada por backup.cmd.
rem Uso:  scripts\restore.cmd baserow backups\baserow-20261106-120000
rem Por seguridad NO borra nada: si los volumenes existen, primero hay que bajar la
rem herramienta con "down -v" a proposito (ver scripts\backup_restore.md).
setlocal EnableDelayedExpansion
cd /d "%~dp0.."

set "TOOL=%~1"
set "SOURCE=%~f2"
if /i "%TOOL%"=="baserow" (
    set "VOLUMES=baserow_data"
) else if /i "%TOOL%"=="teable" (
    set "VOLUMES=teable-db teable-data teable-cache"
) else (
    echo Uso: scripts\restore.cmd baserow^|teable carpeta_del_respaldo
    exit /b 1
)
if "%~2"=="" (
    echo Falta la carpeta del respaldo.
    exit /b 1
)
if not exist "%SOURCE%\SHA256SUMS" (
    echo ERROR: %SOURCE% no parece un respaldo; falta SHA256SUMS
    exit /b 1
)
set "PROJECT=monday-poc-%TOOL%"
set "COMPOSE=docker compose --env-file .env -f infra\%TOOL%\docker-compose.yml"
set "HELPER=postgres:15.4"

for %%v in (%VOLUMES%) do (
    docker volume inspect %PROJECT%_%%v >nul 2>&1 && (
        echo ERROR: el volumen %PROJECT%_%%v ya existe. Para reemplazarlo, primero:
        echo   %COMPOSE% down -v
        exit /b 1
    )
)

echo Verificando el respaldo...
docker run --rm -v "%SOURCE%":/backup:ro -w /backup %HELPER% sha256sum -c SHA256SUMS || exit /b 1

echo Creando contenedores y volumenes vacios...
%COMPOSE% create || exit /b 1

for %%v in (%VOLUMES%) do (
    echo Restaurando %%v...
    docker run --rm -v %PROJECT%_%%v:/data -v "%SOURCE%":/backup:ro %HELPER% tar xzf /backup/%%v.tar.gz --numeric-owner -C /data || exit /b 1
)

echo Iniciando %TOOL%...
%COMPOSE% up -d || exit /b 1
echo Restauracion lista. Espera a que el estado sea "healthy":
echo   %COMPOSE% ps
exit /b 0
