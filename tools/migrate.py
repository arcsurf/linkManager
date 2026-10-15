#!/usr/bin/env python3
"""Importa una página de links estática ya existente a linkManager.

Busca las tarjetas con el formato:

    <article class="aplicacion">
        <a href="URL"><img src="./images/ICONO"><h2>NOMBRE</h2><p>DESCRIPCION</p></a>
    </article>

las guarda en LM_DATA y reemplaza el bloque de tarjetas (desde la primera
<section class="aplicaciones"> hasta </main>) por los marcadores LINKS.
Usa las mismas variables de entorno LM_* que app.py. Hacé un backup antes.
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
    sys.exit("La página ya tiene los marcadores LINKS: no hace falta migrar")

card = re.compile(
    r'<article class="aplicacion">\s*<a href="([^"]*)"[^>]*>\s*'
    r'<img src="\./images/([^"]*)"[^>]*>\s*<h2>(.*?)</h2>\s*<p>(.*?)</p>\s*</a>\s*</article>',
    re.S)
links = [{"id": uuid.uuid4().hex[:8], "nombre": html.unescape(n), "descripcion": html.unescape(d),
          "url": html.unescape(u), "icono": i} for u, i, n, d in card.findall(page)]
if not links:
    sys.exit("No se encontraron tarjetas para migrar")

i = page.index('<section class="aplicaciones">')
j = page.index("</main>")
page = page[:i] + app.START + "\n" + app.END + "\n    " + page[j:]

app.init()
with open(app.DATA, "w") as f:
    json.dump(links, f, ensure_ascii=False, indent=2)
with open(app.INDEX, "w") as f:
    f.write(page)
app.save(links)
print(f"Migrados {len(links)} links:", ", ".join(l["nombre"] for l in links))
