#!/usr/bin/env python3
"""linkManager: API para administrar las tarjetas de una página de links estática.

- Guarda los links en un JSON y regenera las tarjetas de index.html entre los
  marcadores <!-- LINKS:START --> y <!-- LINKS:END -->.
- Cada cambio deja un backup con fecha de links.json e index.html.
- Busca iconos en internet (dashboard-icons, selfh.st, Simple Icons y
  Wikimedia Commons) y los descarga al elegirlos.
- Permite cambiar la contraseña del panel (archivo htpasswd que lee nginx).
- Integraciones con servidores DNS (por ahora Pi-hole v5 y v6), configuradas
  desde el panel: al dar de alta un link se puede crear su registro DNS local,
  A/AAAA si el destino es una IP o CNAME si es un nombre.

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
import shutil
import ssl
import subprocess
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
# partir de esa descripción. Para sumar otro servidor, agregá una entrada en
# PROVIDERS con sus funciones add(cfg, registro) y test(cfg).
_LABEL = r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?"
HOSTNAME_RE = re.compile(rf"(?:{_LABEL}\.)*{_LABEL}")
_pihole_versions = {}  # versión detectada por URL


def valid_hostname(name):
    return len(name) <= 253 and HOSTNAME_RE.fullmatch(name) is not None


def normalize_server_url(value):
    """Acepta una IP, un nombre o una URL; devuelve la URL base sin /admin."""
    v = str(value or "").strip().rstrip("/")
    if v.lower().endswith("/admin"):
        v = v[:-len("/admin")].rstrip("/")
    if v and "://" not in v:
        v = "http://" + v
    u = urllib.parse.urlsplit(v)
    if u.scheme not in ("http", "https") or not u.hostname:
        raise BadRequest("Dirección del servidor inválida (usá una IP, un nombre o una URL http/https)")
    return v


def server_request(cfg, method, path, body=None, headers=None):
    """Devuelve (status, json o texto). Los errores HTTP también se devuelven."""
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(cfg["url"] + path, data=data, method=method,
                                 headers={"User-Agent": USER_AGENT, **(headers or {})})
    if data is not None:
        req.add_header("Content-Type", "application/json")
    ctx = None
    if cfg["url"].startswith("https://") and not cfg.get("verify_tls", True):
        ctx = ssl.create_default_context()
        ctx.check_hostname, ctx.verify_mode = False, ssl.CERT_NONE
    try:
        with urllib.request.urlopen(req, timeout=15, context=ctx) as r:
            status, raw = r.status, r.read(1024 * 1024)
    except urllib.error.HTTPError as ex:
        status, raw = ex.code, ex.read(1024 * 1024)
    except (urllib.error.URLError, OSError) as ex:
        raise BadRequest(f"{cfg['nombre']}: no se pudo conectar ({getattr(ex, 'reason', ex)})")
    try:
        return status, json.loads(raw)
    except ValueError:
        return status, raw.decode("utf-8", "replace").strip()


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


def pihole_v6(cfg, method, path):
    status, res = server_request(cfg, "POST", "/api/auth", {"password": cfg["password"]})
    sid = res.get("session", {}).get("sid") if isinstance(res, dict) else None
    if not sid:
        raise BadRequest(f"{cfg['nombre']}: contraseña incorrecta o API no disponible (HTTP {status})")
    auth = {"X-FTL-SID": sid}
    try:
        status, res = server_request(cfg, method, path, headers=auth)
        if status >= 300:
            err = res.get("error", {}) if isinstance(res, dict) else {}
            detalle = ": ".join(str(x) for x in (err.get("message"), err.get("hint")) if x)
            raise BadRequest(f"{cfg['nombre']}: {detalle or f'error HTTP {status}'}")
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


def pihole_test(cfg):
    if pihole_version(cfg) == "6":
        dns = pihole_v6(cfg, "GET", "/api/config/dns").get("config", {}).get("dns", {})
        hosts, cnames = len(dns.get("hosts", [])), len(dns.get("cnameRecords", []))
    else:
        hosts = len(pihole_v5(cfg, {"customdns": "", "action": "get"}).get("data", []))
        cnames = len(pihole_v5(cfg, {"customcname": "", "action": "get"}).get("data", []))
    return (f"Conectado a Pi-hole v{pihole_version(cfg)}: "
            f"{hosts} registros DNS locales y {cnames} CNAME")


PROVIDERS = {
    "pihole": {
        "nombre": "Pi-hole",
        "registros": ["A", "AAAA", "CNAME"],
        "campos": [
            {"id": "url", "etiqueta": "IP o URL del servidor", "tipo": "text", "requerido": True,
             "formato": "url", "placeholder": "192.0.2.10  o  https://pi.hole",
             "ayuda": "Sin /admin. Si ponés solo la IP se usa http://."},
            {"id": "version", "etiqueta": "Versión", "tipo": "select", "defecto": "auto",
             "opciones": [["auto", "Detectar automáticamente"], ["6", "Pi-hole v6"], ["5", "Pi-hole v5"]]},
            {"id": "password", "etiqueta": "Contraseña o API key", "tipo": "password",
             "requerido": True, "secreto": True,
             "ayuda": "v6: contraseña web o app password. v5: token de la API "
                      "(Settings → API → Show API token) o la contraseña web."},
            {"id": "verify_tls", "etiqueta": "Verificar el certificado HTTPS", "tipo": "checkbox",
             "defecto": True, "ayuda": "Desmarcalo si usa un certificado autofirmado."},
        ],
        "add": pihole_add,
        "test": pihole_test,
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
    return [{"tipo": k, "nombre": p["nombre"], "registros": p["registros"], "campos": p["campos"]}
            for k, p in PROVIDERS.items()]


def public_integration(it):
    """Sin secretos: solo indica si están cargados."""
    out = dict(it)
    for campo in PROVIDERS.get(it["tipo"], {}).get("campos", []):
        if campo.get("secreto"):
            out[campo["id"]] = ""
            out[campo["id"] + "_cargado"] = bool(it.get(campo["id"]))
    out["tipo_nombre"] = PROVIDERS.get(it["tipo"], {}).get("nombre", it["tipo"])
    return out


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
        k = campo["id"]
        if campo["tipo"] == "checkbox":
            v = bool(body[k]) if k in body else (current or {}).get(k, campo.get("defecto", False))
        else:
            v = str(body.get(k) if body.get(k) is not None else "").strip()[:500]
            if not v and campo.get("secreto") and current:
                v = current.get(k, "")  # vacío al editar = conservar la contraseña
            elif not v and k not in body:
                v = (current or {}).get(k, campo.get("defecto", ""))
            if campo["tipo"] == "select" and v not in [o[0] for o in campo["opciones"]]:
                raise BadRequest(f"Valor inválido para '{campo['etiqueta']}'")
            if campo.get("requerido") and not v:
                raise BadRequest(f"El campo '{campo['etiqueta']}' es obligatorio")
            if campo.get("formato") == "url":
                v = normalize_server_url(v)
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
                    links.remove(item)
                    save(links)
                    return 200, {"ok": True}
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
