# Baserow en la Mac mini

Instalación de Baserow en la "Mac mini de Bots", publicada en internet solo por **ngrok** y protegida con login de **Microsoft**. El DNS se queda en HostGator: solo se agrega un registro para el subdominio. Arranca con el respaldo verificado de la PoC.

```
Equipo (login Microsoft) ─┐
n8n (pendiente, por IP)  ─┼─► ngrok (reglas de ngrok-policy.yml) ─► agente ngrok ─► baserow
Externos (solo formulario)┘                                          (en la Mac, sin puertos abiertos)
```

## Por qué las reglas de acceso son obligatorias

En las pruebas, Baserow sirve los archivos (`/media/...`) **sin pedir sesión** a quien tenga la URL. Publicado sin protección, cualquiera con un enlace podría descargar facturas o cartas bancarias. `ngrok-policy.yml` exige login de Microsoft con un correo de la empresa antes de llegar a Baserow. Las excepciones son las rutas del formulario público y n8n.

## Archivos de esta carpeta

| Archivo | Para qué |
|---|---|
| `docker-compose.yml` | Baserow 2.4.1 y el agente de ngrok 3.39.13; ningún puerto publicado |
| `ngrok-policy.yml` | Quién entra: formulario libre, el resto con login de Microsoft |
| `.env.example` | Variables; copiar a `.env` |
| `backup.sh` | Respaldo con retención de los últimos `BACKUP_KEEP` |
| `restore.sh` | Restauración; también acepta respaldos hechos con `scripts\backup.cmd` en Windows |

`backup.sh` y `restore.sh` se probaron en Linux con el respaldo real de la PoC. Restaurar tomó 64 s más unos 50 s hasta `healthy`; respaldar, 114 s; los conteos de filas y usuarios coincidieron. **No se han probado en macOS**: están escritos para su bash 3.2 y sus herramientas BSD. **La política de ngrok tampoco se ha probado**: la fase 1 sirve justamente para comprobarla (sección 6).

## Se hace en dos fases

| Fase | Dirección | Costo | Para qué |
|---|---|---|---|
| **1. Prueba** | `https://exclusion-refrain-bring.ngrok-free.dev` (dominio gratuito de la cuenta) | USD 0 | Instalar todo en la Mac y comprobar las reglas de acceso **antes de pagar** |
| **2. Definitiva** | `https://system.bdunity.com` | USD 20 al mes | Uso real |

Pasar de la fase 1 a la 2 es cambiar `BASEROW_PUBLIC_URL` en `.env` y reiniciar (`docker compose up -d`). Los datos no se tocan.

En la fase 1, ngrok muestra a los navegadores una página de aviso antes de entrar. Es normal en el plan gratuito y desaparece en la fase 2.

## 1. Cuenta de ngrok

1. **Authtoken:** cópialo de *Getting Started > Your Authtoken*; va en `.env`.
2. **Solo en la fase 2:** contrata **Pay-as-you-go** (USD 20 al mes con USD 20 de consumo incluido). Luego, en *Domains > New Domain > Bring your own domain*, agrega `system.bdunity.com`. ngrok te da un valor **CNAME**.

## 2. HostGator (solo fase 2)

`bdunity.com` usa el CDN de Cloudflare incluido en el hosting de HostGator: todo registro nuevo queda detrás de su proxy, y así ngrok no puede emitir el certificado. Por eso son tres pasos:

1. En el **cPanel**, en el editor de zona, crea:

   | Tipo | Nombre | Valor |
   |---|---|---|
   | CNAME | `system` | El valor de ngrok del paso 1.2 |

2. **Escribe al chat de soporte de HostGator** y pide que **desactiven el proxy de Cloudflare solo para el registro `system.bdunity.com`**. Solo soporte puede hacerlo; confirmaron que es posible sin afectar el resto del dominio.
3. Comprueba que ya no pasa por Cloudflare. En CMD o Terminal:

   ```bash
   nslookup system.bdunity.com 1.1.1.1
   ```

   Debe mostrar el nombre de ngrok y **no** las IPs `104.21.52.108` / `172.67.198.48`. Cuando sea así, ngrok emite el certificado solo, lo que puede tardar de minutos a unas horas.

No se toca nada más: el correo de Microsoft 365 y la web siguen igual.

## 3. Ajustar la política

En `ngrok-policy.yml`, cambia `@tu-empresa.com` por el dominio de correo real, probablemente `@bdunity.com`. Aparece una sola vez, en la regla `solo-empresa`. Si también entran correos de otro dominio, como `@pldunity.com`, agrega los dos (el archivo explica cómo).

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

En `.env` llena `NGROK_AUTHTOKEN` y `BASEROW_PUBLIC_URL`: en la fase 1, `https://exclusion-refrain-bring.ngrok-free.dev`; en la fase 2, `https://system.bdunity.com`.

En los logs de ngrok, si la política tiene un error el agente lo dice al arrancar y **no publica nada**. Ojo: una condición que falla **mientras corre** no detiene al agente; esa regla simplemente no se aplica. Por eso la sección 6 incluye pruebas que deben repetirse después de cada cambio a la política.

## 6. Comprobar antes de avisar a nadie

Usa una ventana de incógnito:

| Prueba | Resultado esperado |
|---|---|
| `https://system.bdunity.com` | Login de **Microsoft**, no de Baserow |
| Login con un correo de fuera de la empresa | Error 403 |
| URL pública del formulario | Carga sin pedir login |
| Enviar el formulario sin archivo | Error de campo obligatorio |
| Enviar el formulario con archivo | Se guarda; revisa el registro dentro de Baserow |
| Abrir la URL de un archivo (`/media/...`) | Pide login de Microsoft |

Prueba desde cualquier terminal que un encabezado inventado **no** se salte el login (debe responder `302`):

```bash
curl -s -o /dev/null -w "%{http_code}
" -H "X-N8N-Key: inventado" https://system.bdunity.com/api/_health/
```

Si el formulario no carga o no envía, revisa en las herramientas del navegador (pestaña *Network*) qué ruta recibe la redirección a Microsoft y agrégala a las excepciones **solo si es del formulario**.

## 7. n8n

- **Peticiones de n8n a Baserow: pendiente.** n8n no puede hacer login de Microsoft. Se agregará a la política una excepción por la IP pública del servidor de n8n (`conn.client_ip == '<IP>'`), y se probará antes de usarla con datos reales. Mientras tanto, n8n no puede llegar a Baserow. Una versión anterior usaba un encabezado secreto con `secrets.get()`; **fallaba abierto** y se retiró (ver el comentario en `ngrok-policy.yml`).
- **Nodo nativo de Baserow en n8n:** no permite encabezados propios; con ngrok delante hay que usar **HTTP Request**.
- **Webhooks de Baserow a n8n:** van de salida y no pasan por ngrok. Solo necesitan que n8n tenga una URL alcanzable desde la Mac.

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
- [Agente en Docker](https://ngrok.com/docs/using-ngrok-with/docker)
- [Dominios propios](https://ngrok.com/docs/gateway/domains/custom-domains)
