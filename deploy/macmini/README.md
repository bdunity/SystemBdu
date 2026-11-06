# Baserow en la Mac mini

Instalación de Baserow en la "Mac mini de Bots", publicada en internet solo por **ngrok** y protegida con login de **Microsoft**. El DNS se queda en HostGator: solo se agrega un registro para el subdominio. Arranca con el respaldo verificado de la PoC.

```
Equipo (login Microsoft) ─┐
n8n (encabezado secreto) ─┼─► ngrok (reglas de ngrok-policy.yml) ─► agente ngrok ─► baserow
Externos (solo formulario)┘                                          (en la Mac, sin puertos abiertos)
```

## Por qué las reglas de acceso son obligatorias

En las pruebas, Baserow sirve los archivos (`/media/...`) **sin pedir sesión** a quien tenga la URL. Publicado sin protección, cualquiera con un enlace podría descargar facturas o cartas bancarias. `ngrok-policy.yml` exige login de Microsoft con un correo de la empresa antes de llegar a Baserow. Las excepciones son las rutas del formulario público y n8n.

## Archivos de esta carpeta

| Archivo | Para qué |
|---|---|
| `docker-compose.yml` | Baserow 2.4.1 y el agente de ngrok 3.39.13; ningún puerto publicado |
| `ngrok-policy.yml` | Quién entra: formulario libre, n8n con secreto, el resto con login de Microsoft |
| `.env.example` | Variables; copiar a `.env` |
| `backup.sh` | Respaldo con retención de los últimos `BACKUP_KEEP` |
| `restore.sh` | Restauración; también acepta respaldos hechos con `scripts\backup.cmd` en Windows |

`backup.sh` y `restore.sh` se probaron en Linux con el respaldo real de la PoC. Restaurar tomó 64 s más unos 50 s hasta `healthy`; respaldar, 114 s; los conteos de filas y usuarios coincidieron. **No se han probado en macOS**: están escritos para su bash 3.2 y sus herramientas BSD. **La política de ngrok tampoco se ha probado**: requiere una cuenta; la sección 5 dice cómo comprobarla.

## 1. Cuenta de ngrok

1. Crea la cuenta en ngrok y contrata **Pay-as-you-go**: USD 20 al mes con USD 20 de consumo incluido. Es el plan que permite dominios propios.
2. **Dominio:** en *Domains > New Domain*, agrega `baserow.tu-dominio.com`. ngrok te da un valor **CNAME**.
3. **Authtoken:** cópialo de *Getting Started > Your Authtoken*; va en `.env`.
4. **Secreto de n8n**, desde una terminal con el CLI de ngrok autenticado:

   ```bash
   ngrok api vaults create --name "baserow"
   ngrok api secrets create --name "n8n-key" --value "<una cadena larga aleatoria>" --vault-id "<id del vault anterior>"
   ```

   Guarda ese valor en la credencial de n8n, no en archivos. Para generarlo puedes usar `openssl rand -hex 32` en la Mac.

## 2. HostGator

En el panel de DNS del dominio, agrega **un solo registro**:

| Tipo | Nombre | Valor |
|---|---|---|
| CNAME | `baserow` | El que te dio ngrok en el paso 1.2 |

No se toca nada más: el correo y la web siguen igual. ngrok emite el certificado HTTPS solo cuando el CNAME ya resuelve, lo que puede tardar de minutos a unas horas.

## 3. Ajustar la política

En `ngrok-policy.yml`, cambia `@tu-empresa.com` por el dominio de correo real. Aparece una sola vez, en la regla `solo-empresa`.

## 4. Preparar la Mac mini

1. Instala **Docker Desktop for Mac (Apple silicon)**. En *Settings*:
   - *General*: activa *Start Docker Desktop when you sign in*.
   - *Resources*: al menos 4 GB de memoria (Baserow usa unos 2.4 GB).
2. En *Ajustes del Sistema > Energía*:
   - Evita que la Mac entre en reposo.
   - Activa el arranque automático después de un corte de luz.
3. Docker Desktop solo corre con una sesión iniciada. Para que Baserow vuelva solo tras un reinicio hace falta inicio de sesión automático, pero **FileVault lo desactiva**. Hay que elegir:
   - **FileVault activo:** el disco queda cifrado, que es lo recomendable con datos bancarios, pero tras un corte de luz alguien tiene que iniciar sesión.
   - **Inicio de sesión automático:** Baserow vuelve solo, pero el disco queda sin cifrar.
4. Clona el repo: `git clone https://github.com/bdunity/SystemBdu.git ~/SystemBdu`

## 5. Restaurar y arrancar

En la PC, haz un respaldo nuevo para incluir lo último:

```bat
scripts\backup.cmd baserow
```

Copia la carpeta del respaldo a la Mac por Tailscale. Va cifrado y no pasa por internet:

```bat
"C:\Program Files\Tailscale\tailscale.exe" file cp backups\baserow-<fecha>\baserow_data.tar.gz backups\baserow-<fecha>\SHA256SUMS mac-mini-de-bots:
```

En la Mac, los archivos llegan a *Descargas*. Muévelos a `~/SystemBdu/deploy/macmini/backups/baserow-<fecha>/` y luego:

```bash
cd ~/SystemBdu/deploy/macmini
cp .env.example .env
nano .env
chmod +x backup.sh restore.sh
./restore.sh backups/baserow-<fecha>
docker compose ps
docker compose logs ngrok
```

En `.env` llena `BASEROW_PUBLIC_URL` (con `https://`) y `NGROK_AUTHTOKEN`.

En los logs de ngrok, si la política tiene un error (por ejemplo, si ngrok no acepta `secrets.get` dentro de una condición), el agente lo dice al arrancar y **no publica nada**. En ese caso, usa la alternativa de n8n por IP de la sección 7.

## 6. Comprobar antes de avisar a nadie

Usa una ventana de incógnito:

| Prueba | Resultado esperado |
|---|---|
| `https://baserow.<tu-dominio>` | Login de **Microsoft**, no de Baserow |
| Login con un correo de fuera de la empresa | Error 403 |
| URL pública del formulario | Carga sin pedir login |
| Enviar el formulario sin archivo | Error de campo obligatorio |
| Enviar el formulario con archivo | Se guarda; revisa el registro dentro de Baserow |
| Abrir la URL de un archivo (`/media/...`) | Pide login de Microsoft |

Prueba de n8n desde cualquier terminal. Sin el secreto debe pedir login; con él, debe responder `200`:

```bash
curl -s -o /dev/null -w "%{http_code}\n" https://baserow.<tu-dominio>/api/_health/
curl -s -o /dev/null -w "%{http_code}\n" -H "X-N8N-Key: <secreto>" https://baserow.<tu-dominio>/api/_health/
```

Si el formulario no carga o no envía, revisa en las herramientas del navegador (pestaña *Network*) qué ruta recibe la redirección a Microsoft y agrégala a las excepciones **solo si es del formulario**.

## 7. n8n

- **Peticiones de n8n a Baserow:** n8n no puede hacer login de Microsoft, así que usa el encabezado `X-N8N-Key`. El nodo nativo de Baserow no permite encabezados propios, por lo que hay que usar **HTTP Request**, con el token de Baserow más ese encabezado.
- **Webhooks de Baserow a n8n:** van de salida y no pasan por ngrok. Solo necesitan que n8n tenga una URL alcanzable desde la Mac.
- **Alternativa por IP:** si el secreto no funciona en la política, cambia en las dos reglas la condición del encabezado por `conn.client_ip == '<IP pública del servidor de n8n>'`. Es menos flexible, pero no depende de secretos.

## 8. Al terminar

- **Contraseña del administrador.** El respaldo trae la cuenta de prueba de la PoC (`admin@monday-poc.test`), cuya contraseña está en el `.env` de tu PC. Cámbiala de inmediato o crea tu cuenta de administrador real y borra la de prueba.
- **Usuarios.** Crea las cuentas reales del equipo. Cada persona necesita su login de Microsoft para pasar ngrok y su cuenta de Baserow.
- **Registros.** Desactiva el registro libre en *Admin > Settings* de Baserow.
- **Restos de la PoC.** Decide si borras la tabla "Prueba n8n" y las vistas con prefijo "PoC".
- **Datos al día.** El respaldo trae los datos de CPP del 4 de noviembre. Antes de dejar Monday hay que reimportar o cargar los cambios posteriores.

## Respaldos

```bash
cd ~/SystemBdu/deploy/macmini && ./backup.sh
```

Detiene Baserow unos 2 minutos. Pendiente, sin probar:
- **Programarlo de noche.** Con `cron`, por ejemplo:

  ```
  PATH=/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin
  0 3 * * * cd ~/SystemBdu/deploy/macmini && ./backup.sh >> backups/backup.log 2>&1
  ```

  En macOS, `cron` puede necesitar *Acceso total al disco*.
- **Copia fuera de la Mac** (Backblaze B2, por ejemplo con `rclone`). Mientras no exista, un fallo del disco pierde los datos **y** los respaldos.

## Actualizar Baserow

Haz primero un respaldo. Cambia la versión de la imagen en `docker-compose.yml` y en los dos scripts, y luego:

```bash
docker compose pull && docker compose up -d
```

## Referencias de ngrok

- [Acción oauth](https://ngrok.com/docs/traffic-policy/actions/oauth)
- [Variables de la petición](https://ngrok.com/docs/traffic-policy/variables/req)
- [Secretos](https://ngrok.com/docs/traffic-policy/secrets)
- [Agente en Docker](https://ngrok.com/docs/using-ngrok-with/docker)
- [Dominios propios](https://ngrok.com/docs/gateway/domains/custom-domains)
