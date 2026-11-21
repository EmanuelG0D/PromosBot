"""Decathlon Colombia: agregador de ofertas deportivas y liquidaciones (COP).

Extrae las liquidaciones directamente desde las páginas oficiales de descuentos
de Decathlon Colombia (https://www.decathlon.com.co/ofertas-decathlon/descuentos):
- Ofertas generales y paginadas (?from=40&size=40)
- Categorías especializadas: Ciclismo, Fitness, Running, Camping, Montaña, Deportes de Equipo.

Parsea el HTML generado por SSR de Next.js extrayendo título, enlace de producto,
imagen en alta resolución de mediadecathlon, precio actual y precio anterior,
calculando el porcentaje de descuento y aplicando los filtros de rigor.
"""
from __future__ import annotations

import re
from concurrent.futures import ThreadPoolExecutor

from core import http
from core.models import Deal

BASE_URL = "https://www.decathlon.com.co"
STORE_NAME = "Decathlon"
COUNTRY = "CO"
CURRENCY = "COP"
HILOS = 4

CATEGORIAS_DEFECTO = [
    "/ofertas-decathlon/descuentos",
    "/ofertas-decathlon/descuentos?from=40&size=40",
    "/ofertas-decathlon/descuentos-ciclismo",
    "/ofertas-decathlon/descuentos-fitness",
    "/ofertas-decathlon/descuentos-running",
    "/ofertas-decathlon/descuentos-camping",
    "/ofertas-decathlon/descuentos-deportes-equipo",
    "/ofertas-decathlon/descuentos-montana",
]


def extraer_deals_decathlon_html(html: str, min_descuento: float = 15.0) -> list[Deal]:
    """Parsea el HTML de una página de descuentos de Decathlon Colombia."""
    deals: list[Deal] = []
    if not html:
        return deals

    # Dividir por bloques de tarjeta de producto
    bloques = re.split(r'<div[^>]*class="[^"]*product-card__container[^"]*"', html)
    if len(bloques) <= 1:
        # Fallback alternativo: dividir por contenedor de imagen de producto
        bloques = re.split(r'<div[^>]*class="[^"]*product-card-image[^"]*"', html)

    for b in bloques[1:]:
        try:
            # 1. Enlace y Título
            link_m = re.search(r'href="(/p/[^"]+)"[^>]*>([^<]+)</a></h2>', b)
            if not link_m:
                link_m = re.search(r'<h2><a[^>]*href="(/p/[^"]+)"[^>]*>(.*?)</a></h2>', b, re.DOTALL)
                if not link_m:
                    continue

            rel_url = link_m.group(1).strip()
            title = re.sub(r'<[^>]+>', '', link_m.group(2)).strip()
            # Limpiar espacios dobles o saltos de línea en el título
            title = re.sub(r'\s+', ' ', title)
            if not title or not rel_url:
                continue

            p_url = f"{BASE_URL}{rel_url}"

            # Clave única a partir del identificador de modelo (ej. m9041555 o id numérico)
            key_m = re.search(r'/([^/]+)$', rel_url)
            pid = key_m.group(1) if key_m else rel_url

            # 2. Imagen en alta resolución
            img_m = re.search(r'src="(https://contents\.mediadecathlon\.com/[^"]+)"', b)
            if not img_m:
                img_m = re.search(r'srcSet="(https://contents\.mediadecathlon\.com/[^\s",]+)', b)
            img_url = img_m.group(1).replace('&amp;', '&') if img_m else None

            # 3. Precios (actual y anterior)
            price_m = re.search(r'Precio actual</span>\s*<!-- -->\$\s*([\d\.]+)', b)
            if not price_m:
                continue
            price = float(price_m.group(1).replace('.', '').strip())

            list_m = re.search(r'Precio anterior</span>\s*<!-- -->\$\s*([\d\.]+)', b)
            if list_m:
                list_price = float(list_m.group(1).replace('.', '').strip())
            else:
                list_price = price

            if list_price <= price or price <= 0:
                continue

            pct = round(((list_price - price) / list_price) * 100)
            if pct < min_descuento:
                continue

            notes = [f"🇨🇴 {STORE_NAME}", f"-{pct}% OFF", "🚲 Deportes y Gym"]
            if pct >= 40:
                notes.append("🔥 SÚPER LIQUIDACIÓN")

            deals.append(Deal(
                source="decathlon",
                store=STORE_NAME,
                country=COUNTRY,
                key=f"decathlon:{pid}",
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


def _una_pagina(ruta: str, min_descuento: float = 15.0) -> list[Deal]:
    """Descarga y procesa una página de categoría o descuento de Decathlon."""
    url = f"{BASE_URL}{ruta}" if ruta.startswith("/") else ruta
    try:
        raw_html = http.get_text(
            url,
            headers={
                "User-Agent": "curl/8.4.0",
                "Accept": "*/*",
            },
            timeout=15,
        )
        return extraer_deals_decathlon_html(raw_html, min_descuento=min_descuento)
    except Exception as exc:
        print(f"  [decathlon] aviso: error descargando '{ruta}': {exc}")
        return []


def fetch(categorias: list[str] | None = None,
          min_descuento: float = 15.0) -> list[Deal]:
    """Recolecta ofertas de Decathlon Colombia en paralelo."""
    cat_efectivas = categorias if categorias is not None else CATEGORIAS_DEFECTO
    if not cat_efectivas:
        return []

    deals: list[Deal] = []
    vistos: set[str] = set()

    with ThreadPoolExecutor(max_workers=HILOS) as pool:
        for lote in pool.map(lambda c: _una_pagina(c, min_descuento=min_descuento), cat_efectivas):
            for d in lote:
                if d.key in vistos:
                    continue
                vistos.add(d.key)
                deals.append(d)

    return deals
