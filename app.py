#!/usr/bin/env python3
"""linkManager: API to manage the cards of a static links page.

- Stores the links in a JSON file and regenerates the cards of index.html
  between the <!-- LINKS:START --> and <!-- LINKS:END --> markers.
- Every change leaves a timestamped backup of links.json and index.html.
- Searches icons online (dashboard-icons, selfh.st, Simple Icons and
  Wikimedia Commons) and downloads the chosen one.
- Changes the panel password (htpasswd file read by nginx).
- DNS server integrations configured from the panel (Pi-hole, AdGuard Home,
  Technitium, PowerDNS, NextDNS, Control D and, over SSH, Unbound, dnsmasq,
  Blocky, Knot Resolver, CoreDNS, BIND9, dnscrypt-proxy and OpenWrt): adding a
  link can create its DNS record (A/AAAA when the target is an IP, CNAME when
  it is a name) and deleting the link deletes the record.
- English and Spanish user interface; the language is a panel setting.

Only the Python standard library is used. Authentication is done by nginx
(auth_basic); this service must listen on localhost only.

Note: the JSON field names stored in links.json / integraciones.json and used
by the HTTP API ("nombre", "descripcion", "icono", "seccion"...) are kept in
Spanish for backward compatibility with existing installations.
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

# --- Configuration (environment variables) -----------------------------------
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

# Hosts icons may be downloaded from (prevents SSRF).
ICON_HOSTS = {"cdn.jsdelivr.net", "cdn.simpleicons.org",
              "upload.wikimedia.org", "thumb.wikimedia.org"}
DASHBOARD_ICONS = "https://cdn.jsdelivr.net/gh/homarr-labs/dashboard-icons"
SELFHST_ICONS = "https://cdn.jsdelivr.net/gh/selfhst/icons"
SIMPLE_ICONS_DATA = "https://cdn.jsdelivr.net/npm/simple-icons/data/simple-icons.json"
COMMONS_API = "https://commons.wikimedia.org/w/api.php"
INDEX_TTL = 24 * 3600

# DNS server integrations configured from the panel. Stored next to links.json by default.
INTEGRATIONS = os.environ.get("LM_INTEGRATIONS",
                              os.path.join(os.path.dirname(DATA), "integraciones.json"))
# Panel settings (language). Stored next to links.json by default.
SETTINGS = os.environ.get("LM_SETTINGS", os.path.join(os.path.dirname(DATA), "settings.json"))
DEFAULT_LANGUAGE = os.environ.get("LM_LANGUAGE", "en")

lock = threading.Lock()


class BadRequest(Exception):
    pass


# --- Internationalization -----------------------------------------------------
# Texts are written in English in the code; ES holds their Spanish translation.
# tr() returns the text in the language selected in the panel settings.
LANGUAGES = {"en": "English", "es": "Español"}
_settings_cache = {}


def load_settings():
    if not _settings_cache:
        settings = {"language": DEFAULT_LANGUAGE if DEFAULT_LANGUAGE in LANGUAGES else "en"}
        try:
            with open(SETTINGS) as f:
                settings.update(json.load(f))
        except (FileNotFoundError, ValueError):
            pass
        _settings_cache.update(settings)
    return dict(_settings_cache)


def save_settings(body):
    language = str(body.get("language", ""))
    if language not in LANGUAGES:
        raise BadRequest(tr("Unknown language"))
    settings = {**load_settings(), "language": language}
    write_atomic(SETTINGS, json.dumps(settings, indent=2) + "\n")
    _settings_cache.clear()
    _settings_cache.update(settings)
    return settings


def language():
    return load_settings().get("language", "en")


def tr(text, **kw):
    if language() == "es":
        text = ES.get(text, text)
    return text.format(**kw) if kw else text


# --- Persistence ---------------------------------------------------------------
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
            # copy2 keeps the permissions (600 on integraciones.json)
            shutil.copy2(src, os.path.join(BACKUP_DIR, f"{os.path.basename(src)}.bkp-{stamp}"))
    files = sorted(os.listdir(BACKUP_DIR))
    for prefix in ("links.json.bkp-", "index.html.bkp-",
                   os.path.basename(INTEGRATIONS) + ".bkp-"):
        for f in [f for f in files if f.startswith(prefix)][:-BACKUPS_TO_KEEP]:
            os.remove(os.path.join(BACKUP_DIR, f))


def is_section(item):
    return item.get("tipo") == "seccion"


def render(items):
    """Grouped cards: every divider starts a new section with its title."""
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
    """Id of the divider the item is under ("" = initial group without title)."""
    current = ""
    for it in items:
        if it is item:
            return current
        if is_section(it):
            current = it["id"]
    return current


def place(items, item, section_id):
    """Moves (or inserts) the item at the end of the given section's group."""
    if item in items:
        items.remove(item)
    j = 0
    if section_id:
        j = next((i for i, it in enumerate(items) if it["id"] == section_id and is_section(it)), None)
        if j is None:
            raise BadRequest(tr("The selected section does not exist"))
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


# --- Icons -------------------------------------------------------------------
def icons():
    return sorted(f for f in os.listdir(IMAGES)
                  if os.path.splitext(f)[1].lower() in ICON_EXT)


def slugify(text, default="icon"):
    return re.sub(r"[^A-Za-z0-9_-]+", "_", text).strip("_")[:60] or default


def write_icon(stem, ext, raw):
    if len(raw) > MAX_ICON_BYTES:
        raise BadRequest(tr("The icon is larger than 2 MB"))
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
        raise BadRequest(tr("Icon source not allowed"))


def http_get(url, limit=MAX_ICON_BYTES, timeout=15):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with _opener.open(req, timeout=timeout) as r:
        raw = r.read(limit + 1)
        if len(raw) > limit:
            raise BadRequest(tr("The downloaded file is too large"))
        return raw, r.headers.get_content_type()


def download_icon(url, name):
    check_icon_url(url)
    try:
        raw, ctype = http_get(url)
    except BadRequest:
        raise
    except Exception as ex:  # noqa: BLE001
        raise BadRequest(tr("Could not download the icon: {error}", error=ex))
    ext = MIME_EXT.get(ctype)
    if not ext:
        raise BadRequest(tr("The downloaded file is not a valid image ({type})", type=ctype))
    return write_icon(name, ext, raw)


def store_icon(body, name):
    """Returns the icon file name: downloaded, uploaded (base64) or existing."""
    if body.get("icono_url"):
        return download_icon(body["icono_url"], name)
    if body.get("icono_data"):
        stem, ext = os.path.splitext(os.path.basename(body.get("icono_nombre") or ""))
        ext = ext.lower()
        if ext not in ICON_EXT:
            raise BadRequest(tr("Icon format not allowed ({formats})", formats=", ".join(sorted(ICON_EXT))))
        try:
            raw = base64.b64decode(body["icono_data"].split(",", 1)[-1], validate=True)
        except ValueError:
            raise BadRequest(tr("Invalid uploaded icon"))
        return write_icon(stem, ext, raw)
    name = body.get("icono") or ""
    if name not in icons():
        raise BadRequest(tr("Choose an icon: existing, uploaded or from the internet"))
    return name


# --- Online icon search --------------------------------------------------------
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
    """0 = exact, 1 = same prefix, 2 = contains, None = no match."""
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
    # Fallbacks for little-known names: Commons without "logo" and word by word.
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
        raise BadRequest(tr("Could not search the internet: {errors}", errors="; ".join(errors)))
    return results[:total]


# --- Password ----------------------------------------------------------------
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
        raise BadRequest(tr("The current authentication was not received"))
    if not hmac.compare_digest(str(body.get("actual", "")).encode(), current.encode()):
        raise BadRequest(tr("The current password is not correct"))
    new = str(body.get("nueva", ""))
    if len(new) < 8:
        raise BadRequest(tr("The new password must be at least 8 characters long"))
    if new != body.get("repetir"):
        raise BadRequest(tr("The new passwords do not match"))
    if new == current:
        raise BadRequest(tr("The new password is the same as the current one"))
    hashed = subprocess.run(["openssl", "passwd", "-6", "-stdin"], input=new + "\n",
                            capture_output=True, text=True, check=True).stdout.strip()
    if os.path.exists(HTPASSWD):
        os.makedirs(BACKUP_DIR, exist_ok=True)
        shutil.copy2(HTPASSWD, os.path.join(
            BACKUP_DIR, f"htpasswd.bkp-{datetime.now():%Y%m%d-%H%M%S-%f}"))
    write_atomic(HTPASSWD, f"{user}:{hashed}\n")
    return {"ok": True, "usuario": user}


# --- DNS integrations ------------------------------------------------------------
# Every DNS server type describes its fields and the panel builds the form from
# that description. There are three families:
#  - Server API: Pi-hole, AdGuard Home, Technitium, PowerDNS Authoritative.
#  - Cloud: NextDNS and Control D.
#  - SSH, for servers that are only configured through files: linkManager writes
#    its records to a file of its own and reloads the service (Unbound, dnsmasq,
#    Blocky, Knot Resolver, CoreDNS, PowerDNS Recursor, dnscrypt-proxy);
#    BIND9 uses nsupdate and OpenWrt uses uci.
# To add another server, add an entry to PROVIDERS with its functions
# add(cfg, record), delete(cfg, record) and test(cfg).
_LABEL = r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?"
HOSTNAME_RE = re.compile(rf"(?:{_LABEL}\.)*{_LABEL}")
USER_RE = re.compile(r"[a-z_][a-z0-9_.-]{0,31}")
PATH_RE = re.compile(r"/[A-Za-z0-9._/@+-]{1,250}")
PROFILE_RE = re.compile(r"[A-Za-z0-9_-]{1,64}")
KNOWN_HOSTS = os.path.join(os.path.dirname(INTEGRATIONS), "known_hosts")
NEXTDNS_API = "https://api.nextdns.io"
CONTROLD_API = "https://api.controld.com"
FILE_HEADER = "# DNS records managed by linkManager. Do not edit by hand."
_pihole_versions = {}  # detected version per URL


def valid_hostname(name):
    return len(name) <= 253 and HOSTNAME_RE.fullmatch(name) is not None


def valid_host_or_ip(value):
    try:
        ipaddress.ip_address(value)
        return True
    except ValueError:
        return valid_hostname(value)


def normalize_server_url(value, default_port=None):
    """Accepts an IP, a name or a URL; returns the base URL without /admin."""
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
        raise BadRequest(tr("Invalid server address (use an IP, a name or an http/https URL)"))
    if bare and default_port and port is None and not u.path:
        v += f":{default_port}"
    return v


def server_request(cfg, method, path, body=None, headers=None, form=None, base=None):
    """Returns (status, json or text). HTTP errors are returned too."""
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
        raise BadRequest(tr("{name}: could not connect ({reason})", name=cfg["nombre"],
                            reason=getattr(ex, "reason", ex)))
    try:
        return status, json.loads(raw)
    except ValueError:
        return status, raw.decode("utf-8", "replace").strip()


def http_error(cfg, status, res, default=None):
    """Builds a readable message from the error formats of each API."""
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
    return BadRequest(f"{cfg['nombre']}: {msg or default or tr('HTTP error {status}', status=status)}")


def duplicate(cfg, domain):
    return BadRequest(tr("{name}: a record for {domain} already exists", name=cfg["nombre"], domain=domain))


# Pi-hole ---------------------------------------------------------------------
def pihole_version(cfg):
    if cfg.get("version") in ("5", "6"):
        return cfg["version"]
    if cfg["url"] not in _pihole_versions:
        # v6 answers /api/auth with JSON containing "session"; v5 has no such path.
        _, res = server_request(cfg, "GET", "/api/auth")
        _pihole_versions[cfg["url"]] = "6" if isinstance(res, dict) and "session" in res else "5"
    return _pihole_versions[cfg["url"]]


def pihole_v5(cfg, query):
    pw = cfg["password"]
    # The v5 API token is the double SHA-256 hash of the web password.
    token = pw.lower() if re.fullmatch(r"[0-9a-fA-F]{64}", pw) else \
        hashlib.sha256(hashlib.sha256(pw.encode()).hexdigest().encode()).hexdigest()
    status, res = server_request(cfg, "GET", "/admin/api.php?" + urllib.parse.urlencode({**query, "auth": token}))
    if not res or "Not authorized" in str(res):  # without valid auth v5 answers []
        raise BadRequest(tr("{name}: wrong API key or password", name=cfg["nombre"]))
    if not isinstance(res, dict):
        raise BadRequest(tr("{name}: unexpected response (HTTP {status}). Is it a Pi-hole?",
                            name=cfg["nombre"], status=status))
    return res


def pihole_v6(cfg, method, path, missing_ok=False):
    status, res = server_request(cfg, "POST", "/api/auth", {"password": cfg["password"]})
    sid = res.get("session", {}).get("sid") if isinstance(res, dict) else None
    if not sid:
        raise BadRequest(tr("{name}: wrong password or API not available (HTTP {status})",
                            name=cfg["nombre"], status=status))
    auth = {"X-FTL-SID": sid}
    try:
        status, res = server_request(cfg, method, path, headers=auth)
        if status == 404 and missing_ok:
            return None
        if status >= 300:
            raise http_error(cfg, status, res)
        return res
    finally:
        try:  # Release the session: Pi-hole v6 limits concurrent sessions.
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
        raise BadRequest(f"{cfg['nombre']}: {html.unescape(str(res.get('message') or tr('unknown error')))}")


def pihole_delete(cfg, rec):
    """Deletes the record. Returns False when it no longer existed on the server."""
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
    raise BadRequest(f"{cfg['nombre']}: {msg or tr('unknown error')}")


def pihole_test(cfg):
    if pihole_version(cfg) == "6":
        dns = pihole_v6(cfg, "GET", "/api/config/dns").get("config", {}).get("dns", {})
        hosts, cnames = len(dns.get("hosts", [])), len(dns.get("cnameRecords", []))
    else:
        hosts = len(pihole_v5(cfg, {"customdns": "", "action": "get"}).get("data", []))
        cnames = len(pihole_v5(cfg, {"customcname": "", "action": "get"}).get("data", []))
    return tr("Connected to Pi-hole v{version}: {hosts} local DNS records and {cnames} CNAME",
              version=pihole_version(cfg), hosts=hosts, cnames=cnames)


# AdGuard Home (/control API, basic authentication) ---------------------------
def adguard_call(cfg, method, path, body=None):
    cred = base64.b64encode(f"{cfg['user']}:{cfg['password']}".encode()).decode()
    status, res = server_request(cfg, method, "/control" + path, body,
                                 headers={"Authorization": "Basic " + cred})
    if status in (401, 403):
        raise BadRequest(tr("{name}: wrong user or password", name=cfg["nombre"]))
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
    return tr("Connected to AdGuard Home {version}: {count} DNS rewrites",
              version=version, count=len(adguard_rewrites(cfg))).replace("  ", " ")


# Technitium DNS Server (HTTP API with token) ---------------------------------
def technitium_call(cfg, path, params=None):
    q = {"token": cfg["token"], **(params or {})}
    status, res = server_request(cfg, "GET", f"/api/{path}?" + urllib.parse.urlencode(q))
    if not isinstance(res, dict):
        raise http_error(cfg, status, res, tr("unexpected response. Is it a Technitium DNS Server?"))
    if res.get("status") == "invalid-token":
        raise BadRequest(tr("{name}: invalid or expired token", name=cfg["nombre"]))
    if res.get("status") != "ok":
        raise http_error(cfg, status, res, str(res.get("status")))
    return res.get("response") or {}


def technitium_params(cfg, rec):
    p = {"domain": rec["dominio"], "type": rec["tipo"]}
    if cfg.get("zone"):
        p["zone"] = cfg["zone"]
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
    zones = technitium_call(cfg, "zones/list").get("zones", [])
    names = [z.get("name", "").lower() for z in zones]
    if cfg.get("zone") and cfg["zone"] not in names:
        raise BadRequest(tr("{name}: zone {zone} does not exist on the server",
                            name=cfg["nombre"], zone=cfg["zone"]))
    return tr("Connected to Technitium DNS Server: {count} zones", count=len(zones))


# PowerDNS Authoritative (REST API with X-API-Key) ----------------------------
def pdns_zone(cfg):
    return cfg["zone"].rstrip(".") + "."


def pdns_call(cfg, method, body=None):
    path = (f"/api/v1/servers/{urllib.parse.quote(cfg['server_id'], safe='')}"
            f"/zones/{urllib.parse.quote(pdns_zone(cfg), safe='')}")
    status, res = server_request(cfg, method, path, body, headers={"X-API-Key": cfg["api_key"]})
    if status in (401, 403):
        raise BadRequest(tr("{name}: wrong API key", name=cfg["nombre"]))
    if status == 404:
        raise BadRequest(tr("{name}: zone {zone} does not exist (or the server id is wrong)",
                            name=cfg["nombre"], zone=pdns_zone(cfg)))
    if status >= 300:
        raise http_error(cfg, status, res)
    return res


def pdns_content(rec):
    return rec["destino"] + "." if rec["tipo"] == "CNAME" else rec["destino"]


def pdns_add(cfg, rec):
    zone, name = pdns_zone(cfg), rec["dominio"] + "."
    if name != zone and not name.endswith("." + zone):
        raise BadRequest(tr("{name}: {domain} does not belong to zone {zone}",
                            name=cfg["nombre"], domain=rec["dominio"], zone=zone))
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
    rest = [x for x in records if x.get("content") != content]
    change = {"name": name, "type": rec["tipo"], "changetype": "REPLACE" if rest else "DELETE"}
    if rest:
        change.update(ttl=rrset.get("ttl", 300), records=rest)
    pdns_call(cfg, "PATCH", {"rrsets": [change]})
    return True


def pdns_test(cfg):
    rrsets = pdns_call(cfg, "GET").get("rrsets", [])
    return tr("Connected to PowerDNS Authoritative: zone {zone} with {count} record sets",
              zone=pdns_zone(cfg), count=len(rrsets))


# NextDNS (cloud; records are profile "Rewrites") ------------------------------
def nextdns_call(cfg, method, path="", body=None):
    status, res = server_request(cfg, method, f"/profiles/{cfg['profile']}{path}", body,
                                 headers={"X-Api-Key": cfg["api_key"]}, base=NEXTDNS_API)
    if status in (401, 403):
        raise BadRequest(tr("{name}: wrong API key", name=cfg["nombre"]))
    if status == 404:
        raise BadRequest(tr("{name}: profile {profile} does not exist", name=cfg["nombre"], profile=cfg["profile"]))
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
    profile = nextdns_call(cfg, "GET") or {}
    return tr('Connected to NextDNS: profile "{profile}" with {count} rewrites',
              profile=profile.get("name", cfg["profile"]), count=len(profile.get("rewrites", [])))


# Control D (cloud; records are SPOOF custom rules) ----------------------------
def controld_call(cfg, method, path, form=None):
    status, res = server_request(cfg, method, path, form=form, base=CONTROLD_API,
                                 headers={"Authorization": "Bearer " + cfg["token"]})
    if status in (401, 403):
        raise BadRequest(tr("{name}: wrong API token or no write permission", name=cfg["nombre"]))
    if status >= 300 or (isinstance(res, dict) and res.get("success") is False):
        raise http_error(cfg, status, res)
    return res.get("body", {}) if isinstance(res, dict) else {}


def controld_rules(cfg):
    return controld_call(cfg, "GET", f"/profiles/{cfg['profile']}/rules").get("rules", [])


def controld_add(cfg, rec):
    if any(r.get("PK") == rec["dominio"] for r in controld_rules(cfg)):
        raise duplicate(cfg, rec["dominio"])
    form = {"do": 2, "status": 1, "hostnames[]": [rec["dominio"]]}  # 2 = SPOOF
    form["via_v6" if rec["tipo"] == "AAAA" else "via"] = rec["destino"]
    controld_call(cfg, "POST", f"/profiles/{cfg['profile']}/rules", form=form)


def controld_delete(cfg, rec):
    def matches(r):
        action = r.get("action", {})
        return r.get("PK") == rec["dominio"] and rec["destino"] in (action.get("via"), action.get("via_v6"))
    if not any(matches(r) for r in controld_rules(cfg)):
        return False
    controld_call(cfg, "DELETE", f"/profiles/{cfg['profile']}/rules/"
                  + urllib.parse.quote(rec["dominio"], safe=""))
    return True


def controld_test(cfg):
    profiles = controld_call(cfg, "GET", "/profiles").get("profiles", [])
    profile = next((p for p in profiles if p.get("PK") == cfg["profile"]), None)
    if not profile:
        raise BadRequest(tr("{name}: profile {profile} does not exist in the account",
                            name=cfg["nombre"], profile=cfg["profile"]))
    return tr('Connected to Control D: profile "{profile}" with {count} rules',
              profile=profile.get("name", cfg["profile"]), count=len(controld_rules(cfg)))


# SSH -------------------------------------------------------------------------
# The remote scripts report errors as "LMERR <code> <argument>" (and optional
# detail lines); script_error() turns them into translated messages.
SCRIPT_ERRORS = {
    "no_dir": "folder {arg} does not exist",
    "not_writable": "the SSH user cannot write to {arg}",
    "no_command": "command {arg} was not found",
    "reload_failed": "the reload command ({arg}) failed and the previous file was restored",
    "reload_failed_uci": "the reload command ({arg}) failed",
    "no_nsupdate": "nsupdate is not installed (package bind9-dnsutils or bind-utils)",
    "no_key": "cannot read key file {arg}",
    "no_session_key": "named's session key was not found (is BIND running? can the SSH user read it?)",
    "not_openwrt": "it is not an OpenWrt (the uci command is missing)",
}


def script_error(err):
    lines = err.strip().splitlines()
    m = re.match(r"LMERR (\w+) ?(.*)", lines[0]) if lines else None
    if not m or m[1] not in SCRIPT_ERRORS:
        return err.strip()
    msg = tr(SCRIPT_ERRORS[m[1]], arg=m[2])
    detail = "\n".join(lines[1:]).strip()
    return f"{msg}: {detail}" if detail else msg


def ssh_exec(cfg, script, args=(), stdin=""):
    """Runs a sh script on the server over SSH (with the integration's private key)."""
    key = cfg.get("ssh_key", "")
    if not key:
        raise BadRequest(tr("{name}: the SSH private key is missing", name=cfg["nombre"]))
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
            raise BadRequest(tr("{name}: the server did not answer over SSH in time", name=cfg["nombre"]))
        except FileNotFoundError:
            raise BadRequest(tr("The ssh client is missing where linkManager runs (package openssh-client)"))
    err = r.stderr.strip()
    if r.returncode == 255:
        detail = err.splitlines()[-1] if err else tr("connection error")
        if "Permission denied" in err:
            detail = tr("the key is not authorized (copy the public key to the server's authorized_keys)")
        elif "IDENTIFICATION HAS CHANGED" in err:
            detail = tr("the server's host key changed; if that is expected, delete its line from {file}",
                        file=KNOWN_HOSTS)
        raise BadRequest(tr("{name}: could not connect over SSH: {detail}", name=cfg["nombre"], detail=detail))
    if r.returncode != 0:
        raise BadRequest(f"{cfg['nombre']}: "
                         + (script_error(err) if err else tr("error {code} on the server", code=r.returncode)))
    return r.stdout


def ssh_keygen(args, stdin=None):
    try:
        return subprocess.run(["ssh-keygen", *args], input=stdin, capture_output=True,
                              text=True, timeout=30)
    except FileNotFoundError:
        raise BadRequest(tr("ssh-keygen is missing where linkManager runs (package openssh-client)"))


def generate_ssh_key():
    with tempfile.TemporaryDirectory(prefix="lm-key-") as d:
        kf = os.path.join(d, "id")
        r = ssh_keygen(["-q", "-t", "ed25519", "-N", "", "-C", "linkmanager", "-f", kf])
        if r.returncode != 0:
            raise BadRequest(tr("Could not generate the key: {error}", error=r.stderr.strip()))
        with open(kf) as f, open(kf + ".pub") as g:
            return {"privada": f.read(), "publica": g.read().strip()}


def ssh_public_key(private):
    """Validates the private key and returns its public key."""
    with tempfile.TemporaryDirectory(prefix="lm-key-") as d:
        kf = os.path.join(d, "id")
        with os.fdopen(os.open(kf, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w") as f:
            f.write(private.strip() + "\n")
        r = ssh_keygen(["-y", "-P", "", "-f", kf])
    if r.returncode != 0:
        raise BadRequest(tr("The SSH private key is not valid or has a passphrase (use one without passphrase)"))
    return r.stdout.strip() + " linkmanager"


# linkManager's own file on the server, edited over SSH.
# write: writes the file and runs the reload command; if it fails, restores the
# previous file and reloads again to leave the service as it was.
FILE_SCRIPT = r'''
F=$1; OP=$2; RELOAD=$3
case "$OP" in
read) [ -f "$F" ] && cat "$F"; exit 0 ;;
check)
  d=$(dirname "$F")
  [ -d "$d" ] || { echo "LMERR no_dir $d" >&2; exit 2; }
  [ -w "$d" ] || { echo "LMERR not_writable $d" >&2; exit 2; }
  if [ -n "$RELOAD" ]; then
    c=${RELOAD%% *}
    command -v "$c" >/dev/null 2>&1 || { echo "LMERR no_command $c" >&2; exit 2; }
  fi
  exit 0 ;;
write)
  # Temporary copies go to /tmp: dnsmasq, for instance, loads every file in
  # its configuration folder and would also read the copy.
  mkdir -p "$(dirname "$F")" || exit 2
  N=$(mktemp) || exit 2; P=""
  if [ -f "$F" ]; then P=$(mktemp) && cat "$F" > "$P" || exit 2; fi
  cat > "$N" && cat "$N" > "$F"; rc=$?; rm -f "$N"
  [ $rc -eq 0 ] || { [ -n "$P" ] && cat "$P" > "$F"; rm -f "$P"; exit 2; }
  if [ -z "$RELOAD" ] || out=$(sh -c "$RELOAD" 2>&1); then rm -f "$P"; exit 0; fi
  if [ -n "$P" ]; then cat "$P" > "$F"; rm -f "$P"; else rm -f "$F"; fi
  sh -c "$RELOAD" >/dev/null 2>&1
  printf 'LMERR reload_failed %s\n%s\n' "$RELOAD" "$out" >&2
  exit 3 ;;
esac
'''


def _unbound_line(r):
    target = r["destino"] + "." if r["tipo"] == "CNAME" else r["destino"]
    return f'    local-data: "{r["dominio"]}. IN {r["tipo"]} {target}"'


FILE_FORMATS = {
    # "IP name": Blocky, Knot Resolver, CoreDNS, PowerDNS Recursor
    "hosts": {"header": [FILE_HEADER],
              "line": lambda r: f"{r['destino']} {r['dominio']}",
              "names": lambda l: l.split()[1:]},
    "dnsmasq": {"header": [FILE_HEADER],
                "line": lambda r: (f"cname={r['dominio']},{r['destino']}" if r["tipo"] == "CNAME"
                                   else f"host-record={r['dominio']},{r['destino']}"),
                "names": lambda l: [l.split("=", 1)[1].split(",")[0]] if "=" in l else []},
    "unbound": {"header": [FILE_HEADER, "server:"],
                "line": _unbound_line,
                "names": lambda l: re.findall(r'local-data:\s*"(\S+?)\.?\s', l)},
    "cloaking": {"header": [FILE_HEADER],  # dnscrypt-proxy: "name IP-or-name"
                 "line": lambda r: f"{r['dominio']} {r['destino']}",
                 "names": lambda l: l.split()[:1]},
}


def file_format(cfg):
    return FILE_FORMATS[PROVIDERS[cfg["tipo"]]["file_format"]]


def file_lines(cfg):
    return ssh_exec(cfg, FILE_SCRIPT, [cfg["file"], "read", ""]).splitlines()


def file_write(cfg, lines):
    ssh_exec(cfg, FILE_SCRIPT, [cfg["file"], "write", cfg.get("reload_command", "")], "\n".join(lines) + "\n")


def file_records(cfg, lines):
    return [l for l in lines if l.strip() and not l.lstrip().startswith("#") and file_format(cfg)["names"](l)]


def file_add(cfg, rec):
    fmt, lines = file_format(cfg), file_lines(cfg)
    if any(rec["dominio"] in fmt["names"](l) for l in file_records(cfg, lines)):
        raise BadRequest(tr("{name}: a record for {domain} already exists in {file}",
                            name=cfg["nombre"], domain=rec["dominio"], file=cfg["file"]))
    file_write(cfg, (lines or list(fmt["header"])) + [fmt["line"](rec)])


def file_delete(cfg, rec):
    lines, line = file_lines(cfg), file_format(cfg)["line"](rec)
    if line not in lines:
        return False
    file_write(cfg, [l for l in lines if l != line])
    return True


def file_test(cfg):
    ssh_exec(cfg, FILE_SCRIPT, [cfg["file"], "check", cfg.get("reload_command", "")])
    n = len(file_records(cfg, file_lines(cfg)))
    return tr("Connected over SSH to {host}: {count} linkManager records in {file}",
              host=cfg["ssh_host"], count=n, file=cfg["file"])


# BIND9: dynamic update (RFC 2136) with nsupdate on the server itself.
NSUPDATE_SCRIPT = r'''
KEY=$1; OP=$2; PORT=${3:-53}
command -v nsupdate >/dev/null 2>&1 || { echo "LMERR no_nsupdate" >&2; exit 2; }
if [ "$OP" = check ]; then
  if [ -n "$KEY" ]; then [ -r "$KEY" ] || { echo "LMERR no_key $KEY" >&2; exit 2; }
  else
    for k in /run/named/session.key /var/run/named/session.key /var/cache/bind/session.key; do
      [ -r "$k" ] && exit 0
    done
    echo "LMERR no_session_key" >&2; exit 2
  fi
  exit 0
fi
if [ -n "$KEY" ]; then out=$(nsupdate -p "$PORT" -k "$KEY" 2>&1); else out=$(nsupdate -p "$PORT" -l 2>&1); fi
rc=$?; [ $rc -eq 0 ] || { echo "$out" | grep -v "Communication with ::1#" >&2; exit $rc; }
'''


def bind_header(cfg):
    # With nsupdate -l (no TSIG key) neither "server" (not allowed) nor "zone"
    # (BIND rejects the session key with BADKEY) is sent: nsupdate finds the zone.
    if not cfg.get("tsig_key"):
        return []
    cmds = []
    if cfg.get("dns_server"):
        cmds.append(f"server {cfg['dns_server']} {cfg.get('dns_port') or 53}")
    if cfg.get("zone"):
        cmds.append(f"zone {cfg['zone']}")
    return cmds


def bind_args(cfg, op):
    return [cfg.get("tsig_key", ""), op, cfg.get("dns_port") or 53]


def bind_update(cfg, rec, add):
    target = rec["destino"] + "." if rec["tipo"] == "CNAME" else rec["destino"]
    name = rec["dominio"] + "."
    cmds = bind_header(cfg)
    if add:
        cmds += [f"prereq nxdomain {name}", f"update add {name} {cfg.get('ttl') or 300} {rec['tipo']} {target}"]
    else:
        cmds += [f"prereq yxrrset {name} {rec['tipo']} {target}", f"update delete {name} {rec['tipo']} {target}"]
    try:
        ssh_exec(cfg, NSUPDATE_SCRIPT, bind_args(cfg, "update"), "\n".join(cmds + ["send", ""]))
    except BadRequest as ex:
        msg = str(ex)
        if "YXDOMAIN" in msg:
            raise duplicate(cfg, rec["dominio"])
        if "NXRRSET" in msg and not add:
            return False
        if "REFUSED" in msg:
            raise BadRequest(tr("{name}: BIND refused the update; the zone must have update-policy local; "
                                "(or allow-update with the TSIG key)", name=cfg["nombre"]))
        if "NOTZONE" in msg or "enclosing zone" in msg or "NOTAUTH" in msg:
            raise BadRequest(tr("{name}: {domain} is not in a zone of this BIND",
                                name=cfg["nombre"], domain=rec["dominio"]))
        raise
    return True


def bind_add(cfg, rec):
    bind_update(cfg, rec, True)


def bind_delete(cfg, rec):
    return bind_update(cfg, rec, False)


def bind_test(cfg):
    ssh_exec(cfg, NSUPDATE_SCRIPT, bind_args(cfg, "check"))
    mode = (tr("key {file}", file=cfg["tsig_key"]) if cfg.get("tsig_key")
            else tr("the local session key (nsupdate -l)"))
    if not cfg.get("zone"):
        return tr("Connected over SSH to {host}: nsupdate available with {mode}. Set the zone so the "
                  "test also checks that BIND accepts updates.", host=cfg["ssh_host"], mode=mode)
    # Empty update with an always-true prerequisite: checks connection and permissions without changes.
    try:
        ssh_exec(cfg, NSUPDATE_SCRIPT, bind_args(cfg, "update"),
                 "\n".join(bind_header(cfg) + [f"prereq yxdomain {cfg['zone']}.", "send", ""]))
    except BadRequest as ex:
        if "REFUSED" in str(ex):
            raise BadRequest(tr("{name}: BIND refused the update; zone {zone} must have update-policy local; "
                                "(or allow-update with the TSIG key)", name=cfg["nombre"], zone=cfg["zone"]))
        raise
    return tr("Connected to BIND over SSH ({host}): zone {zone} accepts updates with {mode}",
              host=cfg["ssh_host"], zone=cfg["zone"], mode=mode)


# OpenWrt: records in the router's dnsmasq through uci (Network → DHCP and DNS).
UCI_SCRIPT = r'''
OP=$1; T=$2; N=$3; V=$4; RELOAD=$5
command -v uci >/dev/null 2>&1 || { echo "LMERR not_openwrt" >&2; exit 2; }
secs() { uci -X show dhcp | sed -n "s/^dhcp\.\([^.=]*\)=$1\$/\1/p"; }
find_secs() {  # $1 section type, $2 name option, $3 value option
  for s in $(secs "$1"); do
    [ "$(uci -q get dhcp.$s.$2)" = "$N" ] || continue
    [ -z "$4" ] || [ "$(uci -q get dhcp.$s.$3)" = "$V" ] || continue
    echo "$s"
  done
}
case "$OP" in
check) echo "$(secs domain | wc -l) $(secs cname | wc -l)"; exit 0 ;;
add)
  if [ -n "$(find_secs domain name ip)$(find_secs cname cname target)" ]; then echo "EXISTS" >&2; exit 4; fi
  if [ "$T" = CNAME ]; then s=$(uci add dhcp cname); uci set "dhcp.$s.cname=$N"; uci set "dhcp.$s.target=$V"
  else s=$(uci add dhcp domain); uci set "dhcp.$s.name=$N"; uci set "dhcp.$s.ip=$V"; fi ;;
delete)
  if [ "$T" = CNAME ]; then ss=$(find_secs cname cname target 1); else ss=$(find_secs domain name ip 1); fi
  [ -n "$ss" ] || { echo NOT_FOUND; exit 0; }
  for s in $ss; do uci delete "dhcp.$s"; done ;;
esac
uci commit dhcp || exit 2
[ -z "$RELOAD" ] || out=$(sh -c "$RELOAD" 2>&1) || { printf 'LMERR reload_failed_uci %s\n%s\n' "$RELOAD" "$out" >&2; exit 3; }
echo OK
'''


def openwrt_add(cfg, rec):
    try:
        ssh_exec(cfg, UCI_SCRIPT, ["add", rec["tipo"], rec["dominio"], rec["destino"], cfg.get("reload_command", "")])
    except BadRequest as ex:
        if "EXISTS" in str(ex):
            raise duplicate(cfg, rec["dominio"])
        raise


def openwrt_delete(cfg, rec):
    out = ssh_exec(cfg, UCI_SCRIPT, ["delete", rec["tipo"], rec["dominio"], rec["destino"],
                                     cfg.get("reload_command", "")])
    return "NOT_FOUND" not in out


def openwrt_test(cfg):
    domains, cnames = (ssh_exec(cfg, UCI_SCRIPT, ["check", "", "", "", ""]).split() + ["0", "0"])[:2]
    return tr("Connected over SSH to OpenWrt ({host}): {domains} local names and {cnames} CNAME",
              host=cfg["ssh_host"], domains=domains, cnames=cnames)


# Common fields ---------------------------------------------------------------
# Field metadata: id (key stored in integraciones.json), label, type (text,
# password, select, checkbox, number, ssh_key), required, secret (never sent back
# to the browser), format (validation), default, help, placeholder, options.
# Labels, help texts, placeholders and instructions are translated by tr().
def url_field(placeholder, port=None, help_text="If you enter only the IP, http:// is used"):
    return {"id": "url", "label": "Server IP or URL", "type": "text", "required": True,
            "format": "url", "port": port, "placeholder": placeholder,
            "help": help_text if not port else help_text + " (port {port})".replace("{port}", str(port))}


TLS_FIELD = {"id": "verify_tls", "label": "Verify the HTTPS certificate", "type": "checkbox",
             "default": True, "help": "Uncheck it if the server uses a self-signed certificate."}
TTL_FIELD = {"id": "ttl", "label": "Record TTL (seconds)", "type": "number",
             "default": 300, "min": 30, "max": 604800}


def ssh_fields(user="root"):
    return [
        {"id": "ssh_host", "label": "Server IP or name", "type": "text", "required": True,
         "format": "host", "placeholder": "192.0.2.53"},
        {"id": "ssh_port", "label": "SSH port", "type": "number", "default": 22, "min": 1, "max": 65535},
        {"id": "ssh_user", "label": "SSH user", "type": "text", "default": user, "required": True,
         "format": "user",
         "help": "It must be able to write the file and run the reload command (root is the simplest)."},
        {"id": "ssh_key", "label": "SSH private key", "type": "ssh_key", "required": True, "secret": True,
         "help": "Generate a key pair here and copy the public key to the server's ~/.ssh/authorized_keys, "
                 "or paste a private key without passphrase."},
    ]


def file_fields(path, reload_command, reload_help=None):
    return [
        {"id": "file", "label": "Records file on the server", "type": "text", "default": path,
         "required": True, "format": "path",
         "help": "linkManager only writes its records to this file; it does not touch the rest of the configuration."},
        {"id": "reload_command", "label": "Command to apply the changes", "type": "text",
         "default": reload_command, "format": "command",
         "help": reload_help or "Runs on the server after every change. If it fails, the previous file "
                                "is restored. Empty: nothing is run."},
    ]


GROUP_API, GROUP_CLOUD, GROUP_SSH = "Server API", "Cloud services", "Over SSH"
ALL_RECORDS, IP_RECORDS = ["A", "AAAA", "CNAME"], ["A", "AAAA"]


def _ssh_file_provider(name, file_format_name, records, path, reload_command, instructions, reload_help=None):
    return {"name": name, "group": GROUP_SSH, "records": records, "file_format": file_format_name,
            "instructions": instructions, "fields": ssh_fields() + file_fields(path, reload_command, reload_help),
            "add": file_add, "delete": file_delete, "test": file_test}


PROVIDERS = {
    "pihole": {
        "name": "Pi-hole", "group": GROUP_API, "records": ALL_RECORDS,
        "instructions": "Works with Pi-hole v5 and v6.",
        "fields": [
            url_field("192.0.2.10  or  https://pi.hole",
                      help_text="Without /admin. If you enter only the IP, http:// is used"),
            {"id": "version", "label": "Version", "type": "select", "default": "auto",
             "options": [["auto", "Detect automatically"], ["6", "Pi-hole v6"], ["5", "Pi-hole v5"]]},
            {"id": "password", "label": "Password or API key", "type": "password",
             "required": True, "secret": True,
             "help": "v6: web password or app password. v5: API token "
                     "(Settings → API → Show API token) or the web password."},
            TLS_FIELD,
        ],
        "add": pihole_add, "delete": pihole_delete, "test": pihole_test,
    },
    "adguard": {
        "name": "AdGuard Home", "group": GROUP_API, "records": ALL_RECORDS,
        "instructions": "Records are created as DNS rewrites (Filters → DNS rewrites).",
        "fields": [
            url_field("192.0.2.10  or  https://adguard.home",
                      help_text="The web panel address. If you enter only the IP, http:// is used"),
            {"id": "user", "label": "User", "type": "text", "required": True},
            {"id": "password", "label": "Password", "type": "password", "required": True, "secret": True},
            TLS_FIELD,
        ],
        "add": adguard_add, "delete": adguard_delete, "test": adguard_test,
    },
    "technitium": {
        "name": "Technitium DNS Server", "group": GROUP_API, "records": ALL_RECORDS,
        "instructions": "Create a token in Administration → Sessions → Create Token. The link's domain "
                        "must be inside a primary zone of the server (for example home.arpa).",
        "fields": [
            url_field("192.0.2.10  or  https://dns.home.arpa:53443", port=5380),
            {"id": "token", "label": "API token", "type": "password", "required": True, "secret": True},
            {"id": "zone", "label": "Zone (optional)", "type": "text", "format": "zone",
             "placeholder": "home.arpa", "help": "Empty: the closest zone to the domain is used."},
            TTL_FIELD, TLS_FIELD,
        ],
        "add": technitium_add, "delete": technitium_delete, "test": technitium_test,
    },
    "pdns_auth": {
        "name": "PowerDNS Authoritative", "group": GROUP_API, "records": ALL_RECORDS,
        "instructions": "Needs the API enabled in pdns.conf: api=yes, api-key=..., webserver=yes and "
                        "webserver-allow-from with linkManager's IP. The domains must be inside the "
                        "configured zone.",
        "fields": [
            url_field("192.0.2.10  or  http://192.0.2.10:8081", port=8081),
            {"id": "api_key", "label": "API key", "type": "password", "required": True, "secret": True},
            {"id": "zone", "label": "Zone", "type": "text", "required": True, "format": "zone",
             "placeholder": "home.arpa"},
            {"id": "server_id", "label": "Server id", "type": "text", "default": "localhost",
             "required": True, "format": "profile"},
            TTL_FIELD, TLS_FIELD,
        ],
        "add": pdns_add, "delete": pdns_delete, "test": pdns_test,
    },
    "nextdns": {
        "name": "NextDNS", "group": GROUP_CLOUD, "records": ALL_RECORDS,
        "instructions": "The API key is at the bottom of my.nextdns.io/account. The profile ID is the one "
                        "in the URL (my.nextdns.io/<id>/setup). Records are created as Rewrites.",
        "fields": [
            {"id": "api_key", "label": "API key", "type": "password", "required": True, "secret": True},
            {"id": "profile", "label": "Profile ID", "type": "text", "required": True,
             "format": "profile", "placeholder": "abc123"},
        ],
        "add": nextdns_add, "delete": nextdns_delete, "test": nextdns_test,
    },
    "controld": {
        "name": "Control D", "group": GROUP_CLOUD, "records": ALL_RECORDS,
        "instructions": "Create an API token with write permission in the Control D dashboard. The "
                        "profile ID is the one in its URL. Records are created as custom redirect "
                        "(SPOOF) rules.",
        "fields": [
            {"id": "token", "label": "API token", "type": "password", "required": True, "secret": True},
            {"id": "profile", "label": "Profile ID", "type": "text", "required": True,
             "format": "profile", "placeholder": "123456abcdef"},
        ],
        "add": controld_add, "delete": controld_delete, "test": controld_test,
    },
    "blocky": _ssh_file_provider(
        "Blocky", "hosts", IP_RECORDS, "/etc/blocky/linkmanager.hosts", "systemctl restart blocky",
        "Add the file to config.yml: hostsFile → sources: [/etc/blocky/linkmanager.hosts]. "
        "Only A and AAAA records."),
    "unbound": _ssh_file_provider(
        "Unbound", "unbound", ALL_RECORDS, "/etc/unbound/unbound.conf.d/linkmanager.conf",
        "unbound-checkconf && systemctl reload unbound",
        "The file is included automatically if your unbound.conf includes the unbound.conf.d folder "
        "(Debian and Ubuntu do it by default). Otherwise add: include: \"/etc/unbound/unbound.conf.d/*.conf\"."),
    "dnsmasq": _ssh_file_provider(
        "dnsmasq", "dnsmasq", ALL_RECORDS, "/etc/dnsmasq.d/linkmanager.conf",
        "dnsmasq --test && systemctl restart dnsmasq",
        "dnsmasq reads /etc/dnsmasq.d/ when it has conf-dir=/etc/dnsmasq.d (Debian and Ubuntu do it by "
        "default). For a CNAME, the target must be a name dnsmasq resolves locally (another record, "
        "/etc/hosts or DHCP)."),
    "knot": _ssh_file_provider(
        "Knot Resolver", "hosts", IP_RECORDS, "/etc/knot-resolver/linkmanager.hosts",
        "systemctl restart kresd@1",
        "Knot Resolver 5: in kresd.conf add modules = { 'hints' } and "
        "hints.add_hosts('/etc/knot-resolver/linkmanager.hosts'). Knot Resolver 6: in config.yaml, "
        "local-data → addresses-files: [/etc/knot-resolver/linkmanager.hosts] and kresctl reload as the "
        "reload command. Only A and AAAA records. Note: Knot Resolver answers NXDOMAIN for special-use "
        "domains (.home.arpa, .test, .local...) without reading the file; use another domain, for "
        "example home.lan."),
    "coredns": _ssh_file_provider(
        "CoreDNS", "hosts", IP_RECORDS, "/etc/coredns/linkmanager.hosts", "",
        "In the Corefile add: hosts /etc/coredns/linkmanager.hosts { fallthrough }. CoreDNS re-reads the "
        "file by itself every few seconds, so no reload command is needed. Only A and AAAA records.",
        "Optional: CoreDNS re-reads the file by itself."),
    "bind9": {
        "name": "BIND9", "group": GROUP_SSH, "records": ALL_RECORDS,
        "instructions": "Uses nsupdate (dynamic update) on the server. The zone must accept updates: "
                        "with update-policy local; the root user and named's session key are enough. "
                        "With a TSIG key, set its file and use allow-update or update-policy with that key.",
        "fields": ssh_fields() + [
            {"id": "tsig_key", "label": "TSIG key file on the server (optional)", "type": "text",
             "format": "path", "placeholder": "/etc/bind/linkmanager.key",
             "help": "Empty: nsupdate -l with the local session key."},
            {"id": "dns_server", "label": "Server to send the update to (optional)",
             "type": "text", "format": "host", "placeholder": "127.0.0.1",
             "help": "Only with a TSIG key (empty: the zone's primary). Without a key, nsupdate -l "
                     "always updates this same server."},
            {"id": "dns_port", "label": "BIND DNS port", "type": "number", "default": 53,
             "min": 1, "max": 65535},
            {"id": "zone", "label": "Zone (recommended)", "type": "text", "format": "zone",
             "placeholder": "home.arpa",
             "help": "With the zone, Test connection checks that BIND accepts updates. "
                     "Empty: nsupdate finds it by itself for every record."},
            TTL_FIELD,
        ],
        "add": bind_add, "delete": bind_delete, "test": bind_test,
    },
    "pdns_recursor": _ssh_file_provider(
        "PowerDNS Recursor", "hosts", IP_RECORDS, "/etc/powerdns/linkmanager.hosts",
        "systemctl restart pdns-recursor",
        "In recursor.conf: export-etc-hosts=yes and etc-hosts-file=/etc/powerdns/linkmanager.hosts "
        "(in the YAML configuration of 5.x: recursor.export_etc_hosts and recursor.etc_hosts_file). "
        "Only A and AAAA records."),
    "dnscrypt": _ssh_file_provider(
        "dnscrypt-proxy", "cloaking", ALL_RECORDS, "/etc/dnscrypt-proxy/linkmanager-cloaking.txt",
        "systemctl restart dnscrypt-proxy",
        "In dnscrypt-proxy.toml: cloaking_rules = '/etc/dnscrypt-proxy/linkmanager-cloaking.txt'. "
        "When the target is a name, dnscrypt-proxy resolves it with its internet servers and returns "
        "those IPs (not a CNAME): use a public name, not another cloaking name."),
    "openwrt": {
        "name": "OpenWrt (adblock, adblock-lean, https-dns-proxy)", "group": GROUP_SSH,
        "records": ALL_RECORDS,
        "instructions": "Creates the records in the router's dnsmasq, like Network → DHCP and DNS → "
                        "Hostnames and CNAME, with uci. adblock, adblock-lean and https-dns-proxy use that "
                        "same dnsmasq, so it works with any of them. Copy the public key to "
                        "System → Administration → SSH-Keys.",
        "fields": ssh_fields() + [
            {"id": "reload_command", "label": "Command to apply the changes", "type": "text",
             "default": "/etc/init.d/dnsmasq reload", "format": "command"},
        ],
        "add": openwrt_add, "delete": openwrt_delete, "test": openwrt_test,
    },
}


# Integration persistence (they contain passwords: file mode 600) ------------
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


def translated_field(field):
    out = dict(field)
    for k in ("label", "help", "placeholder"):
        if out.get(k):
            out[k] = tr(out[k])
    if "options" in out:
        out["options"] = [[value, tr(text)] for value, text in out["options"]]
    return out


def provider_types():
    """Integration types for the panel, in the current language."""
    return [{"type": k, "name": p["name"], "group": tr(p["group"]), "records": p["records"],
             "instructions": tr(p.get("instructions", "")),
             "fields": [translated_field(c) for c in p["fields"]]}
            for k, p in PROVIDERS.items()]


def integration_summary(it):
    if it.get("url"):
        return it["url"]
    if it.get("ssh_host"):
        port = f":{it['ssh_port']}" if it.get("ssh_port") not in (None, 22) else ""
        return f"{it.get('ssh_user', '')}@{it['ssh_host']}{port}"
    if it.get("profile"):
        return tr("profile {profile}", profile=it["profile"])
    return ""


def public_integration(it):
    """Without secrets: only tells whether they are set."""
    prov = PROVIDERS.get(it["tipo"], {})
    out = dict(it)
    for field in prov.get("fields", []):
        if field.get("secret"):
            out[field["id"]] = ""
            out[field["id"] + "_set"] = bool(it.get(field["id"]))
    out["type_name"] = prov.get("name", it["tipo"])
    out["records"] = prov.get("records", [])
    out["summary"] = integration_summary(it)
    return out


def validate_field(field, v):
    """Validates and normalizes a text field according to its "format"."""
    fmt, label = field.get("format"), tr(field["label"])
    if not v:
        return v
    if fmt == "url":
        return normalize_server_url(v, field.get("port"))
    if fmt == "host":
        v = v.lower()
        if not valid_host_or_ip(v):
            raise BadRequest(tr("'{label}' is not a valid IP or name", label=label))
    elif fmt == "zone":
        v = v.lower().rstrip(".")
        if not valid_hostname(v):
            raise BadRequest(tr("'{label}' is not a valid zone name", label=label))
    elif fmt == "user" and not USER_RE.fullmatch(v):
        raise BadRequest(tr("'{label}' is not a valid user name", label=label))
    elif fmt == "path" and not PATH_RE.fullmatch(v):
        raise BadRequest(tr("'{label}' must be an absolute path without spaces", label=label))
    elif fmt == "profile" and not PROFILE_RE.fullmatch(v):
        raise BadRequest(tr("'{label}' only accepts letters, numbers, - and _", label=label))
    elif fmt == "command" and ("\n" in v or "\r" in v):
        raise BadRequest(tr("'{label}' must be a single line", label=label))
    return v


def validate_integration(body, current=None, others=()):
    kind = current["tipo"] if current else str(body.get("tipo", ""))
    prov = PROVIDERS.get(kind)
    if not prov:
        raise BadRequest(tr("Unknown integration type"))
    name = str(body.get("nombre", current["nombre"] if current else "")).strip()[:100]
    if not name:
        raise BadRequest(tr("The integration name is required"))
    if any(o["nombre"].lower() == name.lower() for o in others):
        raise BadRequest(tr("An integration named '{name}' already exists", name=name))
    out = {"id": current["id"] if current else uuid.uuid4().hex[:8], "tipo": kind, "nombre": name}
    for field in prov["fields"]:
        k, t, label = field["id"], field["type"], tr(field["label"])
        if t == "checkbox":
            v = bool(body[k]) if k in body else (current or {}).get(k, field.get("default", False))
        elif t == "number":
            raw = body.get(k) if k in body else (current or {}).get(k, field.get("default"))
            if raw in (None, ""):
                raw = field.get("default")
            try:
                v = int(raw)
            except (TypeError, ValueError):
                raise BadRequest(tr("'{label}' must be a number", label=label))
            if not field.get("min", v) <= v <= field.get("max", v):
                raise BadRequest(tr("'{label}' must be between {min} and {max}",
                                    label=label, min=field["min"], max=field["max"]))
        else:
            limit = 20000 if t == "ssh_key" else 500
            raw = body.get(k)
            v = (str(raw).replace("\r\n", "\n").strip() if t == "ssh_key" else str(raw).strip()) \
                if raw is not None else ""
            v = v[:limit]
            new_secret = bool(v) and field.get("secret")
            if not v and field.get("secret") and current:
                v = current.get(k, "")  # empty while editing = keep the stored secret
            elif not v and k not in body:
                v = (current or {}).get(k, field.get("default", ""))
            if t == "select" and v not in [o[0] for o in field["options"]]:
                raise BadRequest(tr("Invalid value for '{label}'", label=label))
            if field.get("required") and not v:
                raise BadRequest(tr("The field '{label}' is required", label=label))
            if t == "ssh_key":
                out["ssh_public_key"] = ssh_public_key(v) if new_secret else (current or {}).get("ssh_public_key", "")
            else:
                v = validate_field(field, v)
        out[k] = v
    return out


def find_integration(integrations, ident):
    return next((x for x in integrations if x["id"] == ident), None)


# DNS record of a new link ------------------------------------------------------
def validate_dns(dns, integrations):
    """Normalizes {integracion, dominio, destino, usar_en_url} and picks the record type."""
    if not isinstance(dns, dict):
        raise BadRequest(tr("Invalid DNS data"))
    cfg = find_integration(integrations, str(dns.get("integracion", "")))
    if not cfg:
        raise BadRequest(tr("Choose a DNS server configured in Settings"))
    domain = str(dns.get("dominio", "")).strip().lower().rstrip(".")
    target = str(dns.get("destino", "")).strip().lower().rstrip(".")
    if not domain or not target:
        raise BadRequest(tr("For the DNS record, fill in the domain and the target IP or name"))
    if not valid_hostname(domain):
        raise BadRequest(tr("Invalid domain: {domain}", domain=domain))
    try:
        ip = ipaddress.ip_address(target)
        kind, target = ("A" if ip.version == 4 else "AAAA"), str(ip)
    except ValueError:
        if not valid_hostname(target):
            raise BadRequest(tr("Invalid target (neither an IP nor a name): {target}", target=target))
        if target == domain:
            raise BadRequest(tr("The CNAME cannot point to itself"))
        kind = "CNAME"
    if kind not in PROVIDERS[cfg["tipo"]]["records"]:
        raise BadRequest(tr("{name} does not support {type} records", name=cfg["nombre"], type=kind))
    return cfg, {"tipo": kind, "dominio": domain, "destino": target,
                 "usar_en_url": bool(dns.get("usar_en_url"))}


def integration_for_link(dns, integrations):
    ident = dns.get("integracion")
    if ident:
        cfg = find_integration(integrations, ident)
        if not cfg:
            raise BadRequest(tr("Integration '{name}' no longer exists", name=dns.get("servidor", ident)))
        return cfg
    # Links created before integrations existed do not store their server:
    # if there is only one integration, it is that one.
    if len(integrations) == 1:
        return integrations[0]
    raise BadRequest(tr("It is unknown which DNS server holds this link's record"))


def url_with_host(url, host):
    """Replaces the URL host keeping scheme, port, path and query."""
    u = urllib.parse.urlsplit(url)
    netloc = host + (f":{u.port}" if u.port else "")
    return urllib.parse.urlunsplit(u._replace(netloc=netloc))


# --- Validation and HTTP ---------------------------------------------------------
def validate_section(body):
    title = str(body.get("titulo", "")).strip()[:200]
    if not title:
        raise BadRequest(tr("The section title is required"))
    return title


LINK_FIELD_LABELS = {"nombre": "Name", "descripcion": "Description", "url": "URL"}


def validate(body, current=None):
    out = dict(current or {})
    for k in ("nombre", "descripcion", "url"):
        v = str(body.get(k, out.get(k, ""))).strip()
        if k != "descripcion" and not v:
            raise BadRequest(tr("The field '{label}' is required", label=tr(LINK_FIELD_LABELS[k])))
        out[k] = v[:300]
    if urllib.parse.urlparse(out["url"]).scheme not in ("http", "https"):
        raise BadRequest(tr("The URL must start with http:// or https://"))
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
            raise BadRequest(tr("Request too large"))
        try:
            return json.loads(self.rfile.read(n) or b"{}")
        except ValueError:
            raise BadRequest(tr("Invalid JSON"))

    def route(self, method):
        url = urllib.parse.urlparse(self.path)
        parts = [p for p in url.path.split("/") if p][1:]  # without "api"
        if parts == ["settings"]:
            if method == "GET":
                return 200, {**load_settings(), "languages": LANGUAGES}
            if method == "PUT":
                with lock:
                    return 200, {**save_settings(self.body()), "languages": LANGUAGES}
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
                            # Do not leave the just downloaded or uploaded icon orphaned.
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
                    return 404, {"error": tr("Link not found")}
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
                    # With ?forzar=1 only the link is deleted and the DNS record stays on the server.
                    force = urllib.parse.parse_qs(url.query).get("forzar") == ["1"]
                    dns_state = None
                    if item.get("dns") and not force:
                        try:
                            cfg = integration_for_link(item["dns"], load_integrations())
                            existed = PROVIDERS[cfg["tipo"]]["delete"](cfg, item["dns"])
                        except BadRequest as ex:
                            return 409, {"error": tr("Could not delete the DNS record: {error}", error=ex),
                                         "dns_fallo": True}
                        dns_state = "borrado" if existed else "no_existia"
                    links.remove(item)
                    save(links)
                    return 200, {"ok": True, "dns": dns_state}
                if method == "POST" and parts[2:] == ["mover"]:
                    d = 1 if self.body().get("dir", 1) > 0 else -1
                    i = links.index(item)
                    j = i + d
                    if 0 <= j < len(links):
                        links[i], links[j] = links[j], links[i]
                        save(links)
                    return 200, links
        return 404, {"error": tr("Route not found")}

    def route_integrations(self, method, parts):
        if method == "GET" and parts == ["tipos"]:
            return 200, provider_types()
        if method == "POST" and parts == ["generar-clave"]:
            return 200, generate_ssh_key()
        if method == "POST" and parts == ["probar"]:
            # Test what is in the form without saving it. While editing,
            # an empty password uses the stored one.
            body = self.body()
            with lock:
                current = find_integration(load_integrations(), str(body.get("id", "")))
                cfg = validate_integration({**body, "nombre": body.get("nombre") or tr("DNS server")}, current)
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
                return 404, {"error": tr("Integration not found")}
            if method == "PUT" and len(parts) == 1:
                updated = validate_integration(self.body(), it, [x for x in items if x is not it])
                items[items.index(it)] = updated
                save_integrations(items)
                return 200, public_integration(updated)
            if method == "DELETE" and len(parts) == 1:
                items.remove(it)
                save_integrations(items)
                return 200, {"ok": True}
            if not (method == "POST" and parts[1:] == ["probar"]):
                return 404, {"error": tr("Route not found")}
            cfg = dict(it)
        return 200, {"ok": True, "mensaje": PROVIDERS[cfg["tipo"]]["test"](cfg)}

    def handle_method(self, method):
        try:
            code, obj = self.route(method)
        except BadRequest as ex:
            code, obj = 400, {"error": str(ex)}
        except Exception as ex:  # noqa: BLE001
            self.log_error("error: %r", ex)
            code, obj = 500, {"error": tr("Internal error, check the service log")}
        self.send_json(code, obj)

    def do_GET(self): self.handle_method("GET")
    def do_POST(self): self.handle_method("POST")
    def do_PUT(self): self.handle_method("PUT")
    def do_DELETE(self): self.handle_method("DELETE")


def init():
    """Creates an empty links.json and the icons folder when missing."""
    os.makedirs(os.path.dirname(DATA), exist_ok=True)
    os.makedirs(IMAGES, exist_ok=True)
    if not os.path.exists(DATA):
        write_atomic(DATA, "[]\n")
    import_legacy_pihole()


def import_legacy_pihole():
    """Older versions configured Pi-hole with LM_PIHOLE_* variables. If they are set
    and there are no integrations yet, they are imported once as an integration."""
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
        print("Pi-hole integration imported from LM_PIHOLE_*; you can remove those variables now", flush=True)
    except (BadRequest, OSError) as ex:
        print(f"Could not import the LM_PIHOLE_* configuration: {ex}", flush=True)


# --- Spanish translations -----------------------------------------------------------
ES = {
    # Settings and links
    "Unknown language": "Idioma desconocido",
    "The selected section does not exist": "La sección elegida no existe",
    "The icon is larger than 2 MB": "El icono supera 2 MB",
    "Icon source not allowed": "Origen de icono no permitido",
    "The downloaded file is too large": "El archivo descargado es demasiado grande",
    "Could not download the icon: {error}": "No se pudo descargar el icono: {error}",
    "The downloaded file is not a valid image ({type})": "El archivo descargado no es una imagen válida ({type})",
    "Icon format not allowed ({formats})": "Formato de icono no permitido ({formats})",
    "Invalid uploaded icon": "Icono subido inválido",
    "Choose an icon: existing, uploaded or from the internet": "Elegí un icono: existente, subido o de internet",
    "Could not search the internet: {errors}": "No se pudo buscar en internet: {errors}",
    "The current authentication was not received": "No se recibió la autenticación actual",
    "The current password is not correct": "La contraseña actual no es correcta",
    "The new password must be at least 8 characters long": "La nueva contraseña debe tener al menos 8 caracteres",
    "The new passwords do not match": "Las contraseñas nuevas no coinciden",
    "The new password is the same as the current one": "La nueva contraseña es igual a la actual",
    "Invalid DNS data": "Datos de DNS inválidos",
    "Choose a DNS server configured in Settings": "Elegí un servidor DNS configurado en Configuración",
    "For the DNS record, fill in the domain and the target IP or name":
        "Para el DNS completá el dominio y la IP o nombre de destino",
    "Invalid domain: {domain}": "Dominio inválido: {domain}",
    "Invalid target (neither an IP nor a name): {target}": "Destino inválido (no es IP ni nombre): {target}",
    "The CNAME cannot point to itself": "El CNAME no puede apuntar a sí mismo",
    "{name} does not support {type} records": "{name} no admite registros {type}",
    "Integration '{name}' no longer exists": "La integración '{name}' ya no existe",
    "It is unknown which DNS server holds this link's record":
        "No se sabe en qué servidor DNS está el registro de este link",
    "The section title is required": "El título de la sección es obligatorio",
    "Name": "Nombre",
    "Description": "Descripción",
    "URL": "URL",
    "The URL must start with http:// or https://": "La URL debe empezar con http:// o https://",
    "Request too large": "Petición demasiado grande",
    "Invalid JSON": "JSON inválido",
    "Link not found": "Link no encontrado",
    "Could not delete the DNS record: {error}": "No se pudo borrar el registro DNS: {error}",
    "Route not found": "Ruta no encontrada",
    "DNS server": "Servidor DNS",
    "Integration not found": "Integración no encontrada",
    "Internal error, check the service log": "Error interno, revisá el log del servicio",
    # Integrations: validation
    "Unknown integration type": "Tipo de integración desconocido",
    "The integration name is required": "El nombre de la integración es obligatorio",
    "An integration named '{name}' already exists": "Ya existe una integración llamada '{name}'",
    "'{label}' must be a number": "'{label}' tiene que ser un número",
    "'{label}' must be between {min} and {max}": "'{label}' tiene que estar entre {min} y {max}",
    "Invalid value for '{label}'": "Valor inválido para '{label}'",
    "The field '{label}' is required": "El campo '{label}' es obligatorio",
    "'{label}' is not a valid IP or name": "'{label}' no es una IP ni un nombre válido",
    "'{label}' is not a valid zone name": "'{label}' no es un nombre de zona válido",
    "'{label}' is not a valid user name": "'{label}' no es un nombre de usuario válido",
    "'{label}' must be an absolute path without spaces": "'{label}' tiene que ser una ruta absoluta sin espacios",
    "'{label}' only accepts letters, numbers, - and _": "'{label}' solo admite letras, números, - y _",
    "'{label}' must be a single line": "'{label}' tiene que ser una sola línea",
    "Invalid server address (use an IP, a name or an http/https URL)":
        "Dirección del servidor inválida (usá una IP, un nombre o una URL http/https)",
    "profile {profile}": "perfil {profile}",
    # Integrations: connection and server errors
    "{name}: could not connect ({reason})": "{name}: no se pudo conectar ({reason})",
    "HTTP error {status}": "error HTTP {status}",
    "{name}: a record for {domain} already exists": "{name}: ya existe un registro para {domain}",
    "unknown error": "error desconocido",
    "{name}: wrong API key or password": "{name}: API key o contraseña incorrecta",
    "{name}: unexpected response (HTTP {status}). Is it a Pi-hole?":
        "{name}: respuesta inesperada (HTTP {status}). ¿Es un Pi-hole?",
    "{name}: wrong password or API not available (HTTP {status})":
        "{name}: contraseña incorrecta o API no disponible (HTTP {status})",
    "{name}: wrong user or password": "{name}: usuario o contraseña incorrectos",
    "unexpected response. Is it a Technitium DNS Server?": "respuesta inesperada. ¿Es un Technitium DNS Server?",
    "{name}: invalid or expired token": "{name}: token inválido o vencido",
    "{name}: zone {zone} does not exist on the server": "{name}: la zona {zone} no existe en el servidor",
    "{name}: wrong API key": "{name}: API key incorrecta",
    "{name}: zone {zone} does not exist (or the server id is wrong)":
        "{name}: la zona {zone} no existe (o el server id es incorrecto)",
    "{name}: {domain} does not belong to zone {zone}": "{name}: {domain} no pertenece a la zona {zone}",
    "{name}: profile {profile} does not exist": "{name}: el perfil {profile} no existe",
    "{name}: wrong API token or no write permission": "{name}: token de API incorrecto o sin permiso de escritura",
    "{name}: profile {profile} does not exist in the account": "{name}: el perfil {profile} no existe en la cuenta",
    "{name}: the SSH private key is missing": "{name}: falta la clave privada SSH",
    "{name}: the server did not answer over SSH in time": "{name}: el servidor no respondió a tiempo por SSH",
    "The ssh client is missing where linkManager runs (package openssh-client)":
        "Falta el cliente ssh donde corre linkManager (paquete openssh-client)",
    "connection error": "error de conexión",
    "the key is not authorized (copy the public key to the server's authorized_keys)":
        "la clave no está autorizada (copiá la clave pública en authorized_keys del servidor)",
    "the server's host key changed; if that is expected, delete its line from {file}":
        "cambió la clave de host del servidor; si es esperado, borrá su línea de {file}",
    "{name}: could not connect over SSH: {detail}": "{name}: no se pudo conectar por SSH: {detail}",
    "error {code} on the server": "error {code} en el servidor",
    "ssh-keygen is missing where linkManager runs (package openssh-client)":
        "Falta ssh-keygen donde corre linkManager (paquete openssh-client)",
    "Could not generate the key: {error}": "No se pudo generar la clave: {error}",
    "The SSH private key is not valid or has a passphrase (use one without passphrase)":
        "La clave privada SSH no es válida o tiene contraseña (usá una sin contraseña)",
    "folder {arg} does not exist": "no existe la carpeta {arg}",
    "the SSH user cannot write to {arg}": "el usuario SSH no puede escribir en {arg}",
    "command {arg} was not found": "no se encontró el comando {arg}",
    "the reload command ({arg}) failed and the previous file was restored":
        "falló el comando de recarga ({arg}) y se restauró el archivo anterior",
    "the reload command ({arg}) failed": "falló el comando de recarga ({arg})",
    "nsupdate is not installed (package bind9-dnsutils or bind-utils)":
        "nsupdate no está instalado (paquete bind9-dnsutils o bind-utils)",
    "cannot read key file {arg}": "no se puede leer la clave {arg}",
    "named's session key was not found (is BIND running? can the SSH user read it?)":
        "no se encontró la clave de sesión de named (¿está corriendo BIND? ¿el usuario SSH puede leerla?)",
    "it is not an OpenWrt (the uci command is missing)": "no es un OpenWrt (no está el comando uci)",
    "{name}: a record for {domain} already exists in {file}": "{name}: ya existe un registro para {domain} en {file}",
    "{name}: BIND refused the update; the zone must have update-policy local; (or allow-update with the TSIG key)":
        "{name}: BIND rechazó la actualización; la zona tiene que tener update-policy local; "
        "(o allow-update con la clave TSIG)",
    "{name}: {domain} is not in a zone of this BIND": "{name}: {domain} no está en una zona de este BIND",
    "key {file}": "clave {file}",
    "the local session key (nsupdate -l)": "clave de sesión local (nsupdate -l)",
    "{name}: BIND refused the update; zone {zone} must have update-policy local; (or allow-update with the TSIG key)":
        "{name}: BIND rechazó la actualización; la zona {zone} tiene que tener update-policy local; "
        "(o allow-update con la clave TSIG)",
    # Integrations: test results
    "Connected to Pi-hole v{version}: {hosts} local DNS records and {cnames} CNAME":
        "Conectado a Pi-hole v{version}: {hosts} registros DNS locales y {cnames} CNAME",
    "Connected to AdGuard Home {version}: {count} DNS rewrites":
        "Conectado a AdGuard Home {version}: {count} reescrituras DNS",
    "Connected to Technitium DNS Server: {count} zones": "Conectado a Technitium DNS Server: {count} zonas",
    "Connected to PowerDNS Authoritative: zone {zone} with {count} record sets":
        "Conectado a PowerDNS Authoritative: zona {zone} con {count} conjuntos de registros",
    'Connected to NextDNS: profile "{profile}" with {count} rewrites':
        'Conectado a NextDNS: perfil "{profile}" con {count} rewrites',
    'Connected to Control D: profile "{profile}" with {count} rules':
        'Conectado a Control D: perfil "{profile}" con {count} reglas',
    "Connected over SSH to {host}: {count} linkManager records in {file}":
        "Conectado por SSH a {host}: {count} registros de linkManager en {file}",
    "Connected over SSH to {host}: nsupdate available with {mode}. Set the zone so the test also checks "
    "that BIND accepts updates.":
        "Conectado por SSH a {host}: nsupdate disponible con {mode}. Indicá la zona para que la prueba "
        "también verifique que BIND acepta actualizaciones.",
    "Connected to BIND over SSH ({host}): zone {zone} accepts updates with {mode}":
        "Conectado a BIND por SSH ({host}): la zona {zone} acepta actualizaciones con {mode}",
    "Connected over SSH to OpenWrt ({host}): {domains} local names and {cnames} CNAME":
        "Conectado por SSH a OpenWrt ({host}): {domains} nombres locales y {cnames} CNAME",
    # Integration types: groups, fields and instructions
    "Server API": "API del servidor",
    "Cloud services": "Servicios en la nube",
    "Over SSH": "Por SSH",
    "Server IP or URL": "IP o URL del servidor",
    "If you enter only the IP, http:// is used (port 5380)": "Si ponés solo la IP se usa http:// y el puerto 5380",
    "If you enter only the IP, http:// is used (port 8081)": "Si ponés solo la IP se usa http:// y el puerto 8081",
    "Without /admin. If you enter only the IP, http:// is used": "Sin /admin. Si ponés solo la IP se usa http://",
    "The web panel address. If you enter only the IP, http:// is used":
        "La dirección del panel web. Si ponés solo la IP se usa http://",
    "192.0.2.10  or  https://pi.hole": "192.0.2.10  o  https://pi.hole",
    "192.0.2.10  or  https://adguard.home": "192.0.2.10  o  https://adguard.home",
    "192.0.2.10  or  https://dns.home.arpa:53443": "192.0.2.10  o  https://dns.home.arpa:53443",
    "192.0.2.10  or  http://192.0.2.10:8081": "192.0.2.10  o  http://192.0.2.10:8081",
    "Verify the HTTPS certificate": "Verificar el certificado HTTPS",
    "Uncheck it if the server uses a self-signed certificate.": "Desmarcalo si usa un certificado autofirmado.",
    "Record TTL (seconds)": "TTL de los registros (segundos)",
    "Server IP or name": "IP o nombre del servidor",
    "SSH port": "Puerto SSH",
    "SSH user": "Usuario SSH",
    "It must be able to write the file and run the reload command (root is the simplest).":
        "Tiene que poder escribir el archivo y ejecutar el comando de recarga (lo más simple es root).",
    "SSH private key": "Clave privada SSH",
    "Generate a key pair here and copy the public key to the server's ~/.ssh/authorized_keys, or paste a "
    "private key without passphrase.":
        "Generá un par de claves acá y copiá la clave pública en ~/.ssh/authorized_keys del servidor, o "
        "pegá una clave privada sin contraseña.",
    "Records file on the server": "Archivo de registros en el servidor",
    "linkManager only writes its records to this file; it does not touch the rest of the configuration.":
        "linkManager escribe sus registros solo en este archivo; no toca el resto de la configuración.",
    "Command to apply the changes": "Comando para aplicar los cambios",
    "Runs on the server after every change. If it fails, the previous file is restored. Empty: nothing is run.":
        "Se ejecuta en el servidor después de cada cambio. Si falla, se restaura el archivo anterior. "
        "Vacío: no ejecuta nada.",
    "Optional: CoreDNS re-reads the file by itself.": "Opcional: CoreDNS relee el archivo solo.",
    "Version": "Versión",
    "Detect automatically": "Detectar automáticamente",
    "Password or API key": "Contraseña o API key",
    "v6: web password or app password. v5: API token (Settings → API → Show API token) or the web password.":
        "v6: contraseña web o app password. v5: token de la API (Settings → API → Show API token) o la "
        "contraseña web.",
    "Works with Pi-hole v5 and v6.": "Funciona con Pi-hole v5 y v6.",
    "Records are created as DNS rewrites (Filters → DNS rewrites).":
        "Los registros se crean como reescrituras DNS (Filters → DNS rewrites).",
    "User": "Usuario",
    "Password": "Contraseña",
    "Create a token in Administration → Sessions → Create Token. The link's domain must be inside a primary "
    "zone of the server (for example home.arpa).":
        "Creá un token en Administration → Sessions → Create Token. El dominio del link tiene que estar "
        "dentro de una zona primaria del servidor (por ejemplo home.arpa).",
    "API token": "Token de la API",
    "Zone (optional)": "Zona (opcional)",
    "Empty: the closest zone to the domain is used.": "Vacío: se usa la zona más cercana al dominio.",
    "Needs the API enabled in pdns.conf: api=yes, api-key=..., webserver=yes and webserver-allow-from with "
    "linkManager's IP. The domains must be inside the configured zone.":
        "Necesita la API activada en pdns.conf: api=yes, api-key=..., webserver=yes y webserver-allow-from "
        "con la IP de linkManager. Los dominios tienen que estar dentro de la zona configurada.",
    "API key": "API key",
    "Zone": "Zona",
    "Server id": "Server id",
    "The API key is at the bottom of my.nextdns.io/account. The profile ID is the one in the URL "
    "(my.nextdns.io/<id>/setup). Records are created as Rewrites.":
        "La API key está al pie de my.nextdns.io/account. El ID del perfil es el que aparece en la URL "
        "(my.nextdns.io/<id>/setup). Los registros se crean como Rewrites.",
    "Profile ID": "ID del perfil",
    "Create an API token with write permission in the Control D dashboard. The profile ID is the one in its "
    "URL. Records are created as custom redirect (SPOOF) rules.":
        "Creá un token de API con permiso de escritura en el panel de Control D. El ID del perfil es el que "
        "aparece en su URL. Los registros se crean como reglas personalizadas de tipo redirección (SPOOF).",
    "Add the file to config.yml: hostsFile → sources: [/etc/blocky/linkmanager.hosts]. Only A and AAAA records.":
        "Agregá el archivo en config.yml: hostsFile → sources: [/etc/blocky/linkmanager.hosts]. Solo "
        "registros A y AAAA.",
    "The file is included automatically if your unbound.conf includes the unbound.conf.d folder (Debian and "
    "Ubuntu do it by default). Otherwise add: include: \"/etc/unbound/unbound.conf.d/*.conf\".":
        "El archivo se incluye solo si tu unbound.conf incluye la carpeta unbound.conf.d (en Debian y Ubuntu "
        "ya viene así). Si no, agregá: include: \"/etc/unbound/unbound.conf.d/*.conf\".",
    "dnsmasq reads /etc/dnsmasq.d/ when it has conf-dir=/etc/dnsmasq.d (Debian and Ubuntu do it by default). "
    "For a CNAME, the target must be a name dnsmasq resolves locally (another record, /etc/hosts or DHCP).":
        "dnsmasq lee /etc/dnsmasq.d/ si tiene conf-dir=/etc/dnsmasq.d (en Debian y Ubuntu ya viene así). "
        "Para un CNAME, el destino tiene que ser un nombre que dnsmasq resuelva localmente (otro registro, "
        "/etc/hosts o DHCP).",
    "Knot Resolver 5: in kresd.conf add modules = { 'hints' } and "
    "hints.add_hosts('/etc/knot-resolver/linkmanager.hosts'). Knot Resolver 6: in config.yaml, local-data → "
    "addresses-files: [/etc/knot-resolver/linkmanager.hosts] and kresctl reload as the reload command. Only A "
    "and AAAA records. Note: Knot Resolver answers NXDOMAIN for special-use domains (.home.arpa, .test, "
    ".local...) without reading the file; use another domain, for example home.lan.":
        "Knot Resolver 5: en kresd.conf agregá modules = { 'hints' } y "
        "hints.add_hosts('/etc/knot-resolver/linkmanager.hosts'). Knot Resolver 6: en config.yaml, "
        "local-data → addresses-files: [/etc/knot-resolver/linkmanager.hosts] y como comando de recarga "
        "kresctl reload. Solo registros A y AAAA. Ojo: Knot Resolver responde NXDOMAIN para dominios de uso "
        "especial (.home.arpa, .test, .local...) sin mirar el archivo; usá otro dominio, por ejemplo casa.lan.",
    "In the Corefile add: hosts /etc/coredns/linkmanager.hosts { fallthrough }. CoreDNS re-reads the file by "
    "itself every few seconds, so no reload command is needed. Only A and AAAA records.":
        "En el Corefile agregá: hosts /etc/coredns/linkmanager.hosts { fallthrough }. CoreDNS relee el "
        "archivo solo cada pocos segundos, así que no hace falta comando de recarga. Solo registros A y AAAA.",
    "Uses nsupdate (dynamic update) on the server. The zone must accept updates: with update-policy local; "
    "the root user and named's session key are enough. With a TSIG key, set its file and use allow-update or "
    "update-policy with that key.":
        "Usa nsupdate (actualización dinámica) en el servidor. La zona tiene que aceptar actualizaciones: "
        "con update-policy local; alcanza con el usuario root y la clave de sesión de named. Con una clave "
        "TSIG, indicá su archivo y usá allow-update o update-policy con esa clave.",
    "TSIG key file on the server (optional)": "Archivo de clave TSIG en el servidor (opcional)",
    "Empty: nsupdate -l with the local session key.": "Vacío: nsupdate -l con la clave de sesión local.",
    "Server to send the update to (optional)": "Servidor al que enviar la actualización (opcional)",
    "Only with a TSIG key (empty: the zone's primary). Without a key, nsupdate -l always updates this same "
    "server.":
        "Solo con clave TSIG (vacío: el primario de la zona). Sin clave, nsupdate -l actualiza siempre este "
        "mismo servidor.",
    "BIND DNS port": "Puerto DNS de BIND",
    "Zone (recommended)": "Zona (recomendada)",
    "With the zone, Test connection checks that BIND accepts updates. Empty: nsupdate finds it by itself for "
    "every record.":
        "Con la zona, Probar conexión verifica que BIND acepta actualizaciones. Vacío: nsupdate la detecta "
        "sola en cada registro.",
    "In recursor.conf: export-etc-hosts=yes and etc-hosts-file=/etc/powerdns/linkmanager.hosts (in the YAML "
    "configuration of 5.x: recursor.export_etc_hosts and recursor.etc_hosts_file). Only A and AAAA records.":
        "En recursor.conf: export-etc-hosts=yes y etc-hosts-file=/etc/powerdns/linkmanager.hosts (en la "
        "configuración YAML de la 5.x: recursor.export_etc_hosts y recursor.etc_hosts_file). Solo registros "
        "A y AAAA.",
    "In dnscrypt-proxy.toml: cloaking_rules = '/etc/dnscrypt-proxy/linkmanager-cloaking.txt'. When the target "
    "is a name, dnscrypt-proxy resolves it with its internet servers and returns those IPs (not a CNAME): use "
    "a public name, not another cloaking name.":
        "En dnscrypt-proxy.toml: cloaking_rules = '/etc/dnscrypt-proxy/linkmanager-cloaking.txt'. Si el "
        "destino es un nombre, dnscrypt-proxy lo resuelve con sus servidores de internet y devuelve esas IPs "
        "(no un CNAME): usá un nombre público, no otro nombre del cloaking.",
    "Creates the records in the router's dnsmasq, like Network → DHCP and DNS → Hostnames and CNAME, with "
    "uci. adblock, adblock-lean and https-dns-proxy use that same dnsmasq, so it works with any of them. Copy "
    "the public key to System → Administration → SSH-Keys.":
        "Crea los registros en el dnsmasq del router, como Network → DHCP and DNS → Hostnames y CNAME, con "
        "uci. adblock, adblock-lean y https-dns-proxy usan ese mismo dnsmasq, así que sirve con cualquiera "
        "de ellos. Copiá la clave pública en System → Administration → SSH-Keys.",
}


if __name__ == "__main__":
    init()
    ThreadingHTTPServer(LISTEN, Handler).serve_forever()
