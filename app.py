#!/usr/bin/env python3
"""linkManager: API para administrar las tarjetas de una página de links estática.

- Guarda los links en un JSON y regenera las tarjetas de index.html entre los
  marcadores <!-- LINKS:START --> y <!-- LINKS:END -->.
- Cada cambio deja un backup con fecha de links.json e index.html.
- Busca iconos en internet (dashboard-icons, selfh.st, Simple Icons y
  Wikimedia Commons) y los descarga al elegirlos.
- Permite cambiar la contraseña del panel (archivo htpasswd que lee nginx).

Solo usa la biblioteca estándar de Python. La autenticación la hace nginx
(auth_basic); este servicio debe escuchar únicamente en localhost.
"""
import base64
import hmac
import html
import json
import os
import re
import shutil
import subprocess
import threading
import time
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


def backup():
    os.makedirs(BACKUP_DIR, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    for src in (DATA, INDEX):
        if os.path.exists(src):
            shutil.copy2(src, os.path.join(BACKUP_DIR, f"{os.path.basename(src)}.bkp-{stamp}"))
    files = sorted(os.listdir(BACKUP_DIR))
    for prefix in ("links.json.bkp-", "index.html.bkp-"):
        for f in [f for f in files if f.startswith(prefix)][:-BACKUPS_TO_KEEP]:
            os.remove(os.path.join(BACKUP_DIR, f))


def render(links):
    e = lambda s: html.escape(s, quote=True)
    cards = [
        f"""            <article class="aplicacion">
                <a href="{e(l['url'])}" target="_blank">
                    <img src="./images/{e(l['icono'])}" alt="{e(l['nombre'])}">
                    <h2>{e(l['nombre'])}</h2>
                    <p>{e(l['descripcion'])}</p>
                </a>
            </article>"""
        for l in links
    ]
    return (f'{START}\n        <section class="aplicaciones">\n'
            + "\n".join(cards)
            + f"\n        </section>\n        {END}")


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


# --- Validación y HTTP -------------------------------------------------------
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
                item = validate(self.body())
                item["id"] = uuid.uuid4().hex[:8]
                links.append(item)
                save(links)
                return 201, item
            if len(parts) >= 2 and parts[0] == "links":
                item = find(parts[1])
                if not item:
                    return 404, {"error": "Link no encontrado"}
                if method == "PUT" and len(parts) == 2:
                    item.update(validate(self.body(), item))
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


if __name__ == "__main__":
    init()
    ThreadingHTTPServer(LISTEN, Handler).serve_forever()
