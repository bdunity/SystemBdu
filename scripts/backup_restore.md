# Respaldo y restauración

Probado el 2026-11-06 en esta PoC: respaldar, borrar los volúmenes con `down -v`, restaurar y verificar. Las dos herramientas volvieron sin diferencias.

## Qué se respalda

| Herramienta | Volúmenes | Contenido |
|---|---|---|
| Baserow | `baserow_data` | PostgreSQL y Redis internos, archivos subidos y secretos generados. Un solo volumen tiene todo |
| Teable | `teable-db` | PostgreSQL: tablas, registros y metadatos |
| | `teable-data` | Archivos adjuntos |
| | `teable-cache` | Redis, incluida la cola de automatizaciones, como pide la [guía de Teable](https://help.teable.ai/en/deploy/docker) |

El respaldo copia los volúmenes completos con la herramienta detenida, así que la base de datos queda consistente. Se restaura con las mismas versiones de imagen: si cambias de versión, restaura primero y actualiza después.

## Respaldar

Desde la raíz del repo, en CMD:

```bat
scripts\backup.cmd baserow
```

```bat
scripts\backup.cmd teable
```

Crea `backups\<herramienta>-<fecha>\` con un `.tar.gz` por volumen y `SHA256SUMS`. La herramienta se detiene mientras copia; con los datos de CPP, Baserow estuvo detenido unos 2 minutos y Teable unos 2.5.

`backups\` está en `.gitignore`: contiene los datos bancarios y las facturas.

## Restaurar

Por seguridad, `restore.cmd` no borra nada: si los volúmenes existen, se detiene. Para reemplazar los datos actuales:

1. Baja la herramienta **borrando sus volúmenes**:

   ```bat
   docker compose --env-file .env -f infra\baserow\docker-compose.yml down -v
   ```

2. Restaura desde la carpeta del respaldo:

   ```bat
   scripts\restore.cmd baserow backups\baserow-20261106-104523
   ```

3. Espera a que el estado sea `healthy` con `docker compose ... ps`.

Para Teable es igual, cambiando `baserow` por `teable` en los tres pasos.

El script verifica `SHA256SUMS` antes de tocar nada, crea volúmenes vacíos con `docker compose create`, descomprime cada archivo y arranca la herramienta.

## Verificar

```bat
C:\Users\Fernando\AppData\Local\Microsoft\WindowsApps\python.exe scripts\import_baserow.py 18393961251 --validate-only
```

```bat
C:\Users\Fernando\AppData\Local\Microsoft\WindowsApps\python.exe scripts\import_teable.py 18393961251 --validate-only
```

Comparan filas, campos y el SHA-256 de cada archivo contra el export de Monday.

## Resultado de la prueba

| | Baserow | Teable |
|---|---|---|
| Tamaño del respaldo | 675 MB | 642 MB (BD 31, archivos 604, Redis 7) |
| Respaldar, incluido el tiempo detenido | 142 s | 164 s |
| Restaurar hasta `healthy` | 137 s | unos 105 s |
| Huella de las 3 tablas antes y después | Igual | Igual |
| Validación contra Monday (2,415 archivos) | Sin diferencias | Sin diferencias |

Al arrancar después de restaurar, `up --wait` marcó a Teable como `unhealthy` y unos 30 segundos después pasó a `healthy`, sin errores ni reinicios. Es lo mismo que en el primer arranque: el healthcheck de su guía da poco margen.

## Pendiente para producción (no probado)

- **Copia externa:** subir `backups\` a Backblaze B2, por ejemplo con `rclone`, y programarlo.
- **Retención:** decidir cuántas copias guardar.
- **Restauración en el VPS:** en EasyPanel los volúmenes tienen otros nombres; hay que adaptar los scripts.
- **Sin detener el servicio:** si no se puede parar, usar `pg_dump` para Teable. Para Baserow, su imagen trae `backend-cmd-with-db backup`, pero solo cubre la BD y no los archivos.
