"""Scraper de ofertas verificadas de El Promo Hunter (elpromohunter.com).

Extrae las ofertas estructuradas del payload nativo (Next.js App Router)
de elpromohunter.com. Filtra estrictamente las ofertas con envío gratis directo
a Colombia (sin casilleros), con precio en COP, cupón y enlace limpio a Amazon.
"""
from __future__ import annotations

import json
import re
from urllib.parse import urlparse, parse_qs, urlencode, urlunparse

from core import http
from core import filtros
from core.models import Deal

BASE_URL = "https://elpromohunter.com/?page={p}"
_INITIAL_DEALS_RE = re.compile(r'initialDeals\\":(\[.*?\])(?:,\\|\})')
_INITIAL_DEALS_FALLBACK_RE = re.compile(r'"initialDeals":(\[.*?\])(?:,\s*"|\})')
_TAG_PARAM_RE = re.compile(r"[?&]tag=[^&]+")


def limpiar_enlace_amazon(url: str | None) -> str:
    """Limpia tags de afiliados ajenos de la URL para dejar un enlace directo limpio."""
    if not url:
        return ""
    try:
        parsed = urlparse(url)
        params = parse_qs(parsed.query)
        # Eliminar tags de afiliados ajenos si existen
        params.pop("tag", None)
        nueva_query = urlencode(params, doseq=True)
        return urlunparse((
            parsed.scheme,
            parsed.netloc,
            parsed.path,
            parsed.params,
            nueva_query,
            parsed.fragment,
        ))
    except Exception:
        return _TAG_PARAM_RE.sub("", url).rstrip("?&")


def extraer_deals_html(html: str) -> list[dict]:
    """Extrae la lista de diccionarios de ofertas del JSON incrustado en el HTML."""
    if not html:
        return []
    match = _INITIAL_DEALS_RE.search(html)
    if not match:
        match = _INITIAL_DEALS_FALLBACK_RE.search(html)
    if not match:
        return []

    raw = match.group(1).replace('\\"', '"').replace('\\\\', '\\')
    try:
        data = json.loads(raw)
        return data if isinstance(data, list) else []
    except Exception as exc:
        print(f"  [promohunter] Error al parsear JSON: {exc}")
        return []


def parse_deal(item: dict) -> Deal | None:
    """Convierte un objeto de oferta crudo de PromoHunter al modelo Deal."""
    # Filtro estricto: Solo ofertas con envío gratis directo a Colombia (casillero == 0)
    if item.get("casillero") != 0:
        return None

    deal_id = item.get("id")
    titulo = (item.get("titulo") or "").strip()
    if not titulo or not deal_id:
        return None

    precio_oferta = item.get("precio_oferta")
    if precio_oferta is None or precio_oferta <= 0:
        return None

    precio_original = item.get("precio_original")
    if precio_original is not None and precio_original <= 0:
        precio_original = None

    enlace_crudo = item.get("enlace") or item.get("enlace_afiliado") or ""
    enlace = limpiar_enlace_amazon(enlace_crudo)
    if not enlace:
        return None

    asin = item.get("asin") or ""
    key = f"promohunter:{deal_id}"

    # Cupones y notas de calificación
    cupon_raw = (item.get("cupones") or "").strip()
    cupones: list[str] = []
    notas: list[str] = []

    if cupon_raw and "no necesita" not in cupon_raw.lower():
        # Extraer códigos alfanuméricos en mayúsculas
        codigos = re.findall(r"\b[A-Z0-9]{5,20}\b", cupon_raw)
        cupones = [c for c in codigos if not c.isdigit()]
        if "seleccionable" in cupon_raw.lower() or "clip" in cupon_raw.lower():
            notas.append("Cupón seleccionable en Amazon")
        elif not cupones and cupon_raw:
            notas.append(f"Cupón: {cupon_raw}")

    rating = item.get("rating")
    num_resenas = item.get("num_resenas")
    if rating and str(rating) != "0":
        if num_resenas:
            notas.append(f"⭐️ {rating} ({num_resenas} reseñas)")
        else:
            notas.append(f"⭐️ {rating}")

    foto = f"https://elpromohunter.com/api/product-image/{deal_id}"

    return Deal(
        source="promohunter",
        store="Amazon (vía PromoHunter)",
        country="CO",
        key=key,
        title=titulo,
        url=enlace,
        price=float(precio_oferta),
        currency="COP",
        list_price=float(precio_original) if precio_original else None,
        coupons=cupones,
        notes=notas,
        image=foto,
        free_shipping_co=True,
    )


def fetch(
    paginas: int = 2,
    incluir: list[str] | None = None,
    excluir: list[str] | None = None,
) -> list[Deal]:
    """Descarga y procesa ofertas de El Promo Hunter."""
    ofertas: list[Deal] = []
    vistos: set[str] = set()

    for p in range(1, paginas + 1):
        url = BASE_URL.format(p=p)
        try:
            html = http.get_text(url)
            raw_deals = extraer_deals_html(html)
        except Exception as exc:
            print(f"  [promohunter] Error al consultar página {p}: {exc}")
            continue

        for item in raw_deals:
            deal = parse_deal(item)
            if not deal:
                continue

            if filtros.descartado(deal.title, excluir):
                continue
            if not filtros.pertinente(deal.title, incluir):
                continue

            if deal.key in vistos:
                continue
            vistos.add(deal.key)
            ofertas.append(deal)

    return ofertas
