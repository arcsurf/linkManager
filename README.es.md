# linkManager

[English](README.md) · **Español**

Página de inicio para tu homelab, con un panel web para administrar sus links:
altas, bajas, modificaciones y orden de las tarjetas, más la búsqueda de iconos
en internet y las integraciones con servidores DNS.

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
  tus servidores DNS: Pi-hole, AdGuard Home, Technitium, PowerDNS, NextDNS,
  Control D y, por SSH, Unbound, dnsmasq, Blocky, Knot Resolver, CoreDNS, BIND9,
  dnscrypt-proxy y OpenWrt. Al dar de alta un link, elegís en cuál crear su
  nombre: indicás el dominio con el que querés resolverlo y el destino. Si el
  destino es una IP se crea un registro A/AAAA, y si es un nombre, un CNAME. Con
  *Usar este dominio en la URL del link*, la tarjeta queda apuntando al nombre
  nuevo en lugar de a la IP. Al borrar el link se borra también su registro (ver
  [Integraciones DNS](#integraciones-dns)).
- **Inglés y español**: el idioma del panel se elige en *Configuración → Idioma* y
  se aplica también a los mensajes del servidor. Por defecto, inglés.
- **Backups automáticos**: cada cambio guarda una copia con fecha de `links.json`,
  `index.html`, `integraciones.json` y `htpasswd` (por defecto se conservan las
  últimas 200).
- **Cambio de contraseña** del administrador desde el panel.
- **Salir (logout)**: como la autenticación básica deja la contraseña guardada en el
  navegador, el botón *Salir* la reemplaza por una credencial ficticia (`logout:logout`,
  válida solo para `/admin/logout.txt`), y el próximo acceso al panel vuelve a pedirla.
- **Sin dependencias**: solo la biblioteca estándar de Python 3, nginx, openssl y,
  para las integraciones DNS por SSH, el cliente `ssh` (openssh-client).

## Instalación (Debian / Ubuntu)

```bash
git clone https://github.com/arcsurf/linkManager.git
cd linkManager
sudo ./install.sh
```

Al terminar, el instalador muestra la URL y la contraseña inicial del usuario
`admin`. Entrá a `http://<servidor>/admin/` y cambiala desde *Configuración →
Cambiar contraseña del administrador*. El panel arranca en inglés: elegí
*Español* en *Settings → Language*.

Funciona bien en un contenedor LXC de Proxmox (Debian 12/13 o Ubuntu 24.04, con
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
| `LM_SETTINGS` | `settings.json` en la carpeta de `LM_DATA` (ajustes del panel, como el idioma) |
| `LM_LANGUAGE` | `en`: idioma hasta que se elija uno en el panel (`en` o `es`) |

## Integraciones DNS

Se administran desde el panel, en **Configuración → Integraciones DNS**: alta,
edición, baja y *Probar conexión*. Cada tipo de servidor pide sus propios datos
(URL, usuario y contraseña, API key, token, datos SSH...) y el panel muestra sus
instrucciones de preparación.

| Servidor | Cómo se conecta | Registros |
|---|---|---|
| Pi-hole v5 y v6 | API (contraseña o API key) | A, AAAA, CNAME |
| AdGuard Home | API `/control/rewrite` (usuario y contraseña) | A, AAAA, CNAME |
| Technitium DNS Server | API (token) en una zona del servidor | A, AAAA, CNAME |
| PowerDNS Authoritative | API REST (API key) en una zona | A, AAAA, CNAME |
| NextDNS | API en la nube (API key + perfil), como *Rewrites* | A, AAAA, CNAME |
| Control D | API en la nube (token + perfil), como reglas *SPOOF* | A, AAAA, CNAME |
| Unbound | SSH: `local-data` en un archivo de `unbound.conf.d` | A, AAAA, CNAME |
| dnsmasq | SSH: `host-record`/`cname` en un archivo de `dnsmasq.d` | A, AAAA, CNAME |
| Blocky | SSH: archivo hosts de `hostsFile.sources` | A, AAAA |
| Knot Resolver | SSH: archivo hosts del módulo `hints` | A, AAAA |
| CoreDNS | SSH: archivo del plugin `hosts` | A, AAAA |
| BIND9 | SSH: `nsupdate` (actualización dinámica, RFC 2136) | A, AAAA, CNAME |
| PowerDNS Recursor | SSH: archivo de `etc-hosts-file` | A, AAAA |
| dnscrypt-proxy | SSH: archivo de `cloaking_rules` | A, AAAA, nombre* |
| OpenWrt (adblock, adblock-lean, https-dns-proxy) | SSH: `uci` en el dnsmasq del router | A, AAAA, CNAME |

\* dnscrypt-proxy resuelve el nombre de destino por internet y devuelve sus IPs:
sirve con nombres públicos, no con otros nombres del *cloaking*.

### Servidores por SSH

Unbound, dnsmasq, Blocky, Knot Resolver, CoreDNS, PowerDNS Recursor y
dnscrypt-proxy no tienen una API para crear registros. linkManager entra por SSH
y escribe sus registros **solo en un archivo propio** (por ejemplo
`/etc/dnsmasq.d/linkmanager.conf`), sin tocar el resto de la configuración.
Después ejecuta el comando de recarga del servicio. Si ese comando falla, restaura
el archivo anterior y vuelve a recargar, para dejar el servicio como estaba. El
archivo y el comando se pueden cambiar en cada integración, y cada tipo trae los
valores habituales de Debian y Ubuntu.

La clave SSH se genera desde el panel (*Generar par de claves*): copiás la clave
pública en `~/.ssh/authorized_keys` del servidor (en OpenWrt, *System →
Administration → SSH-Keys*). También podés pegar una clave privada propia, sin
contraseña. La primera conexión guarda la clave del servidor en `known_hosts`,
junto a los datos; si después cambia, la conexión se rechaza.

Algunos detalles verificados:

- **Knot Resolver** responde NXDOMAIN para dominios de uso especial (`.home.arpa`,
  `.test`, `.local`...) sin mirar su archivo de hosts: usá otro dominio, por
  ejemplo `casa.lan`.
- **dnsmasq** y **OpenWrt**: el destino de un CNAME tiene que ser un nombre que
  dnsmasq resuelva localmente (otro registro, `/etc/hosts` o DHCP).
- **BIND9**: la zona tiene que aceptar actualizaciones dinámicas. Con
  `update-policy local;` en la zona alcanza con entrar como root: linkManager usa
  `nsupdate -l` con la clave de sesión de named. También se puede indicar un
  archivo de clave TSIG.
- **CoreDNS** relee su archivo solo, así que no necesita comando de recarga.

Al guardar una integración, el panel prueba la conexión: según el servidor,
muestra cuántos registros, zonas o reglas tiene, o verifica por SSH que se pueda
escribir el archivo y que exista el comando de recarga.

Las integraciones se guardan en `integraciones.json`, junto a `links.json` (se
puede cambiar con `LM_INTEGRATIONS`). El archivo tiene permisos `600` porque
guarda contraseñas, tokens y claves privadas SSH, y cada cambio deja un backup
con fecha. La API nunca devuelve esos secretos al navegador: al editar, si dejás
el campo vacío, se conserva el guardado.

En el alta de un link, el bloque **Registro DNS** muestra un desplegable con las
integraciones. Si escribís una URL con IP, la IP se propone como destino. Si la
escribís con un nombre, el nombre se propone como dominio.

- Primero se crea el registro en el servidor DNS y después el link. Si el servidor
  rechaza el registro (por ejemplo, porque el dominio ya existe), el link no se
  crea y el panel muestra el error.
- El registro queda guardado en el link (`dns` en `links.json`) y la lista del
  panel lo muestra.
- **Al borrar el link se borra también su registro DNS** en el servidor. Si el
  servidor no responde o la integración ya no existe, el link no se borra y el
  panel ofrece borrarlo igual, dejando el registro en el servidor. Si el registro
  ya no estaba en el servidor, el link se borra sin error.
- Editar el link o borrar la integración **no** modifica el servidor DNS.
- Si venías de la versión que se configuraba con variables `LM_PIHOLE_*`, al
  iniciar se importan solas como una integración llamada "Pi-hole" (solo si
  todavía no hay integraciones). Después podés quitar esas variables.

### Agregar otro tipo de servidor DNS

En `app.py`, sumá una entrada a `PROVIDERS` con su nombre, grupo, los tipos de
registro que admite, sus instrucciones, los campos que pide (el panel arma el
formulario con esa lista; un campo con `"secret": True` nunca se devuelve al
navegador; los campos SSH comunes salen de `ssh_fields()`) y tres funciones:
`add(cfg, registro)`, `delete(cfg, registro)` (devuelve `False` si el registro ya
no existía) y `test(cfg)`. Agregá la traducción al español de los textos nuevos
en `ES` y ejecutá `python3 tools/check_translations.py`.

## Traducciones

Todos los textos están escritos en inglés en el código. Las traducciones al
español están en el diccionario `ES` de `app.py` (mensajes del servidor y textos
de las integraciones) y en el diccionario `I18N` de `admin/index.html` (panel).
`tools/check_translations.py` verifica que no falte nada y que los dos idiomas
usen las mismas variables.

Los nombres de los campos JSON guardados en `links.json` / `integraciones.json` y
usados por la API (`nombre`, `descripcion`, `icono`, `seccion`...) siguen en
español por compatibilidad con las instalaciones existentes.

## API

Todas las rutas requieren autenticación básica.

| Método | Ruta | Descripción |
|---|---|---|
| GET | `/api/links` | Lista de links |
| POST | `/api/links` | Alta: `{nombre, url, descripcion, seccion, icono \| icono_url \| icono_data+icono_nombre, dns?}` o sección: `{tipo: "seccion", titulo}`. `dns` opcional: `{integracion, dominio, destino, usar_en_url}` |
| PUT | `/api/links/<id>` | Modificación (mismos campos, todos opcionales; `seccion` mueve el link) o `{titulo}` para una sección |
| DELETE | `/api/links/<id>` | Baja. Si el link tiene registro DNS, también lo borra del servidor (responde `dns: "borrado" \| "no_existia"`). Si eso falla responde 409 y no borra el link; con `?forzar=1` borra solo el link |
| POST | `/api/links/<id>/mover` | Reordenar: `{dir: -1 \| 1}` |
| GET | `/api/iconos` | Iconos disponibles en `images/` |
| GET | `/api/buscar-icono?q=<nombre>` | Busca iconos en internet |
| GET | `/api/settings` | Ajustes del panel: `{language, languages}` |
| PUT | `/api/settings` | `{language: "en" \| "es"}` |
| GET | `/api/integraciones/tipos` | Tipos de servidor DNS disponibles, sus instrucciones y los campos que pide cada uno (en el idioma elegido) |
| POST | `/api/integraciones/generar-clave` | Genera un par de claves SSH ed25519: `{privada, publica}` |
| GET | `/api/integraciones` | Integraciones configuradas (sin secretos) |
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
- Las integraciones DNS guardan credenciales de tus servidores y, en las de SSH,
  claves privadas que suelen dar acceso de root. Quien controle el panel puede
  usarlas: protegé el acceso al panel y usá una clave SSH exclusiva para
  linkManager (la que genera el panel), que podés revocar borrándola de
  `authorized_keys`.

## Restaurar un backup

```bash
ls /var/backups/linkmanager/
sudo cp -a /var/backups/linkmanager/links.json.bkp-<fecha> /var/lib/linkmanager/links.json
sudo cp -a /var/backups/linkmanager/index.html.bkp-<fecha> /var/www/linkmanager/index.html
```

## Licencia

MIT
