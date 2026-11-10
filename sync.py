#!/usr/bin/env python3
"""
Bsale -> Shopify: stock informativo por sucursal (metafield de variante).

Escribe en cada variante de Shopify el metafield `vchile.stock_sucursales` (JSON)
con el stock disponible por sucursal de Bsale, p. ej. {"1": 752, "2": 20, "5": 14}.

NO toca el inventario de Shopify: el stock vendible de Casa Matriz lo sincroniza
Pivot. Así ambos procesos nunca se pisan.

Uso:
  python sync.py --sku 3756 --dry-run   # prueba con un SKU, sin escribir
  python sync.py --full --dry-run       # muestra todos los cambios
  python sync.py --full                 # sincronización real (cron cada 10 min)
  python sync.py --serve --port 8080    # receptor de webhooks de stock de Bsale
"""
import argparse
import json
import os
import sys
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlparse

import requests

BSALE_TOKEN = os.environ.get("BSALE_TOKEN", "")
SHOP = os.environ.get("SHOPIFY_SHOP", "")              # mrmc81-1m.myshopify.com
# Credenciales de la app creada en el Dev Dashboard de Shopify (desde 2026 las apps
# nuevas no entregan un token fijo: se pide uno de 24 h con client_id + client_secret).
SHOPIFY_CLIENT_ID = os.environ.get("SHOPIFY_CLIENT_ID", "")
SHOPIFY_CLIENT_SECRET = os.environ.get("SHOPIFY_CLIENT_SECRET", "")
SHOPIFY_TOKEN = os.environ.get("SHOPIFY_TOKEN", "")    # opcional: token fijo shpat_ de una app antigua
API_VERSION = os.environ.get("SHOPIFY_API_VERSION", "2026-10")
OFFICES = [o.strip() for o in os.environ.get("BSALE_OFFICES", "1,2,5").split(",") if o.strip()]
NAMESPACE, KEY = "vchile", "stock_sucursales"

BSALE = "https://api.bsale.io/v1/"
session = requests.Session()


def log(*a):
    print(time.strftime("%Y-%m-%d %H:%M:%S"), *a, flush=True)


# ---------------------------------------------------------------- Bsale
def bsale_get(path_or_url, **params):
    url = path_or_url if path_or_url.startswith("http") else BSALE + path_or_url
    for intento in range(6):
        r = session.get(url, params=params or None,
                        headers={"access_token": BSALE_TOKEN}, timeout=30)
        if r.status_code == 429 or r.status_code >= 500:
            time.sleep(5 * (intento + 1)); continue
        r.raise_for_status()
        return r.json()
    raise RuntimeError(f"Bsale no responde: {url}")


def qty(item):
    """quantityAvailable ya descuenta lo reservado; nunca negativo."""
    return max(int(float(item.get("quantityAvailable") or 0)), 0)


def bsale_all_stock():
    """{sku: {officeId: cantidad}} para las sucursales configuradas.
    Se pagina con offset propio: el enlace "next" de Bsale no conserva el expand."""
    stock = {}
    for office in OFFICES:
        n, offset = 0, 0
        while True:
            data = bsale_get("stocks.json", officeid=office, limit=50, offset=offset,
                             expand="[variant]")
            items = data.get("items", [])
            for it in items:
                code = ((it.get("variant") or {}).get("code") or "").strip()
                if code:
                    stock.setdefault(code, {})[office] = qty(it)
                    n += 1
            offset += len(items)
            if not items or offset >= int(data.get("count") or 0):
                break
        log(f"Bsale sucursal {office}: {n} registros")
    return stock


def bsale_stock_for_sku(sku):
    data = bsale_get("stocks.json", code=sku, expand="[office]")
    out = {}
    for it in data.get("items", []):
        office = str((it.get("office") or {}).get("id", ""))
        if office in OFFICES:
            out[office] = out.get(office, 0) + qty(it)
    return out


def payload(per_office):
    """Siempre las mismas claves y orden, para comparar sin falsos cambios."""
    return {o: int(per_office.get(o, 0)) for o in OFFICES}


# ---------------------------------------------------------------- Shopify
_token = {"value": SHOPIFY_TOKEN, "exp": float("inf") if SHOPIFY_TOKEN else 0}


def shopify_token():
    """Token de Admin API. Con client_id/secret se pide uno (dura 24 h) y se renueva solo."""
    if _token["value"] and time.time() < _token["exp"] - 300:
        return _token["value"]
    r = session.post(f"https://{SHOP}/admin/oauth/access_token", timeout=30, data={
        "grant_type": "client_credentials",
        "client_id": SHOPIFY_CLIENT_ID,
        "client_secret": SHOPIFY_CLIENT_SECRET})
    if r.status_code != 200:
        raise RuntimeError(f"Shopify no entregó token (HTTP {r.status_code}). Revisa SHOPIFY_SHOP, "
                           "client id/secret y que la app esté instalada en la tienda.")
    d = r.json()
    _token["value"] = d["access_token"]
    _token["exp"] = time.time() + int(d.get("expires_in", 86399))
    return _token["value"]


def gql(query, variables=None):
    url = f"https://{SHOP}/admin/api/{API_VERSION}/graphql.json"
    for intento in range(8):
        r = session.post(url, json={"query": query, "variables": variables or {}},
                         headers={"X-Shopify-Access-Token": shopify_token()}, timeout=60)
        if r.status_code == 429 or r.status_code >= 500:
            time.sleep(2 * (intento + 1)); continue
        r.raise_for_status()
        body = r.json()
        errs = body.get("errors") or []
        if any((e.get("extensions") or {}).get("code") == "THROTTLED" for e in errs):
            time.sleep(2 * (intento + 1)); continue
        if errs:
            raise RuntimeError(json.dumps(errs, ensure_ascii=False))
        return body["data"]
    raise RuntimeError("Shopify: demasiados reintentos")


Q_VARIANTS = """
query($after: String, $q: String) {
  productVariants(first: 250, after: $after, query: $q) {
    pageInfo { hasNextPage endCursor }
    nodes { id sku metafield(namespace: "%s", key: "%s") { value } }
  }
}""" % (NAMESPACE, KEY)


def shopify_variants(sku=None):
    """{sku: [(variantId, valorActual|None), ...]} (un SKU puede repetirse)."""
    out, after = {}, None
    q = f"sku:{sku}" if sku else None
    while True:
        d = gql(Q_VARIANTS, {"after": after, "q": q})["productVariants"]
        for v in d["nodes"]:
            s = (v.get("sku") or "").strip()
            if not s or (sku and s != sku):
                continue
            cur = (v.get("metafield") or {}).get("value")
            out.setdefault(s, []).append((v["id"], cur))
        if not d["pageInfo"]["hasNextPage"]:
            return out
        after = d["pageInfo"]["endCursor"]


M_SET = """
mutation($m: [MetafieldsSetInput!]!) {
  metafieldsSet(metafields: $m) { userErrors { field message code } }
}"""


def write_metafields(changes, dry_run):
    """changes = [(variantId, dict)]; metafieldsSet admite 25 por llamada."""
    if dry_run or not changes:
        return 0
    errores = 0
    for i in range(0, len(changes), 25):
        lote = [{"ownerId": vid, "namespace": NAMESPACE, "key": KEY, "type": "json",
                 "value": json.dumps(val, separators=(",", ":"))}
                for vid, val in changes[i:i + 25]]
        ue = gql(M_SET, {"m": lote})["metafieldsSet"]["userErrors"]
        for e in ue:
            errores += 1
            log("ERROR metafieldsSet:", e)
    return errores


def same(cur_value, new):
    try:
        return cur_value is not None and json.loads(cur_value) == new
    except ValueError:
        return False


# ---------------------------------------------------------------- sync
def sync(sku=None, dry_run=False):
    t0 = time.time()
    if sku:
        bsale = {sku: bsale_stock_for_sku(sku)}
    else:
        bsale = bsale_all_stock()
    shop = shopify_variants(sku)

    changes, sin_bsale = [], []
    for s, variants in shop.items():
        new = payload(bsale.get(s, {}))
        if s not in bsale:
            sin_bsale.append(s)
        for vid, cur in variants:
            if not same(cur, new):
                changes.append((vid, new))
                if dry_run or sku:
                    log(f"SKU {s}: {cur} -> {json.dumps(new)}")

    sin_shopify = sorted(set(bsale) - set(shop)) if not sku else []
    # Freno de seguridad: si Bsale devolvió muy poco, no se escribe nada (evita poner
    # «sin stock» en toda la tienda por una falla de la API o de la paginación).
    if not sku and shop and len(sin_bsale) > len(shop) * 0.5:
        log(f"ABORTADO: {len(sin_bsale)} de {len(shop)} SKU de Shopify no aparecen en Bsale. "
            "No se escribió nada.")
        return False
    errores = write_metafields(changes, dry_run)

    log(f"{'[dry-run] ' if dry_run else ''}{len(changes)} variantes "
        f"{'a actualizar' if dry_run else 'actualizadas'}, errores {errores}, "
        f"{len(shop)} SKU en Shopify, {round(time.time() - t0, 1)} s")
    if sin_bsale:
        log(f"SKU de Shopify que no están en Bsale ({len(sin_bsale)}): {', '.join(sorted(sin_bsale)[:30])}")
    if sin_shopify:
        log(f"SKU de Bsale sin variante en Shopify: {len(sin_shopify)} (normal: Bsale tiene más productos)")
    return errores == 0


# ---------------------------------------------------------------- webhook
class Hook(BaseHTTPRequestHandler):
    """Bsale envía POST {cpnId, resource, resourceId, topic:'stock', action, officeId, ...}.
    Activación: pedir a ayuda@bsale.app con la URL pública y el RUT/cpnId."""

    def do_POST(self):
        qs = parse_qs(urlparse(self.path).query)
        secret = os.environ.get("WEBHOOK_SECRET")
        if secret and qs.get("key", [None])[0] != secret:
            self.send_response(403); self.end_headers(); return
        raw = self.rfile.read(int(self.headers.get("Content-Length", 0) or 0))
        self.send_response(200); self.end_headers()
        try:
            body = json.loads(raw or b"{}")
            if body.get("topic") != "stock":
                return
            if str(body.get("officeId", "")) and str(body["officeId"]) not in OFFICES:
                return
            st = bsale_get(body["resource"].split("/v1/")[-1], expand="[variant]")
            code = ((st.get("variant") or {}).get("code") or "").strip()
            if code:
                sync(code)  # relee las 3 sucursales del SKU y escribe si cambió
        except Exception as e:  # nunca botar el servidor por un evento malo
            log("webhook error:", e)

    def do_GET(self):  # chequeo de salud
        self.send_response(200); self.end_headers(); self.wfile.write(b"ok")

    def log_message(self, *a):
        pass


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--full", action="store_true", help="sincroniza todo el catálogo")
    ap.add_argument("--sku", help="sincroniza un solo SKU")
    ap.add_argument("--dry-run", action="store_true", help="no escribe, solo muestra")
    ap.add_argument("--serve", action="store_true", help="receptor de webhooks de Bsale")
    ap.add_argument("--port", type=int, default=8080)
    a = ap.parse_args()

    faltan = [k for k, v in {"BSALE_TOKEN": BSALE_TOKEN, "SHOPIFY_SHOP": SHOP}.items() if not v]
    if not SHOPIFY_TOKEN and not (SHOPIFY_CLIENT_ID and SHOPIFY_CLIENT_SECRET):
        faltan.append("SHOPIFY_CLIENT_ID y SHOPIFY_CLIENT_SECRET")
    if faltan and (a.full or a.sku or a.serve):
        sys.exit("Faltan variables de entorno: " + ", ".join(faltan))

    if a.serve:
        log(f"Escuchando webhooks de Bsale en :{a.port}/bsale-stock")
        HTTPServer(("", a.port), Hook).serve_forever()
    elif a.full or a.sku:
        sys.exit(0 if sync(a.sku, a.dry_run) else 1)
    else:
        ap.print_help()


if __name__ == "__main__":
    main()
