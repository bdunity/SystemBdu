# Baserow en la Mac mini

Instalación de Baserow en la "Mac mini de Bots", publicada en internet solo por Cloudflare Tunnel y protegida con Cloudflare Access. Arranca con el respaldo verificado de la PoC.

```
Usuarios internos ─┐
n8n (token) ───────┼─► Cloudflare Access ─► Cloudflare Tunnel ─► cloudflared ─► baserow
Externos (form) ───┘     (formulario libre)                    (en la Mac, sin puertos abiertos)
```

## Por qué Cloudflare Access es obligatorio

En las pruebas, Baserow sirve los archivos (`/media/...`) **sin pedir sesión** a quien tenga la URL. Publicado sin protección, cualquiera con un enlace podría descargar facturas o cartas bancarias. Access exige iniciar sesión antes de llegar a Baserow, salvo en las rutas del formulario público.

## Archivos de esta carpeta

| Archivo | Para qué |
|---|---|
| `docker-compose.yml` | Baserow 2.4.1 y `cloudflared`; ningún puerto publicado |
| `.env.example` | Variables; copiar a `.env` |
| `backup.sh` | Respaldo con retención de los últimos `BACKUP_KEEP` |
| `restore.sh` | Restauración; también acepta respaldos hechos con `scripts\backup.cmd` en Windows |

`backup.sh` y `restore.sh` se probaron en Linux con el respaldo real de la PoC. Restaurar tomó 64 s más unos 50 s hasta `healthy`; respaldar, 114 s; los conteos de filas y usuarios coincidieron. **No se han probado en macOS**: están escritos para su bash 3.2 y sus herramientas BSD.

## 1. Preparar la Mac mini

1. Instala **Docker Desktop for Mac (Apple silicon)**. En *Settings*:
   - *General*: activa *Start Docker Desktop when you sign in*.
   - *Resources*: al menos 4 GB de memoria (Baserow usa unos 2.4 GB).
2. En *Ajustes del Sistema > Energía*:
   - Evita que la Mac entre en reposo.
   - Activa el arranque automático después de un corte de luz.
3. Docker Desktop solo corre con una sesión iniciada. Para que Baserow vuelva solo tras un reinicio hace falta inicio de sesión automático, pero **FileVault lo desactiva**. Hay que elegir:
   - **FileVault activo:** el disco queda cifrado, que es lo recomendable con datos bancarios, pero tras un corte de luz alguien tiene que iniciar sesión.
   - **Inicio de sesión automático:** Baserow vuelve solo, pero el disco queda sin cifrar.
4. Crea la carpeta `~/baserow` y copia ahí los archivos de esta carpeta.

## 2. Cloudflare

Necesitas un dominio administrado en Cloudflare. En **Zero Trust**:

1. **Túnel:** *Networks > Tunnels > Create a tunnel > Cloudflared*. Copia el token; va en `.env` como `CLOUDFLARE_TUNNEL_TOKEN`.
2. **Hostname público del túnel:** `baserow.<tu-dominio>` → servicio `HTTP`, URL `baserow:80`.
3. **Aplicación de Access "Baserow"** (*Access > Applications > Self-hosted*):
   - Destino: `baserow.<tu-dominio>` (todas las rutas).
   - Política *Allow*: correos del equipo, por ejemplo `@tu-empresa.com` o una lista.
   - Política *Service Auth*: el token de servicio de n8n (paso 5).
4. **Aplicación de Access "Baserow formulario público"**:
   - Destinos, todos en `baserow.<tu-dominio>`: `/form/*`, `/_nuxt/*`, `/fonts/*`, `/_i18n/*` y `/api/database/views/form/*`.
   - Política *Bypass*, incluir *Everyone*.

   En Access, la regla de la ruta más específica tiene prioridad ([documentación](https://developers.cloudflare.com/cloudflare-one/policies/access/app-paths/)). Las rutas salen de medir qué pide un navegador sin sesión al abrir y enviar el formulario; el formulario se genera en el servidor y solo pide esos recursos.
5. **Token de servicio para n8n:** *Access > Service credentials > Service Tokens*. n8n debe enviar `CF-Access-Client-Id` y `CF-Access-Client-Secret` en cada petición.

   El nodo nativo de Baserow en n8n no permite agregar encabezados, así que con Access hay que usar **HTTP Request**.

## 3. Restaurar y arrancar

En la PC, haz un respaldo nuevo para incluir lo último:

```bat
scripts\backup.cmd baserow
```

Copia la carpeta del respaldo a la Mac por Tailscale. Va cifrado y no pasa por internet:

```bat
"C:\Program Files\Tailscale\tailscale.exe" file cp backups\baserow-<fecha>\baserow_data.tar.gz backups\baserow-<fecha>\SHA256SUMS mac-mini-de-bots:
```

En la Mac, los archivos llegan a *Descargas*. Muévelos a `~/baserow/backups/baserow-<fecha>/` y luego:

```bash
cd ~/baserow
cp .env.example .env
nano .env
chmod +x backup.sh restore.sh
./restore.sh backups/baserow-<fecha>
docker compose ps
```

En `.env` llena `BASEROW_PUBLIC_URL` (con `https://`) y `CLOUDFLARE_TUNNEL_TOKEN`.

## 4. Comprobar antes de avisar a nadie

Usa una ventana de incógnito:

| Prueba | Resultado esperado |
|---|---|
| `https://baserow.<tu-dominio>` | Pantalla de inicio de sesión de **Cloudflare**, no de Baserow |
| URL pública del formulario | El formulario carga sin pedir sesión |
| Enviar el formulario sin archivo | Error de campo obligatorio |
| Enviar el formulario con archivo | Se guarda; revisa el registro dentro de Baserow |
| Abrir la URL de un archivo (`/media/...`) | Pide sesión de Cloudflare |

Si el formulario no carga o no envía, revisa en las herramientas del navegador (pestaña *Network*) qué ruta bloquea Access y agrégala a la aplicación del formulario **solo si es del formulario**.

## 5. Al terminar

- **Contraseña del administrador.** El respaldo trae la cuenta de prueba de la PoC (`admin@monday-poc.test`), cuya contraseña está en el `.env` de tu PC. Cámbiala de inmediato o crea tu cuenta de administrador real y borra la de prueba.
- **Usuarios.** Crea las cuentas reales del equipo.
- **Restos de la PoC.** Decide si borras la tabla "Prueba n8n" y las vistas con prefijo "PoC".
- **Datos al día.** El respaldo trae los datos de CPP del 4 de noviembre. Antes de dejar Monday hay que reimportar o cargar los cambios posteriores.

## Respaldos

```bash
cd ~/baserow && ./backup.sh
```

Detiene Baserow unos 2 minutos. Pendiente, sin probar:
- **Programarlo de noche.** Con `cron`, por ejemplo:

  ```
  PATH=/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin
  0 3 * * * cd ~/baserow && ./backup.sh >> backups/backup.log 2>&1
  ```

  En macOS, `cron` puede necesitar *Acceso total al disco*.
- **Copia fuera de la Mac** (Backblaze B2, por ejemplo con `rclone`). Mientras no exista, un fallo del disco pierde los datos **y** los respaldos.

## Actualizar Baserow

Haz primero un respaldo. Cambia la versión de la imagen en `docker-compose.yml` y en los dos scripts, y luego:

```bash
docker compose pull && docker compose up -d
```
