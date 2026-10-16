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
- **Backups automáticos**: cada cambio guarda una copia con fecha de `links.json`,
  `index.html` y `htpasswd` (por defecto se conservan las últimas 200).
- **Cambio de contraseña** del administrador desde el panel.
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

## API

Todas las rutas requieren autenticación básica.

| Método | Ruta | Descripción |
|---|---|---|
| GET | `/api/links` | Lista de links |
| POST | `/api/links` | Alta: `{nombre, url, descripcion, seccion, icono \| icono_url \| icono_data+icono_nombre}` o sección: `{tipo: "seccion", titulo}` |
| PUT | `/api/links/<id>` | Modificación (mismos campos, todos opcionales; `seccion` mueve el link) o `{titulo}` para una sección |
| DELETE | `/api/links/<id>` | Baja |
| POST | `/api/links/<id>/mover` | Reordenar: `{dir: -1 \| 1}` |
| GET | `/api/iconos` | Iconos disponibles en `images/` |
| GET | `/api/buscar-icono?q=<nombre>` | Busca iconos en internet |
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
