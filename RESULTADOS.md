# Resultados de la PoC

## Decisión (2026-11-06)

**Se elige Baserow.** De momento se instalará en la Mac mini, publicada con ngrok y login de Microsoft, porque gente externa debe llenar el formulario y el DNS se queda en HostGator (Cloudflare Tunnel gratuito exige mover el DNS). La guía está en [deploy/macmini/README.md](deploy/macmini/README.md).

Lo que la PoC aporta a la decisión, según las mediciones de abajo:
- **A favor de Baserow:**
  - Webhook nativo al cambiar el status, que es la base del file forwarder.
  - Import y restauración sin diferencias.
  - Menos peso en disco que Teable.
- **En contra:**
  - El kanban requiere Premium (USD 10–12 por usuario al mes).
  - Los archivos se sirven sin pedir sesión, lo que obliga a poner una capa de login delante (reglas de ngrok).
- **Teable:** quedó descartado sobre todo porque, sin pagar Business (USD 20–36 por usuario al mes), no tiene webhooks ni automatizaciones.

Sin cerrar: la opinión de usuarios tras 3–5 días, el número de licencias Premium necesarias y los flujos de n8n (tarea 4).

## Versiones evaluadas

| Herramienta | Versión |
|---|---|
| Baserow | `baserow/baserow:2.4.1` |
| Teable | `ghcr.io/teableio/teable:latest`, digest `sha256:36d2703d4b59ccfa637998f4191339184454dc7019b6a5446b9c51aa2edc6a6b` |

## Import del board CPP (tarea 3)

Datos importados a cada herramienta: 1,230 items, 221 comentarios y 2,415 referencias a archivos (2,412 archivos distintos, 581 MB).

| Medición | Baserow | Teable |
|---|---|---|
| Diferencias contra el manifiesto | 0 | 0 |
| Archivos descargados del destino y comparados por hash | 2,415 | 2,415 |
| Campos que solo cambian en espacios de los extremos | 26 | 26 |
| Tiempo del import completo, con validación | 140 s | 539 s |
| Segunda corrida | No crea filas | No crea registros ni sube archivos |

Los tiempos parten de una muestra de 25 items ya cargada y corrieron en el mismo equipo, con 4 descargas o subidas simultáneas. No son una prueba de carga. Baserow sube todos los archivos antes de crear las filas en lote; Teable crea los registros y luego hace una petición de subida por archivo, lo que puede explicar parte de la diferencia.

## Notas de integración por API

**Baserow**
- Crear tablas requiere JWT (correo y contraseña); hay que renovarlo porque caduca.
- Los archivos se suben aparte y se asocian al crear la fila, en lotes de 200.

**Teable**
- La sesión por cookie bastó para el import; para n8n habrá que usar un token de acceso personal.
- Los adjuntos se suben uno por uno a un registro ya creado (`uploadAttachment`).
- Los nombres de archivo con acentos se conservaron correctamente.

**Ambas**
- Recortan espacios de los extremos en campos de texto.

## Automatizaciones: hallazgos antes de n8n (tarea 4)

Probado en local con la tabla ficticia "Prueba n8n" (`scripts/create_test_tables.py`). Los flujos de n8n siguen pendientes.

| Capacidad | Baserow 2.4.1 | Teable (edición gratuita self-hosted) |
|---|---|---|
| Webhook al cambiar un registro | Sí, nativo y por API | **No.** La API de automatizaciones responde `402 payment_required` (`automationEnable`) |
| Filtrar el webhook por campo | Sí; una edición que no tocó "Estado" no lo disparó | No aplica |
| Valor anterior en el evento | Sí (`old_items`) | No aplica |
| Encabezado secreto en el webhook | Sí | No aplica |
| Consultar registros filtrados por estado | No probado | Sí, con token de acceso |
| Descargar archivo sin autenticación | Sí, con la URL del archivo | Sí, con `presignedUrl`, que es relativa y hay que anteponerle el origen |
| Crear registro y subir adjunto con token | Sí, token de base de datos | Sí, token de acceso (dos pasos: crear y luego subir) |

Consecuencia: en Teable gratuito el file forwarder solo puede funcionar consultando periódicamente desde n8n, con el retraso de ese intervalo. Falta confirmar si un plan de pago self-hosted lo habilita y a qué precio.

Otros requisitos ya vistos:
- `BASEROW_PUBLIC_URL` y `TEABLE_PUBLIC_ORIGIN` deben ser la dirección que usa n8n; las URLs de archivos se construyen con ellas.
- `BASEROW_WEBHOOKS_ALLOW_PRIVATE_ADDRESS` solo hace falta si el webhook apunta a una IP privada, incluida una de Tailscale.

## Operación (tarea 5)

### Respaldo y restauración

Probado borrando los volúmenes con `down -v` y restaurando con `scripts/backup.cmd` y `scripts/restore.cmd`. El detalle está en [scripts/backup_restore.md](scripts/backup_restore.md).

| | Baserow | Teable |
|---|---|---|
| Volúmenes a respaldar | 1 (todo junto) | 3 (BD, archivos, Redis) |
| Tamaño | 675 MB | 642 MB |
| Respaldar, con la herramienta detenida | 142 s | 164 s |
| Restaurar hasta `healthy` | 137 s | unos 105 s |
| Datos después de restaurar | Idénticos; 2,415 archivos verificados por hash | Idénticos; 2,415 archivos verificados por hash |

### Consumo con los datos de CPP cargados

Medido con `docker stats` en Docker Desktop con WSL2 y 16 CPU. El CPU está en % de un núcleo. La carga fue la validación completa: leer todas las filas y descargar los 2,415 archivos con 8 hilos. No sustituye el uso real de varias personas en la UI; eso hay que medirlo en el VPS.

| | Baserow | Teable (app + BD + Redis) |
|---|---|---|
| RAM en reposo | unos 2.4 GB | unos 1.2 GB |
| RAM con carga, máximo | 2.1 GB | 1.7 GB |
| CPU en reposo, promedio | 5% | 10% |
| CPU con carga, promedio / máximo | 86% / 109% | app 107% / 118%, más unos 15% promedio de BD y Redis |
| Tamaño de imágenes | 2.4 GB | 4.4 GB (app 3.6, Postgres 0.6, Redis 0.2) |

Baserow usó menos RAM con carga que en reposo porque la medición en reposo fue antes de reiniciarlo para la restauración. Para dimensionar el VPS hay que tomar el máximo de los dos.

## Funciones para el scorecard (tarea 6, parcial)

Probado con `scripts/feature_checks.py` (detalle en `export/feature_checks.json`). Las filas de prueba van a la tabla ficticia y se borran al final; sobre CPP solo se crearon vistas con prefijo "PoC". Además se revisó en el navegador, sin sesión, que los formularios públicos muestren el campo de archivo como obligatorio.

| Función | Baserow 2.4.1 (gratuito) | Teable (gratuito self-hosted) |
|---|---|---|
| Formulario público con adjunto obligatorio | Sí. Sin archivo: `This field is required`; con archivo subido de forma anónima, se guarda | Sí. Sin archivo: `Required form fields are missing`; con archivo subido de forma anónima, se guarda |
| Kanban por Estado | **No.** `402 ERROR_FEATURE_NOT_AVAILABLE`: requiere Premium | Sí, la vista se creó |
| Filtro por responsable ("Creado por") | Sí; 652 filas, igual que Monday | Sí; 652 filas, igual que Monday |
| Recuperar un registro borrado | Sí, desde la papelera | Sí, desde la papelera |
| Deshacer un cambio | Sí | Sí |
| Webhook al cambiar Estado | Sí (ver tarea 4) | **No.** Requiere el plan de pago |

En Teable, el conteo por Estado del kanban se comparó leyendo los registros, no las columnas de la vista. Tampoco se probó la experiencia de arrastrar tarjetas.

### Costo de lo que falta en cada versión gratuita

Precios de la pestaña self-hosted de cada página oficial, consultados el 2026-11-06:

| | Plan que lo incluye | Precio por usuario al mes | Qué desbloquea para CPP |
|---|---|---|---|
| Baserow | Premium | USD 10 anual / USD 12 mensual | Kanban (y todas las vistas), comentarios de fila, exportar a Excel |
| Teable | Business | USD 20 anual / USD 36 mensual | Automatizaciones (webhook al cambiar Estado), SSO, panel de administración |

Para comparar con los 4,000 MXN/mes de Monday falta saber cuántos usuarios necesitan cada función. En Baserow las licencias Premium se asignan por usuario; está pendiente confirmar si basta con licenciar solo a quienes usan el kanban. Sin pagar nada, la alternativa al kanban en Baserow sería agrupar una vista de cuadrícula por Estado; tampoco se ha probado.
