#!/usr/bin/env python3
"""Checks that every translatable text has a Spanish translation with the same placeholders.

- Backend (app.py): literal arguments of tr(), SCRIPT_ERRORS, link field labels and the
  translated metadata of PROVIDERS (groups, instructions, labels, help, placeholders, options).
- Panel (admin/index.html): every key used with t("...") or data-i18n* exists in both
  languages of the I18N dictionary, and both languages have the same keys.

Usage: python3 tools/check_translations.py   (exit code 1 when something is missing)
"""
import ast
import importlib.util
import json
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
problems = []


# Texts that are the same in every language: examples without spaces (IPs, paths,
# ids) and product names.
NEUTRAL = {"Pi-hole v5", "Pi-hole v6"}


def is_neutral(text):
    return text in NEUTRAL or (not re.search(r"\s", text) and re.search(r"[\d./:@<>]", text) is not None)


def placeholders(text):
    return sorted(set(re.findall(r"\{(\w+)\}", text)))


def check_backend():
    os.environ.setdefault("LM_DATA", "/nonexistent/links.json")
    spec = importlib.util.spec_from_file_location("app", os.path.join(ROOT, "app.py"))
    app = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(app)
    texts = set()
    tree = ast.parse(open(os.path.join(ROOT, "app.py")).read())
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and getattr(node.func, "id", None) == "tr" and node.args:
            if isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str):
                texts.add(node.args[0].value)
    texts |= set(app.SCRIPT_ERRORS.values()) | set(app.LINK_FIELD_LABELS.values())
    for p in app.PROVIDERS.values():
        texts |= {p["group"], p.get("instructions", "")}
        for f in p["fields"]:
            texts |= {f.get(k) for k in ("label", "help", "placeholder") if f.get(k)}
            texts |= {o[1] for o in f.get("options", [])}
    texts = {t for t in texts if t and not is_neutral(t)}
    for text in sorted(texts):
        if text not in app.ES:
            problems.append(f"app.py: missing Spanish translation: {text!r}")
        elif placeholders(text) != placeholders(app.ES[text]):
            problems.append(f"app.py: different placeholders: {text!r}")
    for text in app.ES:
        if text not in texts:
            problems.append(f"app.py: unused translation: {text!r}")
    return len(texts)


def check_panel():
    page = open(os.path.join(ROOT, "admin", "index.html")).read()
    m = re.search(r"const I18N = (\{.*?\n\});", page, re.S)
    if not m:
        problems.append("admin/index.html: I18N dictionary not found")
        return 0
    i18n = json.loads(m[1])
    en, es = i18n["en"], i18n["es"]
    for k in en.keys() - es.keys():
        problems.append(f"admin/index.html: key without Spanish text: {k}")
    for k in es.keys() - en.keys():
        problems.append(f"admin/index.html: key without English text: {k}")
    for k in en.keys() & es.keys():
        if placeholders(en[k]) != placeholders(es[k]):
            problems.append(f"admin/index.html: different placeholders in {k}")
    # Every quoted "group.name" string outside the dictionary counts as a used key
    # (t("..."), setSearchStatus("..."), data-i18n="...").
    rest = page.replace(m[0], "")
    used = set(re.findall(r'["\']([a-z]+\.[a-z_]+)["\']', rest))
    for k in sorted(used - en.keys()):
        problems.append(f"admin/index.html: key used but not defined: {k}")
    for k in sorted(en.keys() - used):
        problems.append(f"admin/index.html: key defined but not used: {k}")
    return len(en)


n_back = check_backend()
n_panel = check_panel()
for p in problems:
    print(p)
print(f"backend: {n_back} texts, panel: {n_panel} keys, problems: {len(problems)}")
sys.exit(1 if problems else 0)
