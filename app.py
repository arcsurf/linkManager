#!/usr/bin/env python3
"""linkManager: API para administrar las tarjetas de una página de links estática.

- Guarda los links en un JSON y regenera las tarjetas de index.html entre los
  marcadores <!-- LINKS:START --> y <!-- LINKS:END -->.
- Cada cambio deja un backup con fecha de links.json e index.html.
- Busca iconos en internet (dashboard-icons, selfh.st, Simple Icons y
  Wikimedia Commons) y los descarga al elegirlos.
- Permite cambiar la contraseña del panel (archivo htpasswd que lee nginx).
- Integraciones con servidores DNS configuradas desde el panel (Pi-hole, AdGuard
  Home, Technitium, PowerDNS, NextDNS, Control D y, por SSH, Unbound, dnsmasq,
  Blocky, Knot Resolver, CoreDNS, BIND9, dnscrypt-proxy y OpenWrt): al dar de
  alta un link se puede crear su registro DNS, A/AAAA si el destino es una IP o
  CNAME si es un nombre, y al borrar el link se borra el registro.

Solo usa la biblioteca estándar de Python. La autenticación la hace nginx
(auth_basic); este servicio debe escuchar únicamente en localhost.
"""
import base64
import hashlib
import hmac
import html
import ipaddress
import json
import os
import re
import shlex
import shutil
import ssl
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# --- Configuración (variables de entorno) -----------------------------------
WEB_ROOT = os.environ.get("LM_WEB_ROOT", "/var/www/linkmanager")
DATA = os.environ.get("LM_DATA", "/var/lib/linkmanager/links.json")
BACKUP_DIR = os.environ.get("LM_BACKUP_DIR", "/var/backups/linkmanager")
HTPASSWD = os.environ.get("LM_HTPASSWD", "/var/lib/linkmanager/htpasswd")
BACKUPS_TO_KEEP = int(os.environ.get("LM_BACKUPS_TO_KEEP", "200"))
_host, _port = os.environ.get("LM_LISTEN", "127.0.0.1:8091").rsplit(":", 1)
LISTEN = (_host, int(_port))

INDEX = os.path.join(WEB_ROOT, "index.html")
IMAGES = os.path.join(WEB_ROOT, "images")
ICON_EXT = {".png", ".svg", ".jpg", ".jpeg", ".webp", ".gif", ".ico"}
MIME_EXT = {"image/png": ".png", "image/svg+xml": ".svg", "image/jpeg": ".jpg",
            "image/webp": ".webp", "image/gif": ".gif", "image/x-icon": ".ico",
            "image/vnd.microsoft.icon": ".ico"}
MAX_ICON_BYTES = 2 * 1024 * 1024
START, END = "<!-- LINKS:START -->", "<!-- LINKS:END -->"
USER_AGENT = "linkManager/1.0 (+https://github.com/arcsurf/linkManager)"

# Hosts desde los que se permite descargar iconos (evita SSRF).
ICON_HOSTS = {"cdn.jsdelivr.net", "cdn.simpleicons.org",
              "upload.wikimedia.org", "thumb.wikimedia.org"}
DASHBOARD_ICONS = "https://cdn.jsdelivr.net/gh/homarr-labs/dashboard-icons"
SELFHST_ICONS = "https://cdn.jsdelivr.net/gh/selfhst/icons"
SIMPLE_ICONS_DATA = "https://cdn.jsdelivr.net/npm/simple-icons/data/simple-icons.json"
COMMONS_API = "https://commons.wikimedia.org/w/api.php"
INDEX_TTL = 24 * 3600

# Integraciones (servidores DNS) configuradas desde el panel. Por defecto, junto a links.json.
INTEGRATIONS = os.environ.get("LM_INTEGRATIONS",
                              os.path.join(os.path.dirname(DATA), "integraciones.json"))

lock = threading.Lock()


class BadRequest(Exception):
    pass


# --- Persistencia ------------------------------------------------------------
def load():
    with open(DATA) as f:
        return json.load(f)


def write_atomic(path, content, mode="w"):
    tmp = path + ".tmp"
    with open(tmp, mode) as f:
        f.write(content)
    if os.path.exists(path):
        shutil.copymode(path, tmp)
    os.replace(tmp, path)


def backup(paths=None):
    os.makedirs(BACKUP_DIR, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    for src in paths or (DATA, INDEX):
        if os.path.exists(src):
            # copy2 conserva los permisos (600 en integraciones.json)
            shutil.copy2(src, os.path.join(BACKUP_DIR, f"{os.path.basename(src)}.bkp-{stamp}"))
    files = sorted(os.listdir(BACKUP_DIR))
    for prefix in ("links.json.bkp-", "index.html.bkp-",
                   os.path.basename(INTEGRATIONS) + ".bkp-"):
        for f in [f for f in files if f.startswith(prefix)][:-BACKUPS_TO_KEEP]:
            os.remove(os.path.join(BACKUP_DIR, f))


def is_section(item):
    return item.get("tipo") == "seccion"


def render(items):
    """Tarjetas agrupadas: cada divisor abre una sección nueva con su título."""
    e = lambda s: html.escape(s, quote=True)
    blocks, cards = [], []

    def flush():
        if cards:
            blocks.append('        <section class="aplicaciones">\n'
                          + "\n".join(cards) + "\n        </section>")
            cards.clear()

    for it in items:
        if is_section(it):
            flush()
            blocks.append('        <section class="separador">\n'
                          f'            <h2>{e(it["titulo"])}</h2>\n'
                          '        </section>')
        else:
            cards.append(
                f"""            <article class="aplicacion">
                <a href="{e(it['url'])}" target="_blank">
                    <img src="./images/{e(it['icono'])}" alt="{e(it['nombre'])}">
                    <h2>{e(it['nombre'])}</h2>
                    <p>{e(it['descripcion'])}</p>
                </a>
            </article>""")
    flush()
    return f"{START}\n" + "\n".join(blocks) + f"\n        {END}"


def section_of(items, item):
    """id del divisor bajo el que está item ("" = grupo inicial sin título)."""
    current = ""
    for it in items:
        if it is item:
            return current
        if is_section(it):
            current = it["id"]
    return current


def place(items, item, section_id):
    """Mueve (o inserta) item al final del grupo de la sección indicada."""
    if item in items:
        items.remove(item)
    j = 0
    if section_id:
        j = next((i for i, it in enumerate(items) if it["id"] == section_id and is_section(it)), None)
        if j is None:
            raise BadRequest("La sección elegida no existe")
        j += 1
    while j < len(items) and not is_section(items[j]):
        j += 1
    items.insert(j, item)


def save(links):
    backup()
    write_atomic(DATA, json.dumps(links, ensure_ascii=False, indent=2) + "\n")
    with open(INDEX) as f:
        page = f.read()
    i, j = page.index(START), page.index(END) + len(END)
    write_atomic(INDEX, page[:i] + render(links) + page[j:])


# --- Iconos ------------------------------------------------------------------
def icons():
    return sorted(f for f in os.listdir(IMAGES)
                  if os.path.splitext(f)[1].lower() in ICON_EXT)


def slugify(text, default="icono"):
    return re.sub(r"[^A-Za-z0-9_-]+", "_", text).strip("_")[:60] or default


def write_icon(stem, ext, raw):
    if len(raw) > MAX_ICON_BYTES:
        raise BadRequest("El icono supera 2 MB")
    stem = slugify(stem)
    name, n = stem + ext, 1
    while os.path.exists(os.path.join(IMAGES, name)):
        name = f"{stem}_{n}{ext}"
        n += 1
    write_atomic(os.path.join(IMAGES, name), raw, "wb")
    os.chmod(os.path.join(IMAGES, name), 0o644)
    return name


class AllowedHostsRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        check_icon_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


_opener = urllib.request.build_opener(AllowedHostsRedirect)


def check_icon_url(url):
    u = urllib.parse.urlparse(url)
    if u.scheme != "https" or u.hostname not in ICON_HOSTS:
        raise BadRequest("Origen de icono no permitido")


def http_get(url, limit=MAX_ICON_BYTES, timeout=15):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with _opener.open(req, timeout=timeout) as r:
        raw = r.read(limit + 1)
        if len(raw) > limit:
            raise BadRequest("El archivo descargado es demasiado grande")
        return raw, r.headers.get_content_type()


def download_icon(url, nombre):
    check_icon_url(url)
    try:
        raw, ctype = http_get(url)
    except BadRequest:
        raise
    except Exception as ex:  # noqa: BLE001
        raise BadRequest(f"No se pudo descargar el icono: {ex}")
    ext = MIME_EXT.get(ctype)
    if not ext:
        raise BadRequest(f"El archivo descargado no es una imagen válida ({ctype})")
    return write_icon(nombre, ext, raw)


def store_icon(body, nombre):
    """Devuelve el nombre del icono: descargado, subido (base64) o existente."""
    if body.get("icono_url"):
        return download_icon(body["icono_url"], nombre)
    if body.get("icono_data"):
        stem, ext = os.path.splitext(os.path.basename(body.get("icono_nombre") or ""))
        ext = ext.lower()
        if ext not in ICON_EXT:
            raise BadRequest(f"Formato de icono no permitido ({', '.join(sorted(ICON_EXT))})")
        try:
            raw = base64.b64decode(body["icono_data"].split(",", 1)[-1], validate=True)
        except ValueError:
            raise BadRequest("Icono subido inválido")
        return write_icon(stem, ext, raw)
    name = body.get("icono") or ""
    if name not in icons():
        raise BadRequest("Elegí un icono: existente, subido o de internet")
    return name


# --- Búsqueda de iconos en internet -----------------------------------------
_index_cache = {}


def cached_json(url):
    hit = _index_cache.get(url)
    if hit and time.time() - hit[0] < INDEX_TTL:
        return hit[1]
    raw, _ = http_get(url, limit=20 * 1024 * 1024, timeout=30)
    data = json.loads(raw)
    _index_cache[url] = (time.time(), data)
    return data


def norm(text):
    return re.sub(r"[^a-z0-9]+", "", text.lower())


def rank(q, name):
    """0 = exacto, 1 = empieza igual, 2 = contiene, None = no coincide."""
    n = norm(name)
    if not n or not q:
        return None
    if n == q:
        return 0
    if n.startswith(q):
        return 1
    if q in n:
        return 2
    return None


def search_dashboard_icons(q):
    tree = cached_json(f"{DASHBOARD_ICONS}/tree.json")
    svgs = {os.path.splitext(f)[0] for f in tree.get("svg", [])}
    pngs = {os.path.splitext(f)[0] for f in tree.get("png", [])}
    out = []
    for stem in svgs | pngs:
        r = rank(q, stem)
        if r is not None:
            ext = "svg" if stem in svgs else "png"
            out.append((r, len(stem), {"fuente": "dashboard-icons", "titulo": stem,
                                       "url": f"{DASHBOARD_ICONS}/{ext}/{stem}.{ext}"}))
    return out


def search_selfhst(q):
    out = []
    for item in cached_json(f"{SELFHST_ICONS}/index.json"):
        ref = item.get("Reference", "")
        r = min((x for x in (rank(q, item.get("Name", "")), rank(q, ref)) if x is not None),
                default=None)
        if r is not None and ref:
            ext = "svg" if item.get("SVG") == "Yes" else "png"
            out.append((r, len(ref), {"fuente": "selfh.st", "titulo": item.get("Name", ref),
                                      "url": f"{SELFHST_ICONS}/{ext}/{ref}.{ext}"}))
    return out


def search_simple_icons(q):
    out = []
    for item in cached_json(SIMPLE_ICONS_DATA):
        slug = item.get("slug", "")
        r = min((x for x in (rank(q, item.get("title", "")), rank(q, slug)) if x is not None),
                default=None)
        if r is not None and slug:
            out.append((r, len(slug), {"fuente": "Simple Icons", "titulo": item["title"],
                                       "url": f"https://cdn.simpleicons.org/{slug}"}))
    return out


def search_commons(text, suffix=" logo"):
    params = urllib.parse.urlencode({
        "action": "query", "format": "json", "generator": "search",
        "gsrsearch": f"{text}{suffix}", "gsrnamespace": 6, "gsrlimit": 8,
        "prop": "imageinfo", "iiprop": "url|mime", "iiurlwidth": 256,
    })
    raw, _ = http_get(f"{COMMONS_API}?{params}", limit=2 * 1024 * 1024)
    pages = json.loads(raw).get("query", {}).get("pages", {}).values()
    out = []
    for p in sorted(pages, key=lambda p: p.get("index", 99)):
        info = (p.get("imageinfo") or [{}])[0]
        if info.get("mime") in MIME_EXT and info.get("thumburl"):
            out.append((3, p.get("index", 99), {
                "fuente": "Wikimedia Commons",
                "titulo": p["title"].removeprefix("File:"),
                "url": info["thumburl"].split("?", 1)[0],
            }))
    return out


MIN_RESULTS = 3


def search_icons(text, per_source=4, total=12):
    q = norm(text)
    if len(q) < 2:
        return []
    results, errors, seen = [], [], set()

    def run(fn, *args):
        try:
            for _, _, r in sorted(fn(*args), key=lambda t: (t[0], t[1]))[:per_source]:
                if r["url"] not in seen:
                    seen.add(r["url"])
                    results.append(r)
        except Exception as ex:  # noqa: BLE001
            errors.append(f"{fn.__name__}: {ex}")

    run(search_dashboard_icons, q)
    run(search_selfhst, q)
    run(search_simple_icons, q)
    run(search_commons, text)
    # Respaldos para nombres poco conocidos: Commons sin "logo" y palabra por palabra.
    if len(results) < MIN_RESULTS:
        run(search_commons, text, "")
    words = [w for w in re.split(r"\W+", text) if len(norm(w)) >= 3 and norm(w) != q]
    for w in sorted(words, key=len, reverse=True):
        if len(results) >= MIN_RESULTS:
            break
        for fn in (search_dashboard_icons, search_selfhst, search_simple_icons):
            run(fn, norm(w))
        run(search_commons, w)
    if errors and not results:
        raise BadRequest("No se pudo buscar en internet: " + "; ".join(errors))
    return results[:total]


# --- Contraseña --------------------------------------------------------------
def basic_auth(header):
    if not header or not header.startswith("Basic "):
        return None, None
    try:
        user, _, pw = base64.b64decode(header[6:]).decode().partition(":")
        return user, pw
    except ValueError:
        return None, None


def change_password(auth_header, body):
    user, current = basic_auth(auth_header)
    if not user:
        raise BadRequest("No se recibió la autenticación actual")
    if not hmac.compare_digest(str(body.get("actual", "")).encode(), current.encode()):
        raise BadRequest("La contraseña actual no es correcta")
    new = str(body.get("nueva", ""))
    if len(new) < 8:
        raise BadRequest("La nueva contraseña debe tener al menos 8 caracteres")
    if new != body.get("repetir"):
        raise BadRequest("Las contraseñas nuevas no coinciden")
    if new == current:
        raise BadRequest("La nueva contraseña es igual a la actual")
    hashed = subprocess.run(["openssl", "passwd", "-6", "-stdin"], input=new + "\n",
                            capture_output=True, text=True, check=True).stdout.strip()
    if os.path.exists(HTPASSWD):
        os.makedirs(BACKUP_DIR, exist_ok=True)
        shutil.copy2(HTPASSWD, os.path.join(
            BACKUP_DIR, f"htpasswd.bkp-{datetime.now():%Y%m%d-%H%M%S-%f}"))
    write_atomic(HTPASSWD, f"{user}:{hashed}\n")
    return {"ok": True, "usuario": user}


# --- Integraciones DNS -------------------------------------------------------
# Cada tipo de servidor DNS describe sus campos y el panel arma el formulario a
# partir de esa descripción. Hay tres familias:
#  - API del servidor: Pi-hole, AdGuard Home, Technitium, PowerDNS Authoritative.
#  - Nube: NextDNS y Control D.
#  - SSH, para los servidores que solo se configuran con archivos: linkManager
#    escribe sus registros en un archivo propio y recarga el servicio (Unbound,
#    dnsmasq, Blocky, Knot Resolver, CoreDNS, PowerDNS Recursor, dnscrypt-proxy);
#    BIND9 usa nsupdate y OpenWrt, uci.
# Para sumar otro servidor, agregá una entrada en PROVIDERS con sus funciones
# add(cfg, registro), delete(cfg, registro) y test(cfg).
_LABEL = r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?"
HOSTNAME_RE = re.compile(rf"(?:{_LABEL}\.)*{_LABEL}")
USER_RE = re.compile(r"[a-z_][a-z0-9_.-]{0,31}")
PATH_RE = re.compile(r"/[A-Za-z0-9._/@+-]{1,250}")
PROFILE_RE = re.compile(r"[A-Za-z0-9_-]{1,64}")
KNOWN_HOSTS = os.path.join(os.path.dirname(INTEGRATIONS), "known_hosts")
NEXTDNS_API = "https://api.nextdns.io"
CONTROLD_API = "https://api.controld.com"
FILE_HEADER = "# Registros DNS administrados por linkManager. No editar a mano."
_pihole_versions = {}  # versión detectada por URL


def valid_hostname(name):
    return len(name) <= 253 and HOSTNAME_RE.fullmatch(name) is not None


def valid_host_or_ip(value):
    try:
        ipaddress.ip_address(value)
        return True
    except ValueError:
        return valid_hostname(value)


def normalize_server_url(value, default_port=None):
    """Acepta una IP, un nombre o una URL; devuelve la URL base sin /admin."""
    v = str(value or "").strip().rstrip("/")
    if v.lower().endswith("/admin"):
        v = v[:-len("/admin")].rstrip("/")
    bare = "://" not in v
    if v and bare:
        v = "http://" + v
    u = urllib.parse.urlsplit(v)
    try:
        port = u.port
    except ValueError:
        port = -1
    if u.scheme not in ("http", "https") or not u.hostname or port == -1:
        raise BadRequest("Dirección del servidor inválida (usá una IP, un nombre o una URL http/https)")
    if bare and default_port and port is None and not u.path:
        v += f":{default_port}"
    return v


def server_request(cfg, method, path, body=None, headers=None, form=None, base=None):
    """Devuelve (status, json o texto). Los errores HTTP también se devuelven."""
    base = cfg["url"] if base is None else base
    data, ctype = None, None
    if form is not None:
        data, ctype = urllib.parse.urlencode(form, doseq=True).encode(), "application/x-www-form-urlencoded"
    elif body is not None:
        data, ctype = json.dumps(body).encode(), "application/json"
    req = urllib.request.Request(base + path, data=data, method=method,
                                 headers={"User-Agent": USER_AGENT, **(headers or {})})
    if ctype:
        req.add_header("Content-Type", ctype)
    ctx = None
    if base.startswith("https://") and not cfg.get("verify_tls", True):
        ctx = ssl.create_default_context()
        ctx.check_hostname, ctx.verify_mode = False, ssl.CERT_NONE
    try:
        with urllib.request.urlopen(req, timeout=15, context=ctx) as r:
            status, raw = r.status, r.read(4 * 1024 * 1024)
    except urllib.error.HTTPError as ex:
        status, raw = ex.code, ex.read(1024 * 1024)
    except (urllib.error.URLError, OSError) as ex:
        raise BadRequest(f"{cfg['nombre']}: no se pudo conectar ({getattr(ex, 'reason', ex)})")
    try:
        return status, json.loads(raw)
    except ValueError:
        return status, raw.decode("utf-8", "replace").strip()


def http_error(cfg, status, res, default=None):
    """Arma un mensaje legible a partir de los formatos de error de cada API."""
    msg = None
    if isinstance(res, dict):
        err = res.get("error")
        if isinstance(err, dict):
            msg = ": ".join(str(x) for x in (err.get("message"), err.get("hint")) if x)
        elif err:
            msg = str(err)
        if not msg and isinstance(res.get("errors"), list):
            msg = "; ".join(str(e.get("detail") or e.get("code") or e) if isinstance(e, dict) else str(e)
                            for e in res["errors"])
        msg = msg or res.get("errorMessage") or res.get("message")
    elif isinstance(res, str) and res and not res.lstrip().startswith("<"):
        msg = res[:300]
    return BadRequest(f"{cfg['nombre']}: {msg or default or f'error HTTP {status}'}")


def duplicate(cfg, dominio):
    return BadRequest(f"{cfg['nombre']}: ya existe un registro para {dominio}")


# Pi-hole ---------------------------------------------------------------------
def pihole_version(cfg):
    if cfg.get("version") in ("5", "6"):
        return cfg["version"]
    if cfg["url"] not in _pihole_versions:
        # v6 responde JSON con "session" en /api/auth; v5 no tiene esa ruta.
        _, res = server_request(cfg, "GET", "/api/auth")
        _pihole_versions[cfg["url"]] = "6" if isinstance(res, dict) and "session" in res else "5"
    return _pihole_versions[cfg["url"]]


def pihole_v5(cfg, query):
    pw = cfg["password"]
    # El token de la API v5 es el hash doble SHA-256 de la contraseña web.
    token = pw.lower() if re.fullmatch(r"[0-9a-fA-F]{64}", pw) else \
        hashlib.sha256(hashlib.sha256(pw.encode()).hexdigest().encode()).hexdigest()
    status, res = server_request(cfg, "GET", "/admin/api.php?" + urllib.parse.urlencode({**query, "auth": token}))
    if not res or "Not authorized" in str(res):  # sin auth válido v5 responde []
        raise BadRequest(f"{cfg['nombre']}: API key o contraseña incorrecta")
    if not isinstance(res, dict):
        raise BadRequest(f"{cfg['nombre']}: respuesta inesperada (HTTP {status}). ¿Es un Pi-hole?")
    return res


def pihole_v6(cfg, method, path, missing_ok=False):
    status, res = server_request(cfg, "POST", "/api/auth", {"password": cfg["password"]})
    sid = res.get("session", {}).get("sid") if isinstance(res, dict) else None
    if not sid:
        raise BadRequest(f"{cfg['nombre']}: contraseña incorrecta o API no disponible (HTTP {status})")
    auth = {"X-FTL-SID": sid}
    try:
        status, res = server_request(cfg, method, path, headers=auth)
        if status == 404 and missing_ok:
            return None
        if status >= 300:
            raise http_error(cfg, status, res)
        return res
    finally:
        try:  # Libera la sesión: Pi-hole v6 limita las sesiones simultáneas.
            server_request(cfg, "DELETE", "/api/auth", headers=auth)
        except BadRequest:
            pass


def pihole_add(cfg, rec):
    cname = rec["tipo"] == "CNAME"
    if pihole_version(cfg) == "6":
        item = f"{rec['dominio']},{rec['destino']}" if cname else f"{rec['destino']} {rec['dominio']}"
        path = "/api/config/dns/cnameRecords/" if cname else "/api/config/dns/hosts/"
        pihole_v6(cfg, "PUT", path + urllib.parse.quote(item, safe=""))
        return
    if cname:
        q = {"customcname": "", "action": "add", "domain": rec["dominio"], "target": rec["destino"]}
    else:
        q = {"customdns": "", "action": "add", "domain": rec["dominio"], "ip": rec["destino"]}
    res = pihole_v5(cfg, q)
    if not res.get("success"):
        raise BadRequest(f"{cfg['nombre']}: {html.unescape(str(res.get('message') or 'error desconocido'))}")


def pihole_delete(cfg, rec):
    """Borra el registro. Devuelve False si ya no existía en el servidor."""
    cname = rec["tipo"] == "CNAME"
    if pihole_version(cfg) == "6":
        item = f"{rec['dominio']},{rec['destino']}" if cname else f"{rec['destino']} {rec['dominio']}"
        path = "/api/config/dns/cnameRecords/" if cname else "/api/config/dns/hosts/"
        return pihole_v6(cfg, "DELETE", path + urllib.parse.quote(item, safe=""), missing_ok=True) is not None
    if cname:
        q = {"customcname": "", "action": "delete", "domain": rec["dominio"], "target": rec["destino"]}
    else:
        q = {"customdns": "", "action": "delete", "domain": rec["dominio"], "ip": rec["destino"]}
    res = pihole_v5(cfg, q)
    msg = html.unescape(str(res.get("message") or ""))
    if res.get("success"):
        return True
    if "does not exist" in msg:
        return False
    raise BadRequest(f"{cfg['nombre']}: {msg or 'error desconocido'}")


def pihole_test(cfg):
    if pihole_version(cfg) == "6":
        dns = pihole_v6(cfg, "GET", "/api/config/dns").get("config", {}).get("dns", {})
        hosts, cnames = len(dns.get("hosts", [])), len(dns.get("cnameRecords", []))
    else:
        hosts = len(pihole_v5(cfg, {"customdns": "", "action": "get"}).get("data", []))
        cnames = len(pihole_v5(cfg, {"customcname": "", "action": "get"}).get("data", []))
    return (f"Conectado a Pi-hole v{pihole_version(cfg)}: "
            f"{hosts} registros DNS locales y {cnames} CNAME")


# AdGuard Home (API /control, autenticación básica) ---------------------------
def adguard_call(cfg, method, path, body=None):
    cred = base64.b64encode(f"{cfg['usuario']}:{cfg['password']}".encode()).decode()
    status, res = server_request(cfg, method, "/control" + path, body,
                                 headers={"Authorization": "Basic " + cred})
    if status in (401, 403):
        raise BadRequest(f"{cfg['nombre']}: usuario o contraseña incorrectos")
    if status >= 300:
        raise http_error(cfg, status, res)
    return res


def adguard_rewrites(cfg):
    res = adguard_call(cfg, "GET", "/rewrite/list")
    return res if isinstance(res, list) else []


def adguard_add(cfg, rec):
    if any(r.get("domain") == rec["dominio"] for r in adguard_rewrites(cfg)):
        raise duplicate(cfg, rec["dominio"])
    adguard_call(cfg, "POST", "/rewrite/add", {"domain": rec["dominio"], "answer": rec["destino"]})


def adguard_delete(cfg, rec):
    if not any(r.get("domain") == rec["dominio"] and r.get("answer") == rec["destino"]
               for r in adguard_rewrites(cfg)):
        return False
    adguard_call(cfg, "POST", "/rewrite/delete", {"domain": rec["dominio"], "answer": rec["destino"]})
    return True


def adguard_test(cfg):
    status = adguard_call(cfg, "GET", "/status")
    version = status.get("version", "") if isinstance(status, dict) else ""
    return f"Conectado a AdGuard Home {version}: {len(adguard_rewrites(cfg))} reescrituras DNS".replace("  ", " ")


# Technitium DNS Server (API HTTP con token) ----------------------------------
def technitium_call(cfg, path, params=None):
    q = {"token": cfg["token"], **(params or {})}
    status, res = server_request(cfg, "GET", f"/api/{path}?" + urllib.parse.urlencode(q))
    if not isinstance(res, dict):
        raise http_error(cfg, status, res, "respuesta inesperada. ¿Es un Technitium DNS Server?")
    if res.get("status") == "invalid-token":
        raise BadRequest(f"{cfg['nombre']}: token inválido o vencido")
    if res.get("status") != "ok":
        raise http_error(cfg, status, res, str(res.get("status")))
    return res.get("response") or {}


def technitium_params(cfg, rec):
    p = {"domain": rec["dominio"], "type": rec["tipo"]}
    if cfg.get("zona"):
        p["zone"] = cfg["zona"]
    return p


def technitium_existing(cfg, rec):
    res = technitium_call(cfg, "zones/records/get", {**technitium_params(cfg, rec), "listZone": "false"})
    return [r for r in res.get("records", []) if r.get("name", "").lower().rstrip(".") == rec["dominio"]
            and r.get("type") in ("A", "AAAA", "CNAME")]


def technitium_add(cfg, rec):
    if technitium_existing(cfg, rec):
        raise duplicate(cfg, rec["dominio"])
    p = technitium_params(cfg, rec)
    if cfg.get("ttl"):
        p["ttl"] = cfg["ttl"]
    p["cname" if rec["tipo"] == "CNAME" else "ipAddress"] = rec["destino"]
    technitium_call(cfg, "zones/records/add", p)


def technitium_delete(cfg, rec):
    def matches(r):
        data = r.get("rData", {})
        value = data.get("cname") if rec["tipo"] == "CNAME" else data.get("ipAddress")
        return r.get("type") == rec["tipo"] and str(value or "").lower().rstrip(".") == rec["destino"]
    try:
        existing = technitium_existing(cfg, rec)
    except BadRequest as ex:
        if "No authoritative zone" in str(ex) or "zone was not found" in str(ex):
            return False
        raise
    if not any(matches(r) for r in existing):
        return False
    p = technitium_params(cfg, rec)
    if rec["tipo"] != "CNAME":
        p["ipAddress"] = rec["destino"]
    technitium_call(cfg, "zones/records/delete", p)
    return True


def technitium_test(cfg):
    zonas = technitium_call(cfg, "zones/list").get("zones", [])
    nombres = [z.get("name", "").lower() for z in zonas]
    if cfg.get("zona") and cfg["zona"] not in nombres:
        raise BadRequest(f"{cfg['nombre']}: la zona {cfg['zona']} no existe en el servidor")
    return f"Conectado a Technitium DNS Server: {len(zonas)} zonas"


# PowerDNS Authoritative (API REST con X-API-Key) -----------------------------
def pdns_zone(cfg):
    return cfg["zona"].rstrip(".") + "."


def pdns_call(cfg, method, body=None):
    path = (f"/api/v1/servers/{urllib.parse.quote(cfg['server_id'], safe='')}"
            f"/zones/{urllib.parse.quote(pdns_zone(cfg), safe='')}")
    status, res = server_request(cfg, method, path, body, headers={"X-API-Key": cfg["api_key"]})
    if status in (401, 403):
        raise BadRequest(f"{cfg['nombre']}: API key incorrecta")
    if status == 404:
        raise BadRequest(f"{cfg['nombre']}: la zona {pdns_zone(cfg)} no existe (o el server id es incorrecto)")
    if status >= 300:
        raise http_error(cfg, status, res)
    return res


def pdns_content(rec):
    return rec["destino"] + "." if rec["tipo"] == "CNAME" else rec["destino"]


def pdns_add(cfg, rec):
    zona, name = pdns_zone(cfg), rec["dominio"] + "."
    if name != zona and not name.endswith("." + zona):
        raise BadRequest(f"{cfg['nombre']}: {rec['dominio']} no pertenece a la zona {zona}")
    rrsets = pdns_call(cfg, "GET").get("rrsets", [])
    if any(r["name"] == name and r["type"] in ("A", "AAAA", "CNAME") for r in rrsets):
        raise duplicate(cfg, rec["dominio"])
    pdns_call(cfg, "PATCH", {"rrsets": [{
        "name": name, "type": rec["tipo"], "ttl": cfg.get("ttl") or 300, "changetype": "REPLACE",
        "records": [{"content": pdns_content(rec), "disabled": False}]}]})


def pdns_delete(cfg, rec):
    name, content = rec["dominio"] + ".", pdns_content(rec)
    rrset = next((r for r in pdns_call(cfg, "GET").get("rrsets", [])
                  if r["name"] == name and r["type"] == rec["tipo"]), None)
    records = (rrset or {}).get("records", [])
    if not any(x.get("content") == content for x in records):
        return False
    resto = [x for x in records if x.get("content") != content]
    change = {"name": name, "type": rec["tipo"], "changetype": "REPLACE" if resto else "DELETE"}
    if resto:
        change.update(ttl=rrset.get("ttl", 300), records=resto)
    pdns_call(cfg, "PATCH", {"rrsets": [change]})
    return True


def pdns_test(cfg):
    rrsets = pdns_call(cfg, "GET").get("rrsets", [])
    return f"Conectado a PowerDNS Authoritative: zona {pdns_zone(cfg)} con {len(rrsets)} conjuntos de registros"


# NextDNS (nube; los registros son "Rewrites" del perfil) ---------------------
def nextdns_call(cfg, method, path="", body=None):
    status, res = server_request(cfg, method, f"/profiles/{cfg['perfil']}{path}", body,
                                 headers={"X-Api-Key": cfg["api_key"]}, base=NEXTDNS_API)
    if status in (401, 403):
        raise BadRequest(f"{cfg['nombre']}: API key incorrecta")
    if status == 404:
        raise BadRequest(f"{cfg['nombre']}: el perfil {cfg['perfil']} no existe")
    if status >= 300 or (isinstance(res, dict) and res.get("errors")):
        raise http_error(cfg, status, res)
    return res.get("data") if isinstance(res, dict) else None


def nextdns_rewrites(cfg):
    return nextdns_call(cfg, "GET", "/rewrites") or []


def nextdns_add(cfg, rec):
    if any(r.get("name") == rec["dominio"] for r in nextdns_rewrites(cfg)):
        raise duplicate(cfg, rec["dominio"])
    nextdns_call(cfg, "POST", "/rewrites", {"name": rec["dominio"], "content": rec["destino"]})


def nextdns_delete(cfg, rec):
    r = next((r for r in nextdns_rewrites(cfg)
              if r.get("name") == rec["dominio"] and r.get("content") == rec["destino"]), None)
    if not r:
        return False
    nextdns_call(cfg, "DELETE", "/rewrites/" + urllib.parse.quote(str(r["id"]), safe=""))
    return True


def nextdns_test(cfg):
    perfil = nextdns_call(cfg, "GET") or {}
    return (f"Conectado a NextDNS: perfil \"{perfil.get('name', cfg['perfil'])}\" con "
            f"{len(perfil.get('rewrites', []))} rewrites")


# Control D (nube; los registros son reglas personalizadas SPOOF) -------------
def controld_call(cfg, method, path, form=None):
    status, res = server_request(cfg, method, path, form=form, base=CONTROLD_API,
                                 headers={"Authorization": "Bearer " + cfg["token"]})
    if status in (401, 403):
        raise BadRequest(f"{cfg['nombre']}: token de API incorrecto o sin permiso de escritura")
    if status >= 300 or (isinstance(res, dict) and res.get("success") is False):
        raise http_error(cfg, status, res)
    return res.get("body", {}) if isinstance(res, dict) else {}


def controld_rules(cfg):
    return controld_call(cfg, "GET", f"/profiles/{cfg['perfil']}/rules").get("rules", [])


def controld_add(cfg, rec):
    if any(r.get("PK") == rec["dominio"] for r in controld_rules(cfg)):
        raise duplicate(cfg, rec["dominio"])
    form = {"do": 2, "status": 1, "hostnames[]": [rec["dominio"]]}  # 2 = SPOOF
    form["via_v6" if rec["tipo"] == "AAAA" else "via"] = rec["destino"]
    controld_call(cfg, "POST", f"/profiles/{cfg['perfil']}/rules", form=form)


def controld_delete(cfg, rec):
    def matches(r):
        action = r.get("action", {})
        return r.get("PK") == rec["dominio"] and rec["destino"] in (action.get("via"), action.get("via_v6"))
    if not any(matches(r) for r in controld_rules(cfg)):
        return False
    controld_call(cfg, "DELETE", f"/profiles/{cfg['perfil']}/rules/"
                  + urllib.parse.quote(rec["dominio"], safe=""))
    return True


def controld_test(cfg):
    perfiles = controld_call(cfg, "GET", "/profiles").get("profiles", [])
    perfil = next((p for p in perfiles if p.get("PK") == cfg["perfil"]), None)
    if not perfil:
        raise BadRequest(f"{cfg['nombre']}: el perfil {cfg['perfil']} no existe en la cuenta")
    return f"Conectado a Control D: perfil \"{perfil.get('name', cfg['perfil'])}\" con {len(controld_rules(cfg))} reglas"


# SSH -------------------------------------------------------------------------
def ssh_exec(cfg, script, args=(), stdin=""):
    """Ejecuta un script sh en el servidor por SSH (clave privada de la integración)."""
    key = cfg.get("ssh_key", "")
    if not key:
        raise BadRequest(f"{cfg['nombre']}: falta la clave privada SSH")
    os.makedirs(os.path.dirname(KNOWN_HOSTS), exist_ok=True)
    remote = "sh -c " + shlex.quote(script) + " lm " + " ".join(shlex.quote(str(a)) for a in args)
    with tempfile.TemporaryDirectory(prefix="lm-ssh-") as d:
        kf = os.path.join(d, "id")
        with os.fdopen(os.open(kf, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w") as f:
            f.write(key.strip() + "\n")
        cmd = ["ssh", "-F", "/dev/null", "-i", kf, "-p", str(cfg.get("ssh_port") or 22),
               "-o", "BatchMode=yes", "-o", "IdentitiesOnly=yes", "-o", "ConnectTimeout=10",
               "-o", "StrictHostKeyChecking=accept-new", "-o", f"UserKnownHostsFile={KNOWN_HOSTS}",
               "-o", "LogLevel=ERROR", f"{cfg['ssh_user']}@{cfg['ssh_host']}", remote]
        try:
            r = subprocess.run(cmd, input=stdin, capture_output=True, text=True, timeout=90)
        except subprocess.TimeoutExpired:
            raise BadRequest(f"{cfg['nombre']}: el servidor no respondió a tiempo por SSH")
        except FileNotFoundError:
            raise BadRequest("Falta el cliente ssh donde corre linkManager (paquete openssh-client)")
    err = r.stderr.strip()
    if r.returncode == 255:
        detalle = err.splitlines()[-1] if err else "error de conexión"
        if "Permission denied" in err:
            detalle = "la clave no está autorizada (copiá la clave pública en authorized_keys del servidor)"
        elif "IDENTIFICATION HAS CHANGED" in err:
            detalle = (f"cambió la clave de host del servidor; si es esperado, borrá su línea de {KNOWN_HOSTS}")
        raise BadRequest(f"{cfg['nombre']}: no se pudo conectar por SSH: {detalle}")
    if r.returncode != 0:
        raise BadRequest(f"{cfg['nombre']}: {err or f'error {r.returncode} en el servidor'}")
    return r.stdout


def ssh_keygen(args, stdin=None):
    try:
        return subprocess.run(["ssh-keygen", *args], input=stdin, capture_output=True,
                              text=True, timeout=30)
    except FileNotFoundError:
        raise BadRequest("Falta ssh-keygen donde corre linkManager (paquete openssh-client)")


def generate_ssh_key():
    with tempfile.TemporaryDirectory(prefix="lm-key-") as d:
        kf = os.path.join(d, "id")
        r = ssh_keygen(["-q", "-t", "ed25519", "-N", "", "-C", "linkmanager", "-f", kf])
        if r.returncode != 0:
            raise BadRequest(f"No se pudo generar la clave: {r.stderr.strip()}")
        with open(kf) as f, open(kf + ".pub") as g:
            return {"privada": f.read(), "publica": g.read().strip()}


def ssh_public_key(private):
    """Valida la clave privada y devuelve su clave pública."""
    with tempfile.TemporaryDirectory(prefix="lm-key-") as d:
        kf = os.path.join(d, "id")
        with os.fdopen(os.open(kf, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w") as f:
            f.write(private.strip() + "\n")
        r = ssh_keygen(["-y", "-P", "", "-f", kf])
    if r.returncode != 0:
        raise BadRequest("La clave privada SSH no es válida o tiene contraseña (usá una sin contraseña)")
    return r.stdout.strip() + " linkmanager"


# Archivo propio de linkManager en el servidor, editado por SSH.
# write: escribe el archivo, ejecuta el comando de recarga y, si falla, restaura
# el archivo anterior y vuelve a recargar para dejar el servicio como estaba.
FILE_SCRIPT = r'''
F=$1; OP=$2; RELOAD=$3
case "$OP" in
read) [ -f "$F" ] && cat "$F"; exit 0 ;;
check)
  d=$(dirname "$F")
  [ -d "$d" ] || { echo "no existe la carpeta $d" >&2; exit 2; }
  [ -w "$d" ] || { echo "el usuario SSH no puede escribir en $d" >&2; exit 2; }
  if [ -n "$RELOAD" ]; then
    c=${RELOAD%% *}
    command -v "$c" >/dev/null 2>&1 || { echo "no se encontró el comando $c" >&2; exit 2; }
  fi
  exit 0 ;;
write)
  # Las copias temporales van a /tmp: dnsmasq, por ejemplo, carga todos los
  # archivos de su carpeta de configuración y leería también la copia.
  mkdir -p "$(dirname "$F")" || exit 2
  N=$(mktemp) || exit 2; P=""
  if [ -f "$F" ]; then P=$(mktemp) && cat "$F" > "$P" || exit 2; fi
  cat > "$N" && cat "$N" > "$F"; rc=$?; rm -f "$N"
  [ $rc -eq 0 ] || { [ -n "$P" ] && cat "$P" > "$F"; rm -f "$P"; exit 2; }
  if [ -z "$RELOAD" ] || out=$(sh -c "$RELOAD" 2>&1); then rm -f "$P"; exit 0; fi
  if [ -n "$P" ]; then cat "$P" > "$F"; rm -f "$P"; else rm -f "$F"; fi
  sh -c "$RELOAD" >/dev/null 2>&1
  echo "falló el comando de recarga ($RELOAD) y se restauró el archivo anterior: $out" >&2
  exit 3 ;;
esac
'''


def _unbound_line(r):
    destino = r["destino"] + "." if r["tipo"] == "CNAME" else r["destino"]
    return f'    local-data: "{r["dominio"]}. IN {r["tipo"]} {destino}"'


FILE_FORMATS = {
    # "IP nombre": Blocky, Knot Resolver, CoreDNS, PowerDNS Recursor
    "hosts": {"cabecera": [FILE_HEADER],
              "linea": lambda r: f"{r['destino']} {r['dominio']}",
              "nombres": lambda l: l.split()[1:]},
    "dnsmasq": {"cabecera": [FILE_HEADER],
                "linea": lambda r: (f"cname={r['dominio']},{r['destino']}" if r["tipo"] == "CNAME"
                                    else f"host-record={r['dominio']},{r['destino']}"),
                "nombres": lambda l: [l.split("=", 1)[1].split(",")[0]] if "=" in l else []},
    "unbound": {"cabecera": [FILE_HEADER, "server:"],
                "linea": _unbound_line,
                "nombres": lambda l: re.findall(r'local-data:\s*"(\S+?)\.?\s', l)},
    "cloaking": {"cabecera": [FILE_HEADER],  # dnscrypt-proxy: "nombre IP-o-nombre"
                 "linea": lambda r: f"{r['dominio']} {r['destino']}",
                 "nombres": lambda l: l.split()[:1]},
}


def file_format(cfg):
    return FILE_FORMATS[PROVIDERS[cfg["tipo"]]["formato"]]


def file_lines(cfg):
    return ssh_exec(cfg, FILE_SCRIPT, [cfg["archivo"], "read", ""]).splitlines()


def file_write(cfg, lines):
    ssh_exec(cfg, FILE_SCRIPT, [cfg["archivo"], "write", cfg.get("recarga", "")], "\n".join(lines) + "\n")


def file_records(cfg, lines):
    return [l for l in lines if l.strip() and not l.lstrip().startswith("#") and file_format(cfg)["nombres"](l)]


def file_add(cfg, rec):
    fmt, lines = file_format(cfg), file_lines(cfg)
    if any(rec["dominio"] in fmt["nombres"](l) for l in file_records(cfg, lines)):
        raise BadRequest(f"{cfg['nombre']}: ya existe un registro para {rec['dominio']} en {cfg['archivo']}")
    file_write(cfg, (lines or list(fmt["cabecera"])) + [fmt["linea"](rec)])


def file_delete(cfg, rec):
    lines, linea = file_lines(cfg), file_format(cfg)["linea"](rec)
    if linea not in lines:
        return False
    file_write(cfg, [l for l in lines if l != linea])
    return True


def file_test(cfg):
    ssh_exec(cfg, FILE_SCRIPT, [cfg["archivo"], "check", cfg.get("recarga", "")])
    n = len(file_records(cfg, file_lines(cfg)))
    return (f"Conectado por SSH a {cfg['ssh_host']}: {n} registros de linkManager en {cfg['archivo']}")


# BIND9: actualización dinámica (RFC 2136) con nsupdate en el propio servidor.
NSUPDATE_SCRIPT = r'''
KEY=$1; OP=$2; PORT=${3:-53}
command -v nsupdate >/dev/null 2>&1 || { echo "nsupdate no está instalado (paquete bind9-dnsutils o bind-utils)" >&2; exit 2; }
if [ "$OP" = check ]; then
  if [ -n "$KEY" ]; then [ -r "$KEY" ] || { echo "no se puede leer la clave $KEY" >&2; exit 2; }
  else
    for k in /run/named/session.key /var/run/named/session.key /var/cache/bind/session.key; do
      [ -r "$k" ] && exit 0
    done
    echo "no se encontró la clave de sesión de named (¿está corriendo BIND? ¿el usuario SSH puede leerla?)" >&2; exit 2
  fi
  exit 0
fi
if [ -n "$KEY" ]; then out=$(nsupdate -p "$PORT" -k "$KEY" 2>&1); else out=$(nsupdate -p "$PORT" -l 2>&1); fi
rc=$?; [ $rc -eq 0 ] || { echo "$out" | grep -v "Communication with ::1#" >&2; exit $rc; }
'''


def bind_args(cfg, op):
    return [cfg.get("clave_tsig", ""), op, cfg.get("puerto_dns") or 53]


def bind_update(cfg, rec, add):
    destino = rec["destino"] + "." if rec["tipo"] == "CNAME" else rec["destino"]
    name = rec["dominio"] + "."
    cmds = bind_header(cfg)
    if add:
        cmds += [f"prereq nxdomain {name}", f"update add {name} {cfg.get('ttl') or 300} {rec['tipo']} {destino}"]
    else:
        cmds += [f"prereq yxrrset {name} {rec['tipo']} {destino}", f"update delete {name} {rec['tipo']} {destino}"]
    try:
        ssh_exec(cfg, NSUPDATE_SCRIPT, bind_args(cfg, "update"), "\n".join(cmds + ["send", ""]))
    except BadRequest as ex:
        msg = str(ex)
        if "YXDOMAIN" in msg:
            raise duplicate(cfg, rec["dominio"])
        if "NXRRSET" in msg and not add:
            return False
        if "REFUSED" in msg:
            raise BadRequest(f"{cfg['nombre']}: BIND rechazó la actualización; la zona tiene que tener "
                             "update-policy local; (o allow-update con la clave TSIG)")
        if "NOTZONE" in msg or "enclosing zone" in msg or "NOTAUTH" in msg:
            raise BadRequest(f"{cfg['nombre']}: {rec['dominio']} no está en una zona de este BIND")
        raise
    return True


def bind_header(cfg):
    # Con nsupdate -l (sin clave TSIG) no se manda "server" (no lo admite) ni "zone"
    # (BIND rechaza la clave de sesión con BADKEY): nsupdate encuentra la zona solo.
    if not cfg.get("clave_tsig"):
        return []
    cmds = []
    if cfg.get("servidor_dns"):
        cmds.append(f"server {cfg['servidor_dns']} {cfg.get('puerto_dns') or 53}")
    if cfg.get("zona"):
        cmds.append(f"zone {cfg['zona']}")
    return cmds


def bind_add(cfg, rec):
    bind_update(cfg, rec, True)


def bind_delete(cfg, rec):
    return bind_update(cfg, rec, False)


def bind_test(cfg):
    ssh_exec(cfg, NSUPDATE_SCRIPT, bind_args(cfg, "check"))
    modo = f"clave {cfg['clave_tsig']}" if cfg.get("clave_tsig") else "clave de sesión local (nsupdate -l)"
    if not cfg.get("zona"):
        return (f"Conectado por SSH a {cfg['ssh_host']}: nsupdate disponible con {modo}. "
                "Indicá la zona para que la prueba también verifique que BIND acepta actualizaciones.")
    # Actualización vacía con un prerrequisito siempre cierto: verifica conexión y permisos sin cambiar nada.
    try:
        ssh_exec(cfg, NSUPDATE_SCRIPT, bind_args(cfg, "update"),
                 "\n".join(bind_header(cfg) + [f"prereq yxdomain {cfg['zona']}.", "send", ""]))
    except BadRequest as ex:
        if "REFUSED" in str(ex):
            raise BadRequest(f"{cfg['nombre']}: BIND rechazó la actualización; la zona {cfg['zona']} tiene "
                             "que tener update-policy local; (o allow-update con la clave TSIG)")
        raise
    return f"Conectado a BIND por SSH ({cfg['ssh_host']}): la zona {cfg['zona']} acepta actualizaciones con {modo}"


# OpenWrt: registros en el dnsmasq del router con uci (Network → DHCP and DNS).
UCI_SCRIPT = r'''
OP=$1; T=$2; N=$3; V=$4; RELOAD=$5
command -v uci >/dev/null 2>&1 || { echo "no es un OpenWrt (no está el comando uci)" >&2; exit 2; }
secs() { uci -X show dhcp | sed -n "s/^dhcp\.\([^.=]*\)=$1\$/\1/p"; }
find_secs() {  # $1 tipo de sección, $2 opción nombre, $3 opción valor
  for s in $(secs "$1"); do
    [ "$(uci -q get dhcp.$s.$2)" = "$N" ] || continue
    [ -z "$4" ] || [ "$(uci -q get dhcp.$s.$3)" = "$V" ] || continue
    echo "$s"
  done
}
case "$OP" in
check) echo "$(secs domain | wc -l) $(secs cname | wc -l)"; exit 0 ;;
add)
  if [ -n "$(find_secs domain name ip)$(find_secs cname cname target)" ]; then echo "YA_EXISTE" >&2; exit 4; fi
  if [ "$T" = CNAME ]; then s=$(uci add dhcp cname); uci set "dhcp.$s.cname=$N"; uci set "dhcp.$s.target=$V"
  else s=$(uci add dhcp domain); uci set "dhcp.$s.name=$N"; uci set "dhcp.$s.ip=$V"; fi ;;
delete)
  if [ "$T" = CNAME ]; then ss=$(find_secs cname cname target 1); else ss=$(find_secs domain name ip 1); fi
  [ -n "$ss" ] || { echo NO_EXISTE; exit 0; }
  for s in $ss; do uci delete "dhcp.$s"; done ;;
esac
uci commit dhcp || exit 2
[ -z "$RELOAD" ] || out=$(sh -c "$RELOAD" 2>&1) || { echo "falló el comando de recarga ($RELOAD): $out" >&2; exit 3; }
echo OK
'''


def openwrt_add(cfg, rec):
    try:
        ssh_exec(cfg, UCI_SCRIPT, ["add", rec["tipo"], rec["dominio"], rec["destino"], cfg.get("recarga", "")])
    except BadRequest as ex:
        if "YA_EXISTE" in str(ex):
            raise duplicate(cfg, rec["dominio"])
        raise


def openwrt_delete(cfg, rec):
    out = ssh_exec(cfg, UCI_SCRIPT, ["delete", rec["tipo"], rec["dominio"], rec["destino"], cfg.get("recarga", "")])
    return "NO_EXISTE" not in out


def openwrt_test(cfg):
    dominios, cnames = (ssh_exec(cfg, UCI_SCRIPT, ["check", "", "", "", ""]).split() + ["0", "0"])[:2]
    return f"Conectado por SSH a OpenWrt ({cfg['ssh_host']}): {dominios} nombres locales y {cnames} CNAME"


# Campos comunes -------------------------------------------------------------
def url_field(placeholder, puerto=None, ayuda="Si ponés solo la IP se usa http://"):
    return {"id": "url", "etiqueta": "IP o URL del servidor", "tipo": "text", "requerido": True,
            "formato": "url", "puerto": puerto, "placeholder": placeholder,
            "ayuda": ayuda + (f" y el puerto {puerto}." if puerto else ".")}


TLS_FIELD = {"id": "verify_tls", "etiqueta": "Verificar el certificado HTTPS", "tipo": "checkbox",
             "defecto": True, "ayuda": "Desmarcalo si usa un certificado autofirmado."}
TTL_FIELD = {"id": "ttl", "etiqueta": "TTL de los registros (segundos)", "tipo": "number",
             "defecto": 300, "min": 30, "max": 604800}


def ssh_fields(usuario="root"):
    return [
        {"id": "ssh_host", "etiqueta": "IP o nombre del servidor", "tipo": "text", "requerido": True,
         "formato": "host", "placeholder": "192.0.2.53"},
        {"id": "ssh_port", "etiqueta": "Puerto SSH", "tipo": "number", "defecto": 22, "min": 1, "max": 65535},
        {"id": "ssh_user", "etiqueta": "Usuario SSH", "tipo": "text", "defecto": usuario, "requerido": True,
         "formato": "usuario",
         "ayuda": "Tiene que poder escribir el archivo y ejecutar el comando de recarga (lo más simple es root)."},
        {"id": "ssh_key", "etiqueta": "Clave privada SSH", "tipo": "ssh_key", "requerido": True, "secreto": True,
         "ayuda": "Generá un par de claves acá y copiá la clave pública en ~/.ssh/authorized_keys del "
                  "servidor, o pegá una clave privada sin contraseña."},
    ]


def file_fields(ruta, recarga, ayuda_recarga=None):
    return [
        {"id": "archivo", "etiqueta": "Archivo de registros en el servidor", "tipo": "text", "defecto": ruta,
         "requerido": True, "formato": "ruta",
         "ayuda": "linkManager escribe sus registros solo en este archivo; no toca el resto de la configuración."},
        {"id": "recarga", "etiqueta": "Comando para aplicar los cambios", "tipo": "text", "defecto": recarga,
         "formato": "comando",
         "ayuda": ayuda_recarga or "Se ejecuta en el servidor después de cada cambio. Si falla, se "
                                   "restaura el archivo anterior. Vacío: no ejecuta nada."},
    ]


GRUPO_API, GRUPO_NUBE, GRUPO_SSH = "API del servidor", "Servicios en la nube", "Por SSH"
TODOS, SOLO_IP = ["A", "AAAA", "CNAME"], ["A", "AAAA"]


def _ssh_file_provider(nombre, formato, registros, ruta, recarga, instrucciones, ayuda_recarga=None):
    return {"nombre": nombre, "grupo": GRUPO_SSH, "registros": registros, "formato": formato,
            "instrucciones": instrucciones, "campos": ssh_fields() + file_fields(ruta, recarga, ayuda_recarga),
            "add": file_add, "delete": file_delete, "test": file_test}


PROVIDERS = {
    "pihole": {
        "nombre": "Pi-hole", "grupo": GRUPO_API, "registros": TODOS,
        "instrucciones": "Funciona con Pi-hole v5 y v6.",
        "campos": [
            url_field("192.0.2.10  o  https://pi.hole", ayuda="Sin /admin. Si ponés solo la IP se usa http://"),
            {"id": "version", "etiqueta": "Versión", "tipo": "select", "defecto": "auto",
             "opciones": [["auto", "Detectar automáticamente"], ["6", "Pi-hole v6"], ["5", "Pi-hole v5"]]},
            {"id": "password", "etiqueta": "Contraseña o API key", "tipo": "password",
             "requerido": True, "secreto": True,
             "ayuda": "v6: contraseña web o app password. v5: token de la API "
                      "(Settings → API → Show API token) o la contraseña web."},
            TLS_FIELD,
        ],
        "add": pihole_add, "delete": pihole_delete, "test": pihole_test,
    },
    "adguard": {
        "nombre": "AdGuard Home", "grupo": GRUPO_API, "registros": TODOS,
        "instrucciones": "Los registros se crean como reescrituras DNS (Filters → DNS rewrites).",
        "campos": [
            url_field("192.0.2.10  o  https://adguard.home",
                      ayuda="La dirección del panel web. Si ponés solo la IP se usa http://"),
            {"id": "usuario", "etiqueta": "Usuario", "tipo": "text", "requerido": True},
            {"id": "password", "etiqueta": "Contraseña", "tipo": "password", "requerido": True, "secreto": True},
            TLS_FIELD,
        ],
        "add": adguard_add, "delete": adguard_delete, "test": adguard_test,
    },
    "technitium": {
        "nombre": "Technitium DNS Server", "grupo": GRUPO_API, "registros": TODOS,
        "instrucciones": "Creá un token en Administration → Sessions → Create Token. El dominio del "
                         "link tiene que estar dentro de una zona primaria del servidor (por ejemplo home.arpa).",
        "campos": [
            url_field("192.0.2.10  o  https://dns.home.arpa:53443", puerto=5380),
            {"id": "token", "etiqueta": "Token de la API", "tipo": "password", "requerido": True, "secreto": True},
            {"id": "zona", "etiqueta": "Zona (opcional)", "tipo": "text", "formato": "zona",
             "placeholder": "home.arpa", "ayuda": "Vacío: se usa la zona más cercana al dominio."},
            TTL_FIELD, TLS_FIELD,
        ],
        "add": technitium_add, "delete": technitium_delete, "test": technitium_test,
    },
    "pdns_auth": {
        "nombre": "PowerDNS Authoritative", "grupo": GRUPO_API, "registros": TODOS,
        "instrucciones": "Necesita la API activada en pdns.conf: api=yes, api-key=..., webserver=yes y "
                         "webserver-allow-from con la IP de linkManager. Los dominios tienen que estar "
                         "dentro de la zona configurada.",
        "campos": [
            url_field("192.0.2.10  o  http://192.0.2.10:8081", puerto=8081),
            {"id": "api_key", "etiqueta": "API key", "tipo": "password", "requerido": True, "secreto": True},
            {"id": "zona", "etiqueta": "Zona", "tipo": "text", "requerido": True, "formato": "zona",
             "placeholder": "home.arpa"},
            {"id": "server_id", "etiqueta": "Server id", "tipo": "text", "defecto": "localhost",
             "requerido": True, "formato": "perfil"},
            TTL_FIELD, TLS_FIELD,
        ],
        "add": pdns_add, "delete": pdns_delete, "test": pdns_test,
    },
    "nextdns": {
        "nombre": "NextDNS", "grupo": GRUPO_NUBE, "registros": TODOS,
        "instrucciones": "La API key está al pie de my.nextdns.io/account. El ID del perfil es el que "
                         "aparece en la URL (my.nextdns.io/<id>/setup). Los registros se crean como Rewrites.",
        "campos": [
            {"id": "api_key", "etiqueta": "API key", "tipo": "password", "requerido": True, "secreto": True},
            {"id": "perfil", "etiqueta": "ID del perfil", "tipo": "text", "requerido": True,
             "formato": "perfil", "placeholder": "abc123"},
        ],
        "add": nextdns_add, "delete": nextdns_delete, "test": nextdns_test,
    },
    "controld": {
        "nombre": "Control D", "grupo": GRUPO_NUBE, "registros": TODOS,
        "instrucciones": "Creá un token de API con permiso de escritura en el panel de Control D. El ID "
                         "del perfil es el que aparece en su URL. Los registros se crean como reglas "
                         "personalizadas de tipo redirección (SPOOF).",
        "campos": [
            {"id": "token", "etiqueta": "Token de API", "tipo": "password", "requerido": True, "secreto": True},
            {"id": "perfil", "etiqueta": "ID del perfil", "tipo": "text", "requerido": True,
             "formato": "perfil", "placeholder": "123456abcdef"},
        ],
        "add": controld_add, "delete": controld_delete, "test": controld_test,
    },
    "blocky": _ssh_file_provider(
        "Blocky", "hosts", SOLO_IP, "/etc/blocky/linkmanager.hosts", "systemctl restart blocky",
        "Agregá el archivo en config.yml: hostsFile → sources: [/etc/blocky/linkmanager.hosts]. "
        "Solo registros A y AAAA."),
    "unbound": _ssh_file_provider(
        "Unbound", "unbound", TODOS, "/etc/unbound/unbound.conf.d/linkmanager.conf",
        "unbound-checkconf && systemctl reload unbound",
        "El archivo se incluye solo si tu unbound.conf incluye la carpeta unbound.conf.d (en Debian y "
        "Ubuntu ya viene así). Si no, agregá: include: \"/etc/unbound/unbound.conf.d/*.conf\"."),
    "dnsmasq": _ssh_file_provider(
        "dnsmasq", "dnsmasq", TODOS, "/etc/dnsmasq.d/linkmanager.conf",
        "dnsmasq --test && systemctl restart dnsmasq",
        "dnsmasq lee /etc/dnsmasq.d/ si tiene conf-dir=/etc/dnsmasq.d (en Debian y Ubuntu ya viene así). "
        "Para un CNAME, el destino tiene que ser un nombre que dnsmasq resuelva localmente "
        "(otro registro, /etc/hosts o DHCP)."),
    "knot": _ssh_file_provider(
        "Knot Resolver", "hosts", SOLO_IP, "/etc/knot-resolver/linkmanager.hosts",
        "systemctl restart kresd@1",
        "Knot Resolver 5: en kresd.conf agregá modules = { 'hints' } y "
        "hints.add_hosts('/etc/knot-resolver/linkmanager.hosts'). Knot Resolver 6: en config.yaml, "
        "local-data → addresses-files: [/etc/knot-resolver/linkmanager.hosts] y como comando de "
        "recarga kresctl reload. Solo registros A y AAAA. Ojo: Knot Resolver responde NXDOMAIN "
        "para dominios de uso especial (.home.arpa, .test, .local...) sin mirar el archivo; "
        "usá otro dominio, por ejemplo casa.lan."),
    "coredns": _ssh_file_provider(
        "CoreDNS", "hosts", SOLO_IP, "/etc/coredns/linkmanager.hosts", "",
        "En el Corefile agregá: hosts /etc/coredns/linkmanager.hosts { fallthrough }. CoreDNS relee el "
        "archivo solo cada pocos segundos, así que no hace falta comando de recarga. Solo registros A y AAAA.",
        "Opcional: CoreDNS relee el archivo solo."),
    "bind9": {
        "nombre": "BIND9", "grupo": GRUPO_SSH, "registros": TODOS,
        "instrucciones": "Usa nsupdate (actualización dinámica) en el servidor. La zona tiene que aceptar "
                         "actualizaciones: con update-policy local; alcanza con el usuario root y la clave de "
                         "sesión de named. Con una clave TSIG, indicá su archivo y usá allow-update o "
                         "update-policy con esa clave.",
        "campos": ssh_fields() + [
            {"id": "clave_tsig", "etiqueta": "Archivo de clave TSIG en el servidor (opcional)", "tipo": "text",
             "formato": "ruta", "placeholder": "/etc/bind/linkmanager.key",
             "ayuda": "Vacío: nsupdate -l con la clave de sesión local."},
            {"id": "servidor_dns", "etiqueta": "Servidor al que enviar la actualización (opcional)",
             "tipo": "text", "formato": "host", "placeholder": "127.0.0.1",
             "ayuda": "Solo con clave TSIG (vacío: el primario de la zona). Sin clave, nsupdate -l "
                      "actualiza siempre este mismo servidor."},
            {"id": "puerto_dns", "etiqueta": "Puerto DNS de BIND", "tipo": "number", "defecto": 53,
             "min": 1, "max": 65535},
            {"id": "zona", "etiqueta": "Zona (recomendada)", "tipo": "text", "formato": "zona",
             "placeholder": "home.arpa",
             "ayuda": "Con la zona, Probar conexión verifica que BIND acepta actualizaciones. "
                      "Vacío: nsupdate la detecta sola en cada registro."},
            TTL_FIELD,
        ],
        "add": bind_add, "delete": bind_delete, "test": bind_test,
    },
    "pdns_recursor": _ssh_file_provider(
        "PowerDNS Recursor", "hosts", SOLO_IP, "/etc/powerdns/linkmanager.hosts",
        "systemctl restart pdns-recursor",
        "En recursor.conf: export-etc-hosts=yes y etc-hosts-file=/etc/powerdns/linkmanager.hosts "
        "(en la configuración YAML de la 5.x: recursor.export_etc_hosts y recursor.etc_hosts_file). "
        "Solo registros A y AAAA."),
    "dnscrypt": _ssh_file_provider(
        "dnscrypt-proxy", "cloaking", TODOS, "/etc/dnscrypt-proxy/linkmanager-cloaking.txt",
        "systemctl restart dnscrypt-proxy",
        "En dnscrypt-proxy.toml: cloaking_rules = '/etc/dnscrypt-proxy/linkmanager-cloaking.txt'. "
        "Si el destino es un nombre, dnscrypt-proxy lo resuelve con sus servidores de internet y "
        "devuelve esas IPs (no un CNAME): usá un nombre público, no otro nombre del cloaking."),
    "openwrt": {
        "nombre": "OpenWrt (adblock, adblock-lean, https-dns-proxy)", "grupo": GRUPO_SSH, "registros": TODOS,
        "instrucciones": "Crea los registros en el dnsmasq del router, como Network → DHCP and DNS → "
                         "Hostnames y CNAME, con uci. adblock, adblock-lean y https-dns-proxy usan ese mismo "
                         "dnsmasq, así que sirve con cualquiera de ellos. Copiá la clave pública en "
                         "System → Administration → SSH-Keys.",
        "campos": ssh_fields() + [
            {"id": "recarga", "etiqueta": "Comando para aplicar los cambios", "tipo": "text",
             "defecto": "/etc/init.d/dnsmasq reload", "formato": "comando"},
        ],
        "add": openwrt_add, "delete": openwrt_delete, "test": openwrt_test,
    },
}


# Persistencia de integraciones (contienen contraseñas: archivo 600) ----------
def load_integrations():
    try:
        with open(INTEGRATIONS) as f:
            return json.load(f)
    except FileNotFoundError:
        return []


def save_integrations(items):
    backup([INTEGRATIONS])
    tmp = INTEGRATIONS + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(json.dumps(items, ensure_ascii=False, indent=2) + "\n")
    os.chmod(tmp, 0o600)
    os.replace(tmp, INTEGRATIONS)


def provider_types():
    return [{"tipo": k, "nombre": p["nombre"], "grupo": p["grupo"], "registros": p["registros"],
             "instrucciones": p.get("instrucciones", ""), "campos": p["campos"]}
            for k, p in PROVIDERS.items()]


def integration_summary(it):
    if it.get("url"):
        return it["url"]
    if it.get("ssh_host"):
        port = f":{it['ssh_port']}" if it.get("ssh_port") not in (None, 22) else ""
        return f"{it.get('ssh_user', '')}@{it['ssh_host']}{port}"
    if it.get("perfil"):
        return f"perfil {it['perfil']}"
    return ""


def public_integration(it):
    """Sin secretos: solo indica si están cargados."""
    prov = PROVIDERS.get(it["tipo"], {})
    out = dict(it)
    for campo in prov.get("campos", []):
        if campo.get("secreto"):
            out[campo["id"]] = ""
            out[campo["id"] + "_cargado"] = bool(it.get(campo["id"]))
    out["tipo_nombre"] = prov.get("nombre", it["tipo"])
    out["registros"] = prov.get("registros", [])
    out["resumen"] = integration_summary(it)
    return out


def validate_field(campo, v):
    """Valida y normaliza un campo de texto según su "formato"."""
    fmt, etiqueta = campo.get("formato"), campo["etiqueta"]
    if not v:
        return v
    if fmt == "url":
        return normalize_server_url(v, campo.get("puerto"))
    if fmt == "host":
        v = v.lower()
        if not valid_host_or_ip(v):
            raise BadRequest(f"'{etiqueta}' no es una IP ni un nombre válido")
    elif fmt == "zona":
        v = v.lower().rstrip(".")
        if not valid_hostname(v):
            raise BadRequest(f"'{etiqueta}' no es un nombre de zona válido")
    elif fmt == "usuario" and not USER_RE.fullmatch(v):
        raise BadRequest(f"'{etiqueta}' no es un nombre de usuario válido")
    elif fmt == "ruta" and not PATH_RE.fullmatch(v):
        raise BadRequest(f"'{etiqueta}' tiene que ser una ruta absoluta sin espacios")
    elif fmt == "perfil" and not PROFILE_RE.fullmatch(v):
        raise BadRequest(f"'{etiqueta}' solo admite letras, números, - y _")
    elif fmt == "comando" and ("\n" in v or "\r" in v):
        raise BadRequest(f"'{etiqueta}' tiene que ser una sola línea")
    return v


def validate_integration(body, current=None, others=()):
    tipo = current["tipo"] if current else str(body.get("tipo", ""))
    prov = PROVIDERS.get(tipo)
    if not prov:
        raise BadRequest("Tipo de integración desconocido")
    nombre = str(body.get("nombre", current["nombre"] if current else "")).strip()[:100]
    if not nombre:
        raise BadRequest("El nombre de la integración es obligatorio")
    if any(o["nombre"].lower() == nombre.lower() for o in others):
        raise BadRequest(f"Ya existe una integración llamada '{nombre}'")
    out = {"id": current["id"] if current else uuid.uuid4().hex[:8], "tipo": tipo, "nombre": nombre}
    for campo in prov["campos"]:
        k, t = campo["id"], campo["tipo"]
        if t == "checkbox":
            v = bool(body[k]) if k in body else (current or {}).get(k, campo.get("defecto", False))
        elif t == "number":
            raw = body.get(k) if k in body else (current or {}).get(k, campo.get("defecto"))
            if raw in (None, ""):
                raw = campo.get("defecto")
            try:
                v = int(raw)
            except (TypeError, ValueError):
                raise BadRequest(f"'{campo['etiqueta']}' tiene que ser un número")
            if not campo.get("min", v) <= v <= campo.get("max", v):
                raise BadRequest(f"'{campo['etiqueta']}' tiene que estar entre {campo['min']} y {campo['max']}")
        else:
            limite = 20000 if t == "ssh_key" else 500
            raw = body.get(k)
            v = (str(raw).replace("\r\n", "\n").strip() if t == "ssh_key" else str(raw).strip()) \
                if raw is not None else ""
            v = v[:limite]
            nuevo_secreto = bool(v) and campo.get("secreto")
            if not v and campo.get("secreto") and current:
                v = current.get(k, "")  # vacío al editar = conservar el secreto guardado
            elif not v and k not in body:
                v = (current or {}).get(k, campo.get("defecto", ""))
            if t == "select" and v not in [o[0] for o in campo["opciones"]]:
                raise BadRequest(f"Valor inválido para '{campo['etiqueta']}'")
            if campo.get("requerido") and not v:
                raise BadRequest(f"El campo '{campo['etiqueta']}' es obligatorio")
            if t == "ssh_key":
                out["ssh_publica"] = ssh_public_key(v) if nuevo_secreto else (current or {}).get("ssh_publica", "")
            else:
                v = validate_field(campo, v)
        out[k] = v
    return out


def find_integration(integrations, ident):
    return next((x for x in integrations if x["id"] == ident), None)


# Registro DNS del alta de un link --------------------------------------------
def validate_dns(dns, integrations):
    """Normaliza {integracion, dominio, destino, usar_en_url} y elige el tipo de registro."""
    if not isinstance(dns, dict):
        raise BadRequest("Datos de DNS inválidos")
    cfg = find_integration(integrations, str(dns.get("integracion", "")))
    if not cfg:
        raise BadRequest("Elegí un servidor DNS configurado en Configuración")
    dominio = str(dns.get("dominio", "")).strip().lower().rstrip(".")
    destino = str(dns.get("destino", "")).strip().lower().rstrip(".")
    if not dominio or not destino:
        raise BadRequest("Para el DNS completá el dominio y la IP o nombre de destino")
    if not valid_hostname(dominio):
        raise BadRequest(f"Dominio inválido: {dominio}")
    try:
        ip = ipaddress.ip_address(destino)
        tipo, destino = ("A" if ip.version == 4 else "AAAA"), str(ip)
    except ValueError:
        if not valid_hostname(destino):
            raise BadRequest(f"Destino inválido (no es IP ni nombre): {destino}")
        if destino == dominio:
            raise BadRequest("El CNAME no puede apuntar a sí mismo")
        tipo = "CNAME"
    if tipo not in PROVIDERS[cfg["tipo"]]["registros"]:
        raise BadRequest(f"{cfg['nombre']} no admite registros {tipo}")
    return cfg, {"tipo": tipo, "dominio": dominio, "destino": destino,
                 "usar_en_url": bool(dns.get("usar_en_url"))}


def integration_for_link(dns, integrations):
    ident = dns.get("integracion")
    if ident:
        cfg = find_integration(integrations, ident)
        if not cfg:
            raise BadRequest(f"La integración '{dns.get('servidor', ident)}' ya no existe")
        return cfg
    # Links creados antes de las integraciones no guardan en qué servidor están:
    # si hay una sola integración, es esa.
    if len(integrations) == 1:
        return integrations[0]
    raise BadRequest("No se sabe en qué servidor DNS está el registro de este link")


def url_with_host(url, host):
    """Reemplaza el host de la URL conservando esquema, puerto, ruta y query."""
    u = urllib.parse.urlsplit(url)
    netloc = host + (f":{u.port}" if u.port else "")
    return urllib.parse.urlunsplit(u._replace(netloc=netloc))


# --- Validación y HTTP -------------------------------------------------------
def validate_section(body):
    titulo = str(body.get("titulo", "")).strip()[:200]
    if not titulo:
        raise BadRequest("El título de la sección es obligatorio")
    return titulo


def validate(body, current=None):
    out = dict(current or {})
    for k in ("nombre", "descripcion", "url"):
        v = str(body.get(k, out.get(k, ""))).strip()
        if k != "descripcion" and not v:
            raise BadRequest(f"El campo '{k}' es obligatorio")
        out[k] = v[:300]
    if urllib.parse.urlparse(out["url"]).scheme not in ("http", "https"):
        raise BadRequest("La URL debe empezar con http:// o https://")
    if not current or any(body.get(k) for k in ("icono", "icono_data", "icono_url")):
        out["icono"] = store_icon(body, out["nombre"])
    return out


class Handler(BaseHTTPRequestHandler):
    def send_json(self, code, obj):
        raw = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(raw)

    def body(self):
        n = int(self.headers.get("Content-Length") or 0)
        if n > 4 * MAX_ICON_BYTES:
            raise BadRequest("Petición demasiado grande")
        try:
            return json.loads(self.rfile.read(n) or b"{}")
        except ValueError:
            raise BadRequest("JSON inválido")

    def route(self, method):
        url = urllib.parse.urlparse(self.path)
        parts = [p for p in url.path.split("/") if p][1:]  # sin "api"
        if method == "GET" and parts == ["buscar-icono"]:
            q = urllib.parse.parse_qs(url.query).get("q", [""])[0]
            return 200, search_icons(q[:100])
        if parts[:1] == ["integraciones"]:
            return self.route_integrations(method, parts[1:])
        if method == "POST" and parts == ["password"]:
            with lock:
                return 200, change_password(self.headers.get("Authorization"), self.body())
        with lock:
            links = load()
            find = lambda i: next((x for x in links if x["id"] == i), None)
            if method == "GET" and parts == ["links"]:
                return 200, links
            if method == "GET" and parts == ["iconos"]:
                return 200, icons()
            if method == "POST" and parts == ["links"]:
                body = self.body()
                if body.get("tipo") == "seccion":
                    item = {"id": uuid.uuid4().hex[:8], "tipo": "seccion",
                            "titulo": validate_section(body)}
                    links.append(item)
                else:
                    cfg = dns = None
                    if body.get("dns"):
                        cfg, dns = validate_dns(body["dns"], load_integrations())
                    item = validate(body)
                    if dns:
                        try:
                            PROVIDERS[cfg["tipo"]]["add"](cfg, dns)
                        except BadRequest:
                            # No dejar huérfano el icono recién descargado o subido.
                            if body.get("icono_url") or body.get("icono_data"):
                                try:
                                    os.remove(os.path.join(IMAGES, item["icono"]))
                                except OSError:
                                    pass
                            raise
                        if dns.pop("usar_en_url"):
                            item["url"] = url_with_host(item["url"], dns["dominio"])
                        item["dns"] = {"integracion": cfg["id"], "servidor": cfg["nombre"], **dns}
                    item["id"] = uuid.uuid4().hex[:8]
                    place(links, item, body.get("seccion") or "")
                save(links)
                return 201, item
            if len(parts) >= 2 and parts[0] == "links":
                item = find(parts[1])
                if not item:
                    return 404, {"error": "Link no encontrado"}
                if method == "PUT" and len(parts) == 2:
                    body = self.body()
                    if is_section(item):
                        item["titulo"] = validate_section(body)
                    else:
                        item.update(validate(body, item))
                        if "seccion" in body and (body["seccion"] or "") != section_of(links, item):
                            place(links, item, body["seccion"] or "")
                    save(links)
                    return 200, item
                if method == "DELETE" and len(parts) == 2:
                    # Con ?forzar=1 se borra solo el link y el registro DNS queda en el servidor.
                    forzar = urllib.parse.parse_qs(url.query).get("forzar") == ["1"]
                    dns_estado = None
                    if item.get("dns") and not forzar:
                        try:
                            cfg = integration_for_link(item["dns"], load_integrations())
                            existia = PROVIDERS[cfg["tipo"]]["delete"](cfg, item["dns"])
                        except BadRequest as ex:
                            return 409, {"error": f"No se pudo borrar el registro DNS: {ex}",
                                         "dns_fallo": True}
                        dns_estado = "borrado" if existia else "no_existia"
                    links.remove(item)
                    save(links)
                    return 200, {"ok": True, "dns": dns_estado}
                if method == "POST" and parts[2:] == ["mover"]:
                    d = 1 if self.body().get("dir", 1) > 0 else -1
                    i = links.index(item)
                    j = i + d
                    if 0 <= j < len(links):
                        links[i], links[j] = links[j], links[i]
                        save(links)
                    return 200, links
        return 404, {"error": "Ruta no encontrada"}

    def route_integrations(self, method, parts):
        if method == "GET" and parts == ["tipos"]:
            return 200, provider_types()
        if method == "POST" and parts == ["generar-clave"]:
            return 200, generate_ssh_key()
        if method == "POST" and parts == ["probar"]:
            # Probar lo que está en el formulario, sin guardarlo. Al editar,
            # una contraseña vacía usa la guardada.
            body = self.body()
            with lock:
                current = find_integration(load_integrations(), str(body.get("id", "")))
                cfg = validate_integration({**body, "nombre": body.get("nombre") or "Servidor DNS"}, current)
            return 200, {"ok": True, "mensaje": PROVIDERS[cfg["tipo"]]["test"](cfg)}
        with lock:
            items = load_integrations()
            if method == "GET" and not parts:
                return 200, [public_integration(x) for x in items]
            if method == "POST" and not parts:
                it = validate_integration(self.body(), others=items)
                items.append(it)
                save_integrations(items)
                return 201, public_integration(it)
            it = find_integration(items, parts[0]) if parts else None
            if not it:
                return 404, {"error": "Integración no encontrada"}
            if method == "PUT" and len(parts) == 1:
                nuevo = validate_integration(self.body(), it, [x for x in items if x is not it])
                items[items.index(it)] = nuevo
                save_integrations(items)
                return 200, public_integration(nuevo)
            if method == "DELETE" and len(parts) == 1:
                items.remove(it)
                save_integrations(items)
                return 200, {"ok": True}
            if not (method == "POST" and parts[1:] == ["probar"]):
                return 404, {"error": "Ruta no encontrada"}
            cfg = dict(it)
        return 200, {"ok": True, "mensaje": PROVIDERS[cfg["tipo"]]["test"](cfg)}

    def handle_method(self, method):
        try:
            code, obj = self.route(method)
        except BadRequest as ex:
            code, obj = 400, {"error": str(ex)}
        except Exception as ex:  # noqa: BLE001
            self.log_error("error: %r", ex)
            code, obj = 500, {"error": "Error interno, revisá el log del servicio"}
        self.send_json(code, obj)

    def do_GET(self): self.handle_method("GET")
    def do_POST(self): self.handle_method("POST")
    def do_PUT(self): self.handle_method("PUT")
    def do_DELETE(self): self.handle_method("DELETE")


def init():
    """Crea links.json vacío y la carpeta de iconos si faltan."""
    os.makedirs(os.path.dirname(DATA), exist_ok=True)
    os.makedirs(IMAGES, exist_ok=True)
    if not os.path.exists(DATA):
        write_atomic(DATA, "[]\n")
    import_legacy_pihole()


def import_legacy_pihole():
    """La versión anterior configuraba Pi-hole con variables LM_PIHOLE_*. Si están
    y todavía no hay integraciones, se importan una sola vez como integración."""
    env = os.environ
    if os.path.exists(INTEGRATIONS) or not (env.get("LM_PIHOLE_URL") and env.get("LM_PIHOLE_PASSWORD")):
        return
    try:
        it = validate_integration({
            "tipo": "pihole", "nombre": "Pi-hole", "url": env["LM_PIHOLE_URL"],
            "password": env["LM_PIHOLE_PASSWORD"],
            "version": env.get("LM_PIHOLE_VERSION", "auto").strip() or "auto",
            "verify_tls": env.get("LM_PIHOLE_VERIFY_TLS", "1") != "0"})
        save_integrations([it])
        print("Integración Pi-hole importada de LM_PIHOLE_*; ya podés quitar esas variables", flush=True)
    except (BadRequest, OSError) as ex:
        print(f"No se pudo importar la configuración LM_PIHOLE_*: {ex}", flush=True)


if __name__ == "__main__":
    init()
    ThreadingHTTPServer(LISTEN, Handler).serve_forever()
