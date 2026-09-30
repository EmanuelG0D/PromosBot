"""IKEA Colombia: scraper de ofertas, precios especiales y liquidaciones (COP).

Extrae productos directamente de la tienda oficial ikea.com/co/es de sus secciones
de liquidación ("Últimas unidades", "Precio más bajo" y ofertas especiales), capturando
nombre del producto, medidas/descripción, precios en pesos colombianos, precios anteriores
tachados y fotografías oficiales en alta resolución.
"""
from __future__ import annotations

import html as html_lib
import re
from concurrent.futures import ThreadPoolExecutor

from core import http
from core.models import Deal

BASE_URL = "https://www.ikea.com"

URLS_DEFAULT = [
    "https://www.ikea.com/co/es/cat/last-chance/?filters=f-special-price%3Atrue",
    "https://www.ikea.com/co/es/cat/lowest-price/",
]


def extraer_deals_html(html_str: str) -> list[Deal]:
    """Parsea el HTML de un catálogo o listado de IKEA Colombia y genera Deals."""
    if not html_str:
        return []

    cards = re.findall(
        r'(data-product-number="[0-9]+".*?)(?=data-product-number="|\Z)',
        html_str,
        re.DOTALL,
    )
    deals: list[Deal] = []
    vistos: set[str] = set()

    for c in cards:
        pid_m = re.search(r'data-product-number="([0-9]+)"', c)
        pname_m = re.search(r'data-product-name="([^"]+)"', c)
        if not pid_m or not pname_m:
            continue

        pid = pid_m.group(1).strip()
        if pid in vistos:
            continue
        vistos.add(pid)

        name = html_lib.unescape(pname_m.group(1).strip())
        desc_m = re.search(r'class="[^"]*plp-price-module__description[^"]*">([^<]+)<', c)
        desc = html_lib.unescape(desc_m.group(1).strip()) if desc_m else ""
        titulo = f"{name} {desc}".strip()[:180]

        # Extracción de precios en COP
        price_data_m = re.search(r'data-price="([0-9]+)"', c)
        price_now_m = re.search(r'plp-price-module__current-price.*?plp-price__integer">([\d.]+)<', c, re.DOTALL)
        if price_data_m:
            p_now = float(price_data_m.group(1))
        elif price_now_m:
            p_now = float(price_now_m.group(1).replace(".", ""))
        else:
            continue

        if p_now <= 0:
            continue

        price_prev_m = re.search(r'plp-price--comparison[^>]*>.*?plp-price__integer">([\d.]+)<', c, re.DOTALL)
        if not price_prev_m:
            price_prev_m = re.search(r'plp-price--comparison[^>]*>.*?plp-price__sr-text">\s*\$?\s*([\d.]+)', c, re.DOTALL)
        if not price_prev_m:
            price_prev_m = re.search(r'plp-price--comparison[^>]*>.*?\$?\s*([\d]{1,3}(?:\.[\d]{3})+|\d{4,})', c, re.DOTALL)
        p_prev = float(price_prev_m.group(1).replace(".", "")) if price_prev_m else None

        # Solo admitir productos con rebaja real comprobada
        if not p_prev or p_prev <= p_now:
            continue

        # Enlace canónico directo a la tienda
        url_m = re.search(r'href="(https://www\.ikea\.com/co/es/p/[^"]+)"', c)
        p_url = url_m.group(1) if url_m else f"https://www.ikea.com/co/es/p/{pid}/"

        # Imagen oficial en alta resolución (se remueve ?f=xxs si viene presente)
        img_m = re.search(r'src="(https://www\.ikea\.com/co/es/images/products/[^"?]+)', c)
        img = img_m.group(1) if img_m else None

        pct = round(((p_prev - p_now) / p_prev) * 100)
        notes = ["🇨🇴 IKEA Colombia", f"-{pct}% OFF"]

        badge_m = re.search(r'plp-product-badge[^>]*>([^<]+)<', c)
        if badge_m:
            notes.append(html_lib.unescape(badge_m.group(1).strip()))

        deal = Deal(
            source="ikea",
            store="IKEA",
            country="CO",
            key=f"ikea:{pid}",
            title=titulo,
            url=p_url,
            price=p_now,
            currency="COP",
            list_price=p_prev,
            image=img,
            notes=notes,
            free_shipping_co=False,
        )
        deals.append(deal)

    return deals


def fetch(urls: list[str] | None = None, timeout: float = 8.0) -> list[Deal]:
    """Descarga concurrentemente las páginas de ofertas de IKEA Colombia y devuelve ofertas consolidadas."""
    rutas = urls or URLS_DEFAULT

    def _cargar_una(u: str) -> list[Deal]:
        try:
            html_txt = http.get_text(u, timeout=int(timeout))
            return extraer_deals_html(html_txt)
        except Exception as exc:
            print(f"  [ikea] error descargando {u}: {exc}")
            return []

    with ThreadPoolExecutor(max_workers=min(len(rutas), 4)) as pool:
        resultados = list(pool.map(_cargar_una, rutas))

    consolidadas: dict[str, Deal] = {}
    for lote in resultados:
        for d in lote:
            if d.key not in consolidadas:
                consolidadas[d.key] = d

    lista = list(consolidadas.values())
    # Ordenar priorizando las que tienen mayor descuento real
    lista.sort(key=lambda d: -(d.discount_verificable or 0))
    return lista
