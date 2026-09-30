"""Cascos de moto en Colombia: agregador multi-tienda de ofertas y liquidaciones (COP).

Recolecta ofertas de cascos certificados y protección para motociclistas consultando
las APIs nativas de las principales tiendas especializadas del país:
- Inducascos (VTEX Catalog API)
- DS2M2 (Shopify Products API)
- Rider Site (Shopify Products API)

Filtra estrictamente productos que tengan descuento real comprobable (list_price > price).
"""
from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor

from core import http
from core.models import Deal

TIENDAS_CONFIG = {
    "inducascos": {
        "nombre": "Inducascos",
        "url_base": "https://www.inducascos.com",
        "api_url": "https://www.inducascos.com/api/catalog_system/pub/products/search/cascos?_from=0&_to=49&O=OrderByBestDiscountDESC",
        "tipo": "vtex",
    },
    "ds2m2": {
        "nombre": "DS2M2",
        "url_base": "https://ds2m2.com",
        "api_url": "https://ds2m2.com/collections/cascos/products.json?limit=50",
        "tipo": "shopify",
    },
    "ridersite": {
        "nombre": "Rider Site",
        "url_base": "https://ridersite.com.co",
        "api_url": "https://ridersite.com.co/collections/cascos/products.json?limit=50",
        "tipo": "shopify",
    },
    "cascosromo": {
        "nombre": "Cascos Romo",
        "url_base": "https://cascosromo.com",
        "api_url": "https://cascosromo.com/collections/cascos/products.json?limit=50",
        "tipo": "shopify",
    },
    "zonabiker": {
        "nombre": "Zonabiker",
        "url_base": "https://zonabiker.com.co",
        "api_url": "https://zonabiker.com.co/collections/llantas-para-motos/products.json?limit=50",
        "tipo": "shopify",
    },
}


def extraer_deals_vtex_inducascos(raw_json: str | list) -> list[Deal]:
    """Parsea productos devueltos por la API de catálogo VTEX de Inducascos."""
    deals: list[Deal] = []
    try:
        items = json.loads(raw_json) if isinstance(raw_json, str) else raw_json
    except Exception:
        return []

    if not isinstance(items, list):
        return []

    for p in items:
        try:
            pid = str(p.get("productId") or "").strip()
            title = (p.get("productName") or "").strip()
            link = (p.get("link") or "").strip()
            brand = (p.get("brand") or "").strip()

            skus = p.get("items") or []
            if not skus:
                continue

            sku0 = skus[0]
            sellers = sku0.get("sellers") or []
            if not sellers:
                continue

            offer = sellers[0].get("commertialOffer") or {}
            price = float(offer.get("Price") or 0)
            list_price = float(offer.get("ListPrice") or 0)
            available = int(offer.get("AvailableQuantity") or 0)

            # Validar precio y stock
            if price <= 0 or available <= 0:
                continue

            # Solo admitir productos con descuento comprobado
            if list_price <= price:
                continue

            images = sku0.get("images") or []
            img_url = images[0].get("imageUrl") if images else None

            pct = round(((list_price - price) / list_price) * 100)
            notes = ["🇨🇴 Inducascos", f"-{pct}% OFF"]
            if brand and brand.lower() not in title.lower():
                notes.append(brand)

            deal = Deal(
                source="cascos",
                store="Inducascos",
                country="CO",
                key=f"cascos:inducascos:{pid}",
                title=title,
                url=link,
                price=price,
                currency="COP",
                list_price=list_price,
                image=img_url,
                notes=notes,
                free_shipping_co=False,
                in_stock=True,
            )
            deals.append(deal)
        except Exception:
            continue

    return deals


def extraer_deals_shopify(raw_json: str | dict, store_slug: str, store_nombre: str, url_base: str) -> list[Deal]:
    """Parsea productos devueltos por la API de productos de Shopify (DS2M2 o Rider Site)."""
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

            variants = p.get("variants") or []
            if not variants:
                continue

            # Buscar la variante con mejor descuento disponible
            v_elegida = None
            max_desc = 0.0
            for v in variants:
                if not v.get("available", True):
                    continue
                pr = float(v.get("price") or 0)
                comp = float(v.get("compare_at_price") or 0)
                if comp > pr and pr > 0:
                    desc_ratio = (comp - pr) / comp
                    if desc_ratio > max_desc:
                        max_desc = desc_ratio
                        v_elegida = v

            if not v_elegida:
                continue

            price = float(v_elegida.get("price") or 0)
            list_price = float(v_elegida.get("compare_at_price") or 0)
            if list_price <= price or price <= 0:
                continue

            p_url = f"{url_base.rstrip('/')}/products/{handle}" if handle else url_base
            images = p.get("images") or []
            img_url = images[0].get("src") if images else None

            pct = round(((list_price - price) / list_price) * 100)
            notes = [f"🇨🇴 {store_nombre}", f"-{pct}% OFF"]
            if vendor and vendor.lower() not in title.lower():
                notes.append(vendor)

            deal = Deal(
                source="cascos",
                store=store_nombre,
                country="CO",
                key=f"cascos:{store_slug}:{pid}",
                title=title,
                url=p_url,
                price=price,
                currency="COP",
                list_price=list_price,
                image=img_url,
                notes=notes,
                free_shipping_co=False,
                in_stock=True,
            )
            deals.append(deal)
        except Exception:
            continue

    return deals


def fetch(timeout: float = 8.0) -> list[Deal]:
    """Consulta en paralelo todas las tiendas de cascos y devuelve ofertas consolidadas."""
    def _consultar_tienda(clave: str) -> list[Deal]:
        cfg = TIENDAS_CONFIG.get(clave)
        if not cfg:
            return []
        try:
            txt = http.get_text(cfg["api_url"], timeout=int(timeout))
            if cfg["tipo"] == "vtex":
                return extraer_deals_vtex_inducascos(txt)
            elif cfg["tipo"] == "shopify":
                return extraer_deals_shopify(txt, clave, cfg["nombre"], cfg["url_base"])
            return []
        except Exception as exc:
            print(f"  [cascos] error consultando {cfg.get('nombre')}: {exc}")
            return []

    claves = list(TIENDAS_CONFIG.keys())
    with ThreadPoolExecutor(max_workers=len(claves)) as pool:
        resultados = list(pool.map(_consultar_tienda, claves))

    consolidadas: dict[str, Deal] = {}
    for lote in resultados:
        for d in lote:
            if d.key not in consolidadas:
                consolidadas[d.key] = d

    lista = list(consolidadas.values())
    # Ordenar por mejor descuento relativo comprobable
    lista.sort(key=lambda d: -(d.discount_verificable or 0))
    return lista
