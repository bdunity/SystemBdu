# monday-poc

Prueba funcional para reemplazar Monday.com: Baserow vs Teable, con el board "Cuentas por pagar 2026". Las tareas y reglas están en [CLAUDE.md](CLAUDE.md).

## Requisitos

- Docker Desktop abierto. `docker version` debe mostrar la sección Server.
- Docker Desktop tiene 8 GB asignados. Teable pide un mínimo de 4 GB, así que conviene levantar una herramienta a la vez si la máquina se pone lenta.

## Configuración (una sola vez)

Todos los comandos se corren en CMD desde la raíz del repo.

```bat
copy .env.example .env
```

Abre `.env` y llena `TEABLE_POSTGRES_PASSWORD`, `TEABLE_REDIS_PASSWORD` y `TEABLE_SECRET_KEY`, cada uno con un valor distinto. Este comando genera un valor; córrelo tres veces:

```bat
powershell -NoProfile -Command "[guid]::NewGuid().ToString('N')"
```

## Baserow

| | |
|---|---|
| URL | http://localhost:8081 |
| Primer acceso | La pantalla de registro crea la cuenta; la primera cuenta queda como administrador |

Levantar (el primer arranque tarda unos minutos):

```bat
docker compose --env-file .env -f infra\baserow\docker-compose.yml up -d
```

Ver el estado:

```bat
docker compose --env-file .env -f infra\baserow\docker-compose.yml ps
```

Bajar, conservando los datos:

```bat
docker compose --env-file .env -f infra\baserow\docker-compose.yml down
```

## Teable

| | |
|---|---|
| URL | http://127.0.0.1:3000 |
| Primer acceso | La pantalla de registro crea la cuenta |

Levantar:

```bat
docker compose --env-file .env -f infra\teable\docker-compose.yml up -d
```

Ver el estado:

```bat
docker compose --env-file .env -f infra\teable\docker-compose.yml ps
```

Bajar, conservando los datos:

```bat
docker compose --env-file .env -f infra\teable\docker-compose.yml down
```

## Export e imports

Los scripts solo usan la librería estándar de Python y leen las variables de `.env`. El board CPP es `18393961251`.

Exportar desde Monday (solo lectura):

```bat
C:\Users\Fernando\AppData\Local\Microsoft\WindowsApps\python.exe scripts\export_monday_board.py 18393961251
```

Importar a Baserow y validar. La primera vez, agrega `--create-account` para crear la cuenta de `.env`:

```bat
C:\Users\Fernando\AppData\Local\Microsoft\WindowsApps\python.exe scripts\import_baserow.py 18393961251
```

Importar a Teable y validar, con la misma opción para la primera vez:

```bat
C:\Users\Fernando\AppData\Local\Microsoft\WindowsApps\python.exe scripts\import_teable.py 18393961251
```

- **Para entrar a la UI:** usa `BASEROW_EMAIL`/`BASEROW_PASSWORD` y `TEABLE_EMAIL`/`TEABLE_PASSWORD` de `.env`.
- **Volver a correr:** es seguro; no duplica filas ni vuelve a subir archivos. Con `--validate-only` solo compara.
- **Validación:** descarga cada archivo desde el destino, compara su SHA-256 con `manifest.json` y escribe `export/<board_id>/import_report_<herramienta>.json`. Termina con código 2 si hay diferencias.
- **Espacios:** Baserow y Teable recortan los espacios de los extremos del texto. Se cuentan en el reporte como `whitespace_only` y no bloquean la validación.
- **Datos sensibles:** `export/` y las dos herramientas tienen datos bancarios y facturas reales. Bórralos al terminar la PoC.

## Pruebas de funciones

`scriptseature_checks.py` prueba en las dos herramientas el formulario con adjunto obligatorio, el kanban por Estado, el filtro por responsable, la papelera y deshacer. Los resultados están en [RESULTADOS.md](RESULTADOS.md).

## Respaldo y restauración

Usa `scripts\backup.cmd baserow|teable` y `scripts\restore.cmd`. El procedimiento probado está en [scripts/backup_restore.md](scripts/backup_restore.md).

## Borrar los datos de la PoC

Agregar `-v` a `down` **borra los volúmenes de esa herramienta**. Úsalo solo si quieres empezar de cero:

```bat
docker compose --env-file .env -f infra\baserow\docker-compose.yml down -v
```

Cada compose tiene su propio nombre de proyecto (`monday-poc-baserow`, `monday-poc-teable`), así que `down -v` no afecta a otros contenedores ni a un volumen `baserow_data` creado con `docker run`.

## Decisiones de esta PoC

**Baserow usa el puerto 8081, no el 8080.** El 8080 ya lo ocupa un contenedor `baserow` creado aparte con `docker run`. Si lo quitas y prefieres 8080, cambia `BASEROW_PORT` y `BASEROW_PUBLIC_URL` en `.env`. Ojo: `nosql_mongo_express` (proyecto `dockermongo`, hoy detenido) también usa el 8081, así que no pueden correr al mismo tiempo.

**Primer arranque de Baserow:** tardó unos 4 minutos en esta máquina. Mientras aplica migraciones es normal ver respuestas 502/500, estado `unhealthy` y errores de tareas programadas en el log (corren antes de que existan sus tablas).

**Primer arranque de Teable:** con `up -d --wait`, Compose puede reportar `unhealthy` y salir con error. El healthcheck oficial da poco margen mientras corren las migraciones; el contenedor pasa a `healthy` solo en un par de minutos. Con `up -d`, como en este README, no aparece ese error. El log también muestra una vez `database_health:db_error` a nivel informativo durante la inicialización; en la prueba no hubo reinicios y las migraciones terminaron completas.

**Cambios al compose oficial de Teable:**

| Cambio | Motivo |
|---|---|
| Se quitó la publicación `42345:5432` de la BD | Regla de la PoC: ningún puerto de BD fuera de la red de Docker |
| `environment` explícito en lugar de `env_file` | El `.env` también guarda `MONDAY_API_TOKEN`, que Teable no debe recibir |
| Se agregó `BACKEND_CACHE_PROVIDER=redis` | Viene del ejemplo oficial en el repo de Teable; la guía no lo incluye |
| Puerto de la app solo en `127.0.0.1` | La PoC local no necesita ser accesible desde la red |

**Versiones:** Baserow está fijo en `2.4.1`. Teable usa `latest`, igual que su guía; antes de medir resultados conviene anotar el digest de la imagen en `RESULTADOS.md` para saber qué versión se evaluó.

## n8n (para la tarea 4)

Los dos sentidos de la conexión tienen requisitos distintos si n8n corre en un servidor:

- **Herramienta → n8n (webhook):** sale desde tu máquina, así que funciona si la URL de n8n es pública.
- **n8n → herramienta (descargar archivo, crear registro):** n8n no puede llegar a tu `localhost`. Hace falta un túnel (Tailscale o cloudflared) o montar la PoC en el VPS.

Si n8n está en una IP privada (incluida Tailscale), Baserow bloquea el webhook. Para permitirlo, pon cualquier valor en `BASEROW_WEBHOOKS_ALLOW_PRIVATE_ADDRESS`. Baserow lo documenta como riesgo de seguridad: úsalo solo en la PoC.
