# Handoff: prueba funcional para reemplazar Monday.com

> Pega este archivo al inicio de la sesión de Claude Code (o guárdalo como `CLAUDE.md` en la raíz del repo de la prueba).

## Objetivo

Construir una **prueba funcional (PoC)** para decidir con qué reemplazar Monday.com:

- **Candidato principal:** Baserow self-hosted.
- **Contra quién se compara:** Teable self-hosted.
- **Opcional:** NocoDB, reutilizando el patrón de `infra/docker-compose.yml` del repo `Nexus`.

La prueba se hace con **un solo board real**, "Cuentas por pagar 2026" (CPP). Se eligió porque toca casi todo: formulario, archivos adjuntos, cambio de status y automatización.

Esta sesión es **solo la PoC**. La migración completa viene después, si la prueba sale bien.

## Contexto

- Monday cuesta aprox. **4,000 MXN/mes** (por confirmar si son MXN mensuales).
- Hay 5 automatizaciones que hoy leen o escriben en Monday:
  1. Inventario de equipo de cómputo (script Python que sube desde Excel).
  2. Facturas → board CPP (formulario "Solicitud CPP"; facturas descargadas vía Microsoft Graph).
  3. File forwarder en n8n (al cambiar status, descarga el archivo y lo manda por correo vía Graph).
  4. Bot de WhatsApp "Gala HR" (WAHA + n8n + agente IA) que consulta y escribe en Monday.
  5. Hermes (buzón compartido de Outlook → Monday).
- Las automatizaciones corren en **n8n**; los correos salen por **Microsoft Graph API**.
- Destino final: un VPS de HostGator con **EasyPanel** (uno de los VPS ya tiene la licencia de pago). Respaldos a **Backblaze B2**.
- Plan completo (costos, cronograma, riesgos): https://claude.ai/artifact/Es6jZx196fkSKrLCzRaq4a

## Entorno local

- **Windows 11, terminal CMD.** Los comandos van en una sola línea o con `^` para partir (no `\`). En PowerShell se usa el acento grave.
- **Docker Desktop** tiene que estar abierto y con el motor corriendo antes de cualquier `docker` (verificar con `docker version`, debe aparecer la sección Server).
- **Python:** el venv de Hermes Agent se antepone en el PATH y no trae pip. Usar el Python real con ruta completa: `C:\Users\Fernando\AppData\Local\Microsoft\WindowsApps\python.exe`, o crear un venv propio para este proyecto y usar ese.
- **n8n:** si corre en un servidor y la PoC en `localhost`, los webhooks no van a llegar. Opciones: montar la PoC en el VPS como proyectos temporales de EasyPanel, o exponer local con Tailscale/cloudflared.

## Estructura sugerida del repo

```
monday-poc/
├── CLAUDE.md                 ← este archivo
├── .env.example              ← nombres de variables, sin valores reales
├── .gitignore                ← incluye .env, export/, backups/
├── infra/
│   ├── baserow/docker-compose.yml
│   ├── teable/docker-compose.yml   ← tomarlo de la guía oficial de Teable
│   └── nocodb/docker-compose.yml   ← opcional, basado en Nexus
├── scripts/
│   ├── export_monday_board.py
│   ├── import_baserow.py
│   ├── import_teable.py
│   └── backup_restore.md
├── n8n/
│   ├── baserow_file_forwarder.json
│   ├── baserow_create_record.json
│   ├── teable_file_forwarder.json
│   └── teable_create_record.json
└── RESULTADOS.md             ← scorecard final
```

## Tareas, en orden

### 1. Infra local

- Compose de Baserow (imagen oficial `baserow/baserow`, volumen persistente, puerto 8080).
- Compose de Teable: **no inventarlo**, copiar el de su documentación oficial de self-hosting (necesita PostgreSQL y Redis).
- Opcional: NocoDB con su metadata en una BD aparte (como en Nexus), **sin** las credenciales `nocodb`/`nocodb` ni `NC_ALLOW_LOCAL_EXTERNAL_DBS` fuera de dev.
- README con cómo levantar y bajar cada uno en CMD.

**Listo cuando:** los tres (o dos) levantan con un comando y se puede entrar desde el navegador.

### 2. Export del board CPP desde Monday

`scripts/export_monday_board.py`, solo lectura contra Monday:

- API GraphQL de Monday, token en variable de entorno `MONDAY_API_TOKEN` (nunca en código).
- Recibe el `board_id` como argumento.
- Paginación por cursor (`items_page` → `next_items_page`).
- Guarda columnas, items, valores de columnas, subitems y updates en JSON.
- Descarga los `assets` a `export/<board_id>/<item_id>/<archivo>` **en cuanto obtiene la URL** (las `public_url` caducan).
- Respeta el límite de complejidad: pausa y reintenta con backoff cuando la API lo pida.
- Genera `manifest.json` con conteo de items y archivos y SHA-256 por archivo.

**Listo cuando:** corre de punta a punta sobre CPP y el manifiesto cuadra con lo que se ve en Monday.

### 3. Import a Baserow y a Teable

`scripts/import_baserow.py` y `scripts/import_teable.py`:

- Crean la tabla y los campos a partir de las columnas exportadas:

  | Monday | Destino |
  |---|---|
  | Status / dropdown | Single select con las mismas opciones |
  | Texto, números, fechas | Texto, número, fecha |
  | People | Correo como texto |
  | Files | Campo de archivos |
  | Updates | Tabla de comentarios ligada |

- Agregan siempre la columna `monday_id`.
- Importan registros y suben archivos asociándolos por `monday_id`.
- Al final validan contra `manifest.json` (conteos y hashes) e imprimen las diferencias.

**Listo cuando:** ambos imports pasan la validación sin diferencias.

### 4. Automatizaciones de prueba en n8n

Para **cada** herramienta, dos flujos exportados como JSON:

- **File forwarder:** webhook al cambiar el status → descargar el archivo del registro → mandarlo por correo con Graph.
- **Create record (tipo Hermes):** crear un registro con adjunto vía API.

En Baserow usar el nodo nativo si existe; en Teable, HTTP Request. Anotar en `RESULTADOS.md` el tiempo invertido y los problemas de cada uno.

### 5. Operación

- Documentar en `scripts/backup_restore.md` cómo respaldar (BD + archivos) y restaurar cada herramienta, y **probarlo**: respaldar, borrar volúmenes, restaurar y verificar.
- Medir consumo con `docker stats` durante uso normal.

### 6. Scorecard

`RESULTADOS.md` con una tabla Baserow vs Teable (vs NocoDB) en:

- Formulario "Solicitud CPP" con adjunto obligatorio
- Manejo de archivos (UI y API)
- Webhook al cambiar status
- Creación por API con adjunto
- Recuperar un registro borrado / deshacer
- Vistas: kanban por status y filtro por responsable
- Respaldo y restauración
- RAM y CPU
- Opinión de 2–3 usuarios de CPP tras 3–5 días
- Tiempo invertido en cada integración

Y una recomendación final de un párrafo.

## Reglas

- **Monday es solo lectura.** No crear, editar ni borrar nada en Monday desde la PoC.
- **Cero secretos en git.** Tokens y contraseñas solo en `.env` (ignorado). Commitear `.env.example` con los nombres.
- No reutilizar credenciales de producción (Graph, Monday) en archivos de ejemplo.
- No exponer puertos de BD fuera de la red de Docker.
- Si algo de la API de Monday, Baserow o Teable no está claro, revisar su documentación oficial antes de suponer.

## Primer prompt sugerido

> Lee este CLAUDE.md. Empieza por la tarea 1: crea la estructura del repo, el compose de Baserow y el de Teable (tomado de su documentación oficial), el `.env.example` y el `.gitignore`. Estoy en Windows con CMD; dame los comandos para levantar cada uno.
