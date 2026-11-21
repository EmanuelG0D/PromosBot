"""Simon Premium Outlets (EE. UU.): agregador de ofertas y liquidaciones outlet (USD).

Lee el catálogo directamente desde la plataforma Shopify de shop.simon.com a través
de sus endpoints públicos de colecciones y búsqueda predictiva:
- /collections/{handle}/products.json (liquidaciones, price drops y marcas outlet)
- /search/suggest.json?q={query}&resources[type]=product (búsqueda por término)

Filtra productos con descuento comprobable (list_price > price) y porcentaje mínimo.
Al ser compras en EE. UU., los productos se registran en USD para que el radar
calcule automáticamente el costo puesto en Colombia (TRM + flete).
"""
from __future__ import annotations

import json
import urllib.parse
from concurrent.futures import ThreadPoolExecutor

from core import http
from core.models import Deal

BASE_URL = "https://shop.simon.com"
STORE_NAME = "Simon Outlet"
COUNTRY = "US"
CURRENCY = "USD"
HILOS = 4

COLECCIONES_DEFECTO = [
    "curated-adidas-price-drops",
    "curated-adidas-extra-50",
    "store-coach-outlet",
    "curated-coach-outlet-sale",
    "curated-michael-kors-all",
    "curated-clarks-extra-40-off",
]


def extraer_deals_productos_json(raw_json: str | dict, min_descuento: float = 30.0) -> list[Deal]:
    """Parsea el listado de productos de una colección de Shopify (products.json)."""
    deals: list[Deal] = []
    try:
        data = json.loads(raw_json) if isinstance(raw_json, str) else raw_json
    except Exception:
        return []

    if not isinstance(data, dict):
        return []

    products = data.get("products") or []
    for p in products:
        try:
            pid = str(p.get("id") or "").strip()
            title = (p.get("title") or "").strip()
            handle = (p.get("handle") or "").strip()
            vendor = (p.get("vendor") or "").strip()
            if not pid or not title:
                continue

            variants = p.get("variants") or []
            if not variants:
                continue

            # Buscar la variante disponible con mejor descuento
            mejor_v = None
            max_disc_pct = 0.0
            for v in variants:
                if not v.get("available", True):
                    continue
                pr = float(v.get("price") or 0)
                cp = float(v.get("compare_at_price") or 0)
                if cp > pr and pr > 0:
                    disc_pct = round(((cp - pr) / cp) * 100)
                    if disc_pct > max_disc_pct:
                        max_disc_pct = disc_pct
                        mejor_v = (pr, cp, disc_pct)

            if not mejor_v or max_disc_pct < min_descuento:
                continue

            price, list_price, pct = mejor_v
            p_url = f"{BASE_URL}/products/{handle}" if handle else BASE_URL
            images = p.get("images") or []
            img_url = images[0].get("src") if images else None

            notes = [f"🇺🇸 {STORE_NAME}", f"-{pct}% OFF"]
            if vendor and not vendor.isdigit() and vendor.lower() not in title.lower():
                notes.append(vendor)

            deals.append(Deal(
                source="simon",
                store=STORE_NAME,
                country=COUNTRY,
                key=f"simon:{pid}",
                title=title,
                url=p_url,
                price=price,
                currency=CURRENCY,
                list_price=list_price,
                image=img_url,
                notes=notes,
                free_shipping_co=False,
                in_stock=True,
            ))
        except Exception:
            continue

    return deals


def extraer_deals_suggest_json(raw_json: str | dict, min_descuento: float = 30.0) -> list[Deal]:
    """Parsea productos devueltos por el buscador predictivo de Shopify (search/suggest.json)."""
    deals: list[Deal] = []
    try:
        data = json.loads(raw_json) if isinstance(raw_json, str) else raw_json
    except Exception:
        return []

    if not isinstance(data, dict):
        return []

    prods = data.get("resources", {}).get("results", {}).get("products", [])
    for p in prods:
        try:
            pid = str(p.get("id") or "").strip()
            title = (p.get("title") or "").strip()
            if not pid or not title:
                continue

            price = float(p.get("price") or 0)
            list_price = float(p.get("compare_at_price_max") or 0)
            if list_price <= price or price <= 0:
                continue

            pct = round(((list_price - price) / list_price) * 100)
            if pct < min_descuento:
                continue

            rel_url = (p.get("url") or "").split("?")[0]
            p_url = f"{BASE_URL}{rel_url}" if rel_url.startswith("/") else rel_url
            img_url = p.get("image") or p.get("featured_image")

            deals.append(Deal(
                source="simon",
                store=STORE_NAME,
                country=COUNTRY,
                key=f"simon:{pid}",
                title=title,
                url=p_url,
                price=price,
                currency=CURRENCY,
                list_price=list_price,
                image=img_url,
                notes=[f"🇺🇸 {STORE_NAME}", f"-{pct}% OFF"],
                free_shipping_co=False,
                in_stock=True,
            ))
        except Exception:
            continue

    return deals


def _una_coleccion(handle: str, por_consulta: int = 24, min_descuento: float = 30.0) -> list[Deal]:
    """Descarga y extrae productos de una colección específica de Simon."""
    url = f"{BASE_URL}/collections/{handle}/products.json?limit={por_consulta}"
    try:
        raw = http.get_text(url, timeout=12)
        return extraer_deals_productos_json(raw, min_descuento=min_descuento)
    except Exception as exc:
        print(f"  [simon] aviso: error en colección '{handle}': {exc}")
        return []


def _una_busqueda(query: str, por_consulta: int = 24, min_descuento: float = 30.0) -> list[Deal]:
    """Busca ofertas por término en el buscador de Simon."""
    q_enc = urllib.parse.quote_plus(query.strip())
    url = f"{BASE_URL}/search/suggest.json?q={q_enc}&resources[type]=product&resources[limit]={por_consulta}"
    try:
        raw = http.get_text(url, timeout=12)
        return extraer_deals_suggest_json(raw, min_descuento=min_descuento)
    except Exception as exc:
        print(f"  [simon] aviso: error en búsqueda '{query}': {exc}")
        return []


def fetch(consultas: list[str] | None = None,
          colecciones: list[str] | None = None,
          por_consulta: int = 24,
          min_descuento: float = 30.0) -> list[Deal]:
    """Recolecta ofertas de Simon Premium Outlets en paralelo."""
    col_efectivas = colecciones if colecciones is not None else COLECCIONES_DEFECTO
    q_efectivas = consultas or []

    tareas = []
    for c in col_efectivas:
        if c.strip():
            tareas.append(("col", c.strip()))
    for q in q_efectivas:
        if q.strip():
            tareas.append(("search", q.strip()))

    if not tareas:
        return []

    deals: list[Deal] = []
    vistos: set[str] = set()

    def _ejecutar(t):
        tipo, valor = t
        if tipo == "col":
            return _una_coleccion(valor, por_consulta=por_consulta, min_descuento=min_descuento)
        else:
            return _una_busqueda(valor, por_consulta=por_consulta, min_descuento=min_descuento)

    with ThreadPoolExecutor(max_workers=HILOS) as pool:
        for lote in pool.map(_ejecutar, tareas):
            for d in lote:
                if d.key in vistos:
                    continue
                vistos.add(d.key)
                deals.append(d)

    return deals
