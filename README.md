# linkManager

Página de inicio para tu homelab, con un panel web para administrar sus links:
altas, bajas, modificaciones y orden de las tarjetas, más la búsqueda de iconos
en internet.

La página pública es **HTML estático**. Cada vez que cambiás algo desde el panel,
linkManager regenera `index.html`. Si el servicio está caído, la página se sigue
viendo igual.

## Funcionalidades

- **ABM de links**: nombre, URL, descripción e icono. Las flechas ↑ ↓ cambian el orden.
- **Secciones**: agregá divisores con título para agrupar los links en varias
  secciones de la página. Cada link se asigna a una sección desde el formulario, y
  las secciones se pueden renombrar, mover o borrar (sus links pasan a la anterior).
- **Iconos**: podés elegir uno ya subido, subir uno propio (png, svg, jpg, webp, gif o
  ico, de hasta 2 MB) o **buscarlo en internet** mientras escribís el nombre de
  la aplicación. La búsqueda muestra varias opciones de distintas fuentes:
  - [dashboard-icons](https://github.com/homarr-labs/dashboard-icons)
  - [selfh.st icons](https://github.com/selfhst/icons)
  - [Simple Icons](https://simpleicons.org/)
  - [Wikimedia Commons](https://commons.wikimedia.org/)

  El icono que elegís se descarga al servidor, así que la página no depende de
  servicios externos.
- **Integraciones DNS** (opcional): en la pestaña *Configuración* del panel cargás
  tus servidores DNS (por ahora Pi-hole v5 y v6). Al dar de alta un link, elegís
  en cuál crear su nombre: indicás el dominio con el que querés resolverlo y el
  destino. Si el destino es una IP se crea un registro A/AAAA, y si es un nombre,
  un CNAME. Con *Usar este dominio en la URL del link*, la tarjeta queda apuntando
  al nombre nuevo en lugar de a la IP (ver [Integraciones DNS](#integraciones-dns)).
- **Backups automáticos**: cada cambio guarda una copia con fecha de `links.json`,
  `index.html` y `htpasswd` (por defecto se conservan las últimas 200).
- **Cambio de contraseña** del administrador desde el panel.
- **Salir (logout)**: como la autenticación básica deja la contraseña guardada en el
  navegador, el botón *Salir* la reemplaza por una credencial ficticia (`logout:logout`,
  válida solo para `/admin/logout.txt`), y el próximo acceso al panel vuelve a pedirla.
- **Sin dependencias**: solo la biblioteca estándar de Python 3, nginx y openssl.

## Instalación (Debian / Ubuntu)

```bash
git clone https://github.com/arcsurf/linkManager.git
cd linkManager
sudo ./install.sh
```

Al terminar, el instalador muestra la URL y la contraseña inicial del usuario
`admin`. Entrá a `http://<servidor>/admin/` y cambiala desde la sección
*Cambiar contraseña*.

Funciona bien en un contenedor LXC de Proxmox (Debian 12 o Ubuntu 24.04, con
512 MB de RAM alcanza).

### Migrar una página existente

Si ya tenés una página con tarjetas
`<article class="aplicacion"><a href="..."><img src="./images/..."><h2>...</h2><p>...</p></a></article>`,
copiala a `/var/www/linkmanager/index.html` y ejecutá:

```bash
sudo runuser -u www-data -- env LM_WEB_ROOT=/var/www/linkmanager \
  LM_DATA=/var/lib/linkmanager/links.json LM_BACKUP_DIR=/var/backups/linkmanager \
  python3 tools/migrate.py
```

## Cómo funciona

```
navegador ──► nginx ──┬── /            archivos estáticos (index.html, images/, css/)
                      ├── /admin/      panel (auth_basic)
                      └── /api/        ──► app.py en 127.0.0.1:8091 (auth_basic)
```

- `app.py` guarda los links en `links.json` y reescribe en `index.html` solo lo
  que está entre `<!-- LINKS:START -->` y `<!-- LINKS:END -->`. El resto de la
  página (título, estilos, pie) se puede editar a mano.
- La autenticación la hace nginx con `auth_basic`. La API escucha solo en
  localhost.
- Los iconos se descargan solo desde las CDNs de las fuentes de arriba, que
  forman una lista de hosts permitidos.

## Configuración

Por variables de entorno, en `deploy/linkmanager.service`:

| Variable | Por defecto |
|---|---|
| `LM_WEB_ROOT` | `/var/www/linkmanager` |
| `LM_DATA` | `/var/lib/linkmanager/links.json` |
| `LM_BACKUP_DIR` | `/var/backups/linkmanager` |
| `LM_HTPASSWD` | `/var/lib/linkmanager/htpasswd` |
| `LM_LISTEN` | `127.0.0.1:8091` |
| `LM_BACKUPS_TO_KEEP` | `200` |
| `LM_INTEGRATIONS` | `integraciones.json` en la carpeta de `LM_DATA` |

## Integraciones DNS

Se administran desde el panel, en **Configuración → Integraciones DNS**: alta,
edición, baja y *Probar conexión*. Cada tipo de servidor pide sus propios datos;
para Pi-hole son:

| Campo | Descripción |
|---|---|
| IP o URL del servidor | `192.0.2.10`, `http://192.0.2.10` o `https://pi.hole` (sin `/admin`; con solo la IP se usa `http://`) |
| Versión | *Detectar automáticamente* (por defecto), v6 o v5 |
| Contraseña o API key | v6: contraseña web o *app password*. v5: token de la API (*Settings → API → Show API token*) o la contraseña web |
| Verificar el certificado HTTPS | Desmarcalo si Pi-hole usa un certificado autofirmado |

Al guardar una integración, el panel prueba la conexión y muestra cuántos
registros DNS locales y CNAME tiene el servidor.

Las integraciones se guardan en `integraciones.json`, junto a `links.json` (se
puede cambiar con `LM_INTEGRATIONS`). El archivo tiene permisos `600` porque
guarda las contraseñas, y cada cambio deja un backup con fecha. La API nunca
devuelve las contraseñas al navegador: al editar, si dejás el campo vacío se
conserva la guardada.

En el alta de un link, el bloque **Registro DNS** muestra un desplegable con las
integraciones. Si escribís una URL con IP, la IP se propone como destino. Si la
escribís con un nombre, el nombre se propone como dominio.

- Primero se crea el registro en el servidor DNS y después el link. Si el servidor
  rechaza el registro (por ejemplo, porque el dominio ya existe), el link no se
  crea y el panel muestra el error.
- El registro queda guardado en el link (`dns` en `links.json`). Borrar o editar
  el link, o borrar la integración, **no** modifica el servidor DNS.
- Pi-hole: la v6 usa su API REST (`/api/config/dns/…`) y la v5, `admin/api.php`
  (`customdns` / `customcname`).
- Si venías de la versión que se configuraba con variables `LM_PIHOLE_*`, al
  iniciar se importan solas como una integración llamada "Pi-hole" (solo si
  todavía no hay integraciones). Después podés quitar esas variables.

### Agregar otro tipo de servidor DNS

En `app.py`, sumá una entrada a `PROVIDERS` con su nombre, los tipos de registro
que admite, los campos que pide (el panel arma el formulario con esa lista; un
campo con `"secreto": true` nunca se devuelve al navegador) y dos funciones:
`add(cfg, registro)` y `test(cfg)`.

## API

Todas las rutas requieren autenticación básica.

| Método | Ruta | Descripción |
|---|---|---|
| GET | `/api/links` | Lista de links |
| POST | `/api/links` | Alta: `{nombre, url, descripcion, seccion, icono \| icono_url \| icono_data+icono_nombre, dns?}` o sección: `{tipo: "seccion", titulo}`. `dns` opcional: `{integracion, dominio, destino, usar_en_url}` |
| PUT | `/api/links/<id>` | Modificación (mismos campos, todos opcionales; `seccion` mueve el link) o `{titulo}` para una sección |
| DELETE | `/api/links/<id>` | Baja |
| POST | `/api/links/<id>/mover` | Reordenar: `{dir: -1 \| 1}` |
| GET | `/api/iconos` | Iconos disponibles en `images/` |
| GET | `/api/buscar-icono?q=<nombre>` | Busca iconos en internet |
| GET | `/api/integraciones/tipos` | Tipos de servidor DNS disponibles y los campos que pide cada uno |
| GET | `/api/integraciones` | Integraciones configuradas (sin contraseñas) |
| POST | `/api/integraciones` | Alta: `{tipo, nombre, ...campos del tipo}` |
| PUT | `/api/integraciones/<id>` | Modificación (un campo secreto vacío conserva el guardado) |
| DELETE | `/api/integraciones/<id>` | Baja |
| POST | `/api/integraciones/<id>/probar` | Prueba la conexión de una integración guardada |
| POST | `/api/integraciones/probar` | Prueba datos sin guardarlos (`{id}` opcional para usar la contraseña guardada) |
| POST | `/api/password` | `{actual, nueva, repetir}` |

## Seguridad

- El panel usa autenticación básica. **Sin HTTPS, la contraseña viaja en texto plano**:
  usalo solo en tu red local o poné nginx detrás de TLS (por ejemplo, Let's Encrypt
  o un proxy inverso).
- No publiques el servicio de la API directamente. Debe quedar detrás de nginx.

## Restaurar un backup

```bash
ls /var/backups/linkmanager/
sudo cp -a /var/backups/linkmanager/links.json.bkp-<fecha> /var/lib/linkmanager/links.json
sudo cp -a /var/backups/linkmanager/index.html.bkp-<fecha> /var/www/linkmanager/index.html
```

## Licencia

MIT
