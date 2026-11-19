#!/usr/bin/env python3
"""Imports an existing static links page into linkManager.

Looks for cards with this format:

    <article class="aplicacion">
        <a href="URL"><img src="./images/ICON"><h2>NAME</h2><p>DESCRIPTION</p></a>
    </article>

stores them in LM_DATA and replaces the block of cards (from the first
<section class="aplicaciones"> to </main>) with the LINKS markers.
It uses the same LM_* environment variables as app.py. Make a backup first.
"""
import html
import json
import os
import re
import sys
import uuid

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
sys.path.insert(0, "/opt/linkmanager")
import app  # noqa: E402

page = open(app.INDEX).read()
if app.START in page:
    sys.exit("The page already has the LINKS markers: there is nothing to migrate")

card = re.compile(
    r'<article class="aplicacion">\s*<a href="([^"]*)"[^>]*>\s*'
    r'<img src="\./images/([^"]*)"[^>]*>\s*<h2>(.*?)</h2>\s*<p>(.*?)</p>\s*</a>\s*</article>',
    re.S)
links = [{"id": uuid.uuid4().hex[:8], "nombre": html.unescape(n), "descripcion": html.unescape(d),
          "url": html.unescape(u), "icono": i} for u, i, n, d in card.findall(page)]
if not links:
    sys.exit("No cards to migrate were found")

i = page.index('<section class="aplicaciones">')
j = page.index("</main>")
page = page[:i] + app.START + "\n" + app.END + "\n    " + page[j:]

app.init()
with open(app.DATA, "w") as f:
    json.dump(links, f, ensure_ascii=False, indent=2)
with open(app.INDEX, "w") as f:
    f.write(page)
app.save(links)
print(f"Migrated {len(links)} links:", ", ".join(l["nombre"] for l in links))
