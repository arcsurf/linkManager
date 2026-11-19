# linkManager

**English** · [Español](README.es.md)

Start page for your homelab, with a web admin panel to manage its links: add,
edit, delete and reorder cards, plus online icon search and DNS integrations.

The public page is **static HTML**. Every time you change something in the panel,
linkManager regenerates `index.html`. If the service is down, the page still
looks the same.

## Features

- **Link management**: name, URL, description and icon. The ↑ ↓ arrows change the order.
- **Sections**: add dividers with a title to group the links in several sections
  of the page. Each link is assigned to a section from the form, and sections can
  be renamed, moved or deleted (their links move to the previous one).
- **Icons**: pick an already uploaded one, upload your own (png, svg, jpg, webp,
  gif or ico, up to 2 MB) or **search the internet** while you type the
  application name. The search shows several options from different sources:
  - [dashboard-icons](https://github.com/homarr-labs/dashboard-icons)
  - [selfh.st icons](https://github.com/selfhst/icons)
  - [Simple Icons](https://simpleicons.org/)
  - [Wikimedia Commons](https://commons.wikimedia.org/)

  The chosen icon is downloaded to the server, so the page does not depend on
  external services.
- **DNS integrations** (optional): in the panel's *Settings* tab you add your DNS
  servers: Pi-hole, AdGuard Home, Technitium, PowerDNS, NextDNS, Control D and,
  over SSH, Unbound, dnsmasq, Blocky, Knot Resolver, CoreDNS, BIND9,
  dnscrypt-proxy and OpenWrt. When you add a link, you choose where to create its
  name: the domain it should resolve as and the target. If the target is an IP an
  A/AAAA record is created; if it is a name, a CNAME. With *Use this domain in the
  link URL*, the card points to the new name instead of the IP. Deleting the link
  deletes its record too (see [DNS integrations](#dns-integrations)).
- **English and Spanish**: the panel language is chosen in *Settings → Language*
  and also applies to the server messages. English is the default.
- **Automatic backups**: every change keeps a timestamped copy of `links.json`,
  `index.html`, `integraciones.json` and `htpasswd` (the last 200 by default).
- **Administrator password change** from the panel.
- **Log out**: since basic authentication leaves the password stored in the
  browser, the *Log out* button replaces it with a dummy credential
  (`logout:logout`, only valid for `/admin/logout.txt`), so the next access to the
  panel asks for it again.
- **No dependencies**: only the Python 3 standard library, nginx, openssl and,
  for the SSH DNS integrations, the `ssh` client (openssh-client).

## Installation

### Option 1: Proxmox VE (new LXC container)

Run this in the **Proxmox VE host** shell (as root). It creates an unprivileged
Debian container (1 core, 512 MB RAM, 4 GB disk, DHCP), installs linkManager in
it and shows the URL and the initial password:

```bash
bash -c "$(curl -fsSL https://raw.githubusercontent.com/arcsurf/linkManager/main/proxmox/linkmanager-lxc.sh)"
```

It shows a summary and asks for confirmation before creating anything. It uses
the newest Debian 13 (or 12) template already downloaded, or downloads it.

To choose the options, download the script, review it and run it with them:

```bash
curl -fsSLO https://raw.githubusercontent.com/arcsurf/linkManager/main/proxmox/linkmanager-lxc.sh
bash linkmanager-lxc.sh --help
bash linkmanager-lxc.sh --ctid 130 --hostname links --ip 192.0.2.20/24 --gw 192.0.2.1 --lang es
```

| Option | Default |
|---|---|
| `-i, --ctid ID` | next free ID |
| `-n, --hostname NAME` | `linkmanager` |
| `-s, --storage NAME` | `local-lvm`, `local-zfs` or the first storage for containers |
| `-t, --template-storage NAME` | `local` |
| `-d, --disk GB` / `-m, --memory MB` / `-c, --cores N` | `4` / `512` / `1` |
| `-b, --bridge NAME` | `vmbr0` |
| `--ip CIDR\|dhcp`, `--gw IP`, `--vlan TAG` | `dhcp` |
| `-l, --lang en\|es` | `en` (initial panel language) |
| `--ssh-key FILE` | none (use `pct enter <ID>` to open a console) |
| `--ref REF`, `--repo OWNER/NAME` | `main`, `arcsurf/linkManager` |
| `--source PATH` | download from GitHub (or install a local copy: folder or `.tar.gz`) |
| `--no-onboot`, `-y, --yes` | starts on boot, asks for confirmation |

If something fails after the container was created, the script keeps it so you
can check what happened, and prints the command to delete it.

To update linkManager later, run inside the container (`pct enter <ID>`):

```bash
curl -fsSL https://github.com/arcsurf/linkManager/archive/main.tar.gz | tar -xz -C /opt/linkmanager-src --strip-components=1 && bash /opt/linkmanager-src/install.sh
```

### Option 2: Debian / Ubuntu

```bash
git clone https://github.com/arcsurf/linkManager.git
cd linkManager
sudo ./install.sh                  # or: sudo LM_LANGUAGE=es ./install.sh
```

When it finishes, the installer shows the URL and the initial password of the
`admin` user. Open `http://<server>/admin/` and change it in *Settings → Change
the administrator password*. Running `install.sh` again updates the application
and keeps the links, the page, the password and the settings.

It works well in any Debian 12/13 or Ubuntu 24.04 machine or container (512 MB
of RAM is enough).

### Migrating an existing page

If you already have a page with cards like
`<article class="aplicacion"><a href="..."><img src="./images/..."><h2>...</h2><p>...</p></a></article>`,
copy it to `/var/www/linkmanager/index.html` and run:

```bash
sudo runuser -u www-data -- env LM_WEB_ROOT=/var/www/linkmanager \
  LM_DATA=/var/lib/linkmanager/links.json LM_BACKUP_DIR=/var/backups/linkmanager \
  python3 tools/migrate.py
```

## How it works

```
browser ──► nginx ──┬── /            static files (index.html, images/, css/)
                    ├── /admin/      panel (auth_basic)
                    └── /api/        ──► app.py on 127.0.0.1:8091 (auth_basic)
```

- `app.py` stores the links in `links.json` and only rewrites what is between
  `<!-- LINKS:START -->` and `<!-- LINKS:END -->` in `index.html`. The rest of
  the page (title, styles, footer) can be edited by hand.
- Authentication is done by nginx with `auth_basic`. The API only listens on
  localhost.
- Icons are only downloaded from the CDNs of the sources above, which form an
  allow list of hosts.

## Configuration

Environment variables, in `deploy/linkmanager.service`:

| Variable | Default |
|---|---|
| `LM_WEB_ROOT` | `/var/www/linkmanager` |
| `LM_DATA` | `/var/lib/linkmanager/links.json` |
| `LM_BACKUP_DIR` | `/var/backups/linkmanager` |
| `LM_HTPASSWD` | `/var/lib/linkmanager/htpasswd` |
| `LM_LISTEN` | `127.0.0.1:8091` |
| `LM_BACKUPS_TO_KEEP` | `200` |
| `LM_INTEGRATIONS` | `integraciones.json` in the `LM_DATA` folder |
| `LM_SETTINGS` | `settings.json` in the `LM_DATA` folder (panel settings, such as the language) |
| `LM_LANGUAGE` | `en`: language used until one is chosen in the panel (`en` or `es`) |

## DNS integrations

They are managed from the panel, in **Settings → DNS integrations**: add, edit,
delete and *Test connection*. Each server type asks for its own data (URL, user
and password, API key, token, SSH data...) and the panel shows its setup
instructions.

| Server | How it connects | Records |
|---|---|---|
| Pi-hole v5 and v6 | API (password or API key) | A, AAAA, CNAME |
| AdGuard Home | `/control/rewrite` API (user and password) | A, AAAA, CNAME |
| Technitium DNS Server | API (token) in a zone of the server | A, AAAA, CNAME |
| PowerDNS Authoritative | REST API (API key) in a zone | A, AAAA, CNAME |
| NextDNS | Cloud API (API key + profile), as *Rewrites* | A, AAAA, CNAME |
| Control D | Cloud API (token + profile), as *SPOOF* rules | A, AAAA, CNAME |
| Unbound | SSH: `local-data` in a file of `unbound.conf.d` | A, AAAA, CNAME |
| dnsmasq | SSH: `host-record`/`cname` in a file of `dnsmasq.d` | A, AAAA, CNAME |
| Blocky | SSH: hosts file of `hostsFile.sources` | A, AAAA |
| Knot Resolver | SSH: hosts file of the `hints` module | A, AAAA |
| CoreDNS | SSH: file of the `hosts` plugin | A, AAAA |
| BIND9 | SSH: `nsupdate` (dynamic update, RFC 2136) | A, AAAA, CNAME |
| PowerDNS Recursor | SSH: `etc-hosts-file` file | A, AAAA |
| dnscrypt-proxy | SSH: `cloaking_rules` file | A, AAAA, name* |
| OpenWrt (adblock, adblock-lean, https-dns-proxy) | SSH: `uci` on the router's dnsmasq | A, AAAA, CNAME |

\* dnscrypt-proxy resolves the target name over the internet and returns its
IPs: it works with public names, not with other *cloaking* names.

### SSH servers

Unbound, dnsmasq, Blocky, Knot Resolver, CoreDNS, PowerDNS Recursor and
dnscrypt-proxy have no API to create records. linkManager connects over SSH and
writes its records **only to a file of its own** (for example
`/etc/dnsmasq.d/linkmanager.conf`), without touching the rest of the
configuration. Then it runs the service's reload command. If that command fails,
it restores the previous file and reloads again, to leave the service as it was.
The file and the command can be changed in each integration, and each type comes
with the usual Debian and Ubuntu values.

The SSH key is generated from the panel (*Generate key pair*): copy the public key
to the server's `~/.ssh/authorized_keys` (on OpenWrt, *System → Administration →
SSH-Keys*). You can also paste your own private key, without passphrase. The
first connection stores the server's host key in `known_hosts`, next to the data;
if it changes later, the connection is refused.

Some verified details:

- **Knot Resolver** answers NXDOMAIN for special-use domains (`.home.arpa`,
  `.test`, `.local`...) without reading its hosts file: use another domain, for
  example `home.lan`.
- **dnsmasq** and **OpenWrt**: the target of a CNAME must be a name dnsmasq
  resolves locally (another record, `/etc/hosts` or DHCP).
- **BIND9**: the zone must accept dynamic updates. With `update-policy local;` in
  the zone, connecting as root is enough: linkManager uses `nsupdate -l` with
  named's session key. A TSIG key file can be set too.
- **CoreDNS** re-reads its file by itself, so it needs no reload command.

When an integration is saved, the panel tests the connection: depending on the
server, it shows how many records, zones or rules it has, or checks over SSH that
the file can be written and that the reload command exists.

Integrations are stored in `integraciones.json`, next to `links.json` (it can be
changed with `LM_INTEGRATIONS`). The file has mode `600` because it holds
passwords, tokens and SSH private keys, and every change leaves a timestamped
backup. The API never sends those secrets back to the browser: when editing, an
empty field keeps the stored value.

When adding a link, the **DNS record** block shows a drop-down list with the
integrations. If you type a URL with an IP, the IP is proposed as the target. If
you type it with a name, the name is proposed as the domain.

- The record is created on the DNS server first and then the link. If the server
  rejects the record (for example, because the domain already exists), the link
  is not created and the panel shows the error.
- The record is stored in the link (`dns` in `links.json`) and the panel list
  shows it.
- **Deleting the link also deletes its DNS record** on the server. If the server
  does not answer or the integration no longer exists, the link is not deleted
  and the panel offers to delete it anyway, leaving the record on the server. If
  the record was no longer on the server, the link is deleted without error.
- Editing the link or deleting the integration does **not** change the DNS server.
- If you come from the version configured with `LM_PIHOLE_*` variables, they are
  imported at startup as an integration named "Pi-hole" (only if there are no
  integrations yet). After that you can remove those variables.

### Adding another DNS server type

In `app.py`, add an entry to `PROVIDERS` with its name, group, the record types it
supports, its setup instructions, the fields it asks for (the panel builds the
form from that list; a field with `"secret": True` is never sent back to the
browser; the common SSH fields come from `ssh_fields()`) and three functions:
`add(cfg, record)`, `delete(cfg, record)` (returns `False` when the record no
longer existed) and `test(cfg)`. Add the Spanish translation of the new texts to
`ES` and run `python3 tools/check_translations.py`.

## Translations

Every text is written in English in the code. The Spanish translations live in
the `ES` dictionary of `app.py` (server messages and integration texts) and in the
`I18N` dictionary of `admin/index.html` (panel). `tools/check_translations.py`
checks that nothing is missing and that both languages use the same placeholders.

The JSON field names stored in `links.json` / `integraciones.json` and used by
the API (`nombre`, `descripcion`, `icono`, `seccion`...) are kept in Spanish for
compatibility with existing installations.

## API

Every route requires basic authentication.

| Method | Path | Description |
|---|---|---|
| GET | `/api/links` | List of links |
| POST | `/api/links` | Add: `{nombre, url, descripcion, seccion, icono \| icono_url \| icono_data+icono_nombre, dns?}` or a section: `{tipo: "seccion", titulo}`. Optional `dns`: `{integracion, dominio, destino, usar_en_url}` |
| PUT | `/api/links/<id>` | Edit (same fields, all optional; `seccion` moves the link) or `{titulo}` for a section |
| DELETE | `/api/links/<id>` | Delete. If the link has a DNS record, it is also deleted from the server (answers `dns: "borrado" \| "no_existia"`). If that fails it answers 409 and keeps the link; with `?forzar=1` only the link is deleted |
| POST | `/api/links/<id>/mover` | Reorder: `{dir: -1 \| 1}` |
| GET | `/api/iconos` | Icons available in `images/` |
| GET | `/api/buscar-icono?q=<name>` | Searches icons online |
| GET | `/api/settings` | Panel settings: `{language, languages}` |
| PUT | `/api/settings` | `{language: "en" \| "es"}` |
| GET | `/api/integraciones/tipos` | Available DNS server types, their instructions and the fields each one asks for (in the current language) |
| POST | `/api/integraciones/generar-clave` | Generates an ed25519 SSH key pair: `{privada, publica}` |
| GET | `/api/integraciones` | Configured integrations (without secrets) |
| POST | `/api/integraciones` | Add: `{tipo, nombre, ...fields of the type}` |
| PUT | `/api/integraciones/<id>` | Edit (an empty secret field keeps the stored one) |
| DELETE | `/api/integraciones/<id>` | Delete |
| POST | `/api/integraciones/<id>/probar` | Tests the connection of a saved integration |
| POST | `/api/integraciones/probar` | Tests data without saving it (optional `{id}` to use the stored password) |
| POST | `/api/password` | `{actual, nueva, repetir}` |

## Security

- The panel uses basic authentication. **Without HTTPS the password travels in
  plain text**: use it only on your local network or put nginx behind TLS (for
  example, Let's Encrypt or a reverse proxy).
- Do not publish the API service directly. It must stay behind nginx.
- The DNS integrations store credentials of your servers and, for SSH ones,
  private keys that usually give root access. Whoever controls the panel can use
  them: protect access to the panel and use an SSH key dedicated to linkManager
  (the one the panel generates), which you can revoke by deleting it from
  `authorized_keys`.

## Restoring a backup

```bash
ls /var/backups/linkmanager/
sudo cp -a /var/backups/linkmanager/links.json.bkp-<date> /var/lib/linkmanager/links.json
sudo cp -a /var/backups/linkmanager/index.html.bkp-<date> /var/www/linkmanager/index.html
```

## License

MIT
