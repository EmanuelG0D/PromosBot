"""Ofertas de EE. UU. leidas del RSS publico de Slickdeals.

La comunidad ya probo cupones, verifico stock y voto la oferta; nosotros solo
leemos XML publico, sin scrapear Amazon y sin exponernos a bloqueos de IP.
"""
from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import quote_plus

from core import http
from core import filtros
from core.coupons import extract_coupons, needs_clipping
from core.models import Deal

FEED = "https://slickdeals.net/newsearch.php?mode=popular&q={q}&rss=1"
PORTADA = "https://slickdeals.net/newsearch.php?mode=frontpage&rss=1"
HILOS = 4

_TAGS = re.compile(r"<[^>]+>")
_PRECIO = re.compile(r"\$\s?([\d,]+(?:\.\d{1,2})?)")
_IMAGEN = re.compile(r'<img[^>]+src="([^"]+)"')
_NS = {"content": "http://purl.org/rss/1.0/modules/content/"}
_AT_TIENDA = re.compile("\\bat\\s+([A-Za-z][A-Za-z0-9'&.\\- ]{1,20}?)!?\\s*$", re.IGNORECASE)
_TIENDAS = [
    "Amazon", "eBay", "Walmart", "Target", "Costco", "Best Buy", "Newegg",
    "Nike", "Adidas", "Reebok", "Puma", "Under Armour", "Woot", "Home Depot",
    "Macy's", "Kohl's", "Dell", "Lenovo", "B&H", "StockX", "Sam's Club",
]
MARCAS_PERMITIDAS = ("Amazon", "eBay", "Nike", "Adidas", "Puma")


def es_tienda_permitida(nombre_tienda: str) -> bool:
    """True si la tienda identificada es una de las 5 marcas autorizadas para Colombia."""
    return any(p.lower() in (nombre_tienda or "").lower() for p in MARCAS_PERMITIDAS)


def _texto_plano(html: str | None) -> str:
    if not html:
        return ""
    return _TAGS.sub(" ", html).replace("&amp;", "&").replace("&nbsp;", " ")


def _precio(texto: str) -> float | None:
    match = _PRECIO.search(texto)
    if not match:
        return None
    try:
        return float(match.group(1).replace(",", ""))
    except ValueError:
        return None


def _tienda(titulo: str) -> str:
    """Nombra el comercio solo cuando el titulo lo dice sin ambiguedad.

    El RSS de Slickdeals no publica el comercio (su <category> siempre dice
    "Forum"), asi que quedan dos senales fiables: el sufijo "at <tienda>" y el
    titulo que arranca con la marca. Rastrear nombres sueltos por todo el texto
    producia errores como atribuirle a Adidas una oferta de perfumes de Woot,
    o a Target una de tarjetas de regalo que solo la mencionaba de pasada.
    """
    final = _AT_TIENDA.search(titulo)
    if final:
        nombre = final.group(1).strip(" .!")
        for conocida in _TIENDAS:
            if conocida.lower() == nombre.lower():
                return f"{conocida} (via Slickdeals)"
        if 2 <= len(nombre) <= 20:
            return f"{nombre} (via Slickdeals)"

    for conocida in _TIENDAS:
        if titulo.lower().startswith(conocida.lower()):
            return f"{conocida} (via Slickdeals)"

    return "Slickdeals"   # mejor no decir nada que decir algo falso


def _una_consulta(consulta: str, por_consulta: int, vetadas=None,
                  incluir=None) -> list[Deal]:
    # La consulta especial "portada" lee el feed de primera pagina: son las
    # ofertas que la comunidad voto hacia arriba, no resultados de busqueda.
    url = PORTADA if consulta == "portada" else FEED.format(q=quote_plus(consulta))
    try:
        xml = http.get_text(url)
        raiz = ET.fromstring(xml)
    except Exception as exc:
        print(f"  [slickdeals] {consulta}: {exc}")
        return []

    ofertas: list[Deal] = []
    for item in raiz.findall("./channel/item")[:por_consulta]:
        titulo = (item.findtext("title") or "").strip()
        enlace = (item.findtext("link") or "").strip()
        if not titulo or not enlace:
            continue
        if filtros.descartado(titulo, vetadas):
            continue
        if not filtros.pertinente(titulo, incluir):
            continue

        tienda_detectada = _tienda(titulo)
        marca_en_titulo = any(re.search(r"\b" + m.lower() + r"\b", titulo.lower()) for m in MARCAS_PERMITIDAS)
        # Filtrar comercios no autorizados (Target, Macy's, etc.)
        if not es_tienda_permitida(tienda_detectada) and not marca_en_titulo:
            continue

        # La foto del producto viene en content:encoded, no en description.
        enriquecido = item.findtext("content:encoded", namespaces=_NS) or ""
        hallada = _IMAGEN.search(enriquecido)
        foto = hallada.group(1) if hallada else None

        descripcion = _texto_plano(item.findtext("description"))
        cupones = extract_coupons(titulo, descripcion)
        notas = []
        if needs_clipping(titulo, descripcion):
            notas.append("Requiere activar el cupon (clip) en la pagina del producto")

        precio_val = _precio(titulo) or _precio(descripcion)
        texto_envio = (titulo + " " + descripcion).lower()
        es_amazon = "amazon" in tienda_detectada.lower()
        envio_gratis_co = (
            (es_amazon and precio_val is not None and precio_val >= 35.0)
            or "free shipping to colombia" in texto_envio
            or "envio gratis a colombia" in texto_envio
            or "ships to colombia" in texto_envio
        )

        ofertas.append(Deal(
            source="slickdeals",
            store=tienda_detectada,
            country="US",
            key="slickdeals:" + (item.findtext("guid") or enlace).strip(),
            title=titulo,
            url=enlace,
            price=precio_val,
            currency="USD",
            coupons=cupones,
            notes=notas,
            image=foto,
            free_shipping_co=envio_gratis_co,
        ))
    return ofertas


def fetch(consultas: list[str], por_consulta: int = 12, vetadas=None,
          incluir=None) -> list[Deal]:
    """Las busquedas salen en paralelo: son lecturas de RSS, no scraping."""
    ofertas: list[Deal] = []
    vistos: set[str] = set()

    with ThreadPoolExecutor(max_workers=HILOS) as pool:
        for lote in pool.map(
                lambda c: _una_consulta(c, por_consulta, vetadas, incluir), consultas):
            for deal in lote:
                if deal.key in vistos:
                    continue
                vistos.add(deal.key)
                ofertas.append(deal)
    return ofertas
