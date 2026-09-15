"""Scraper de ofertas verificadas de Milo Derrocha (Telegram @miloderrocha).

Extrae las ofertas del canal publico t.me/s/miloderrocha. Trae productos de
Amazon con envio gratis a Colombia, con precio de oferta, precio anterior
en COP, y fotos en alta definicion.
"""
from __future__ import annotations

import html
import re
import urllib.request
from concurrent.futures import ThreadPoolExecutor

from core import filtros
from core import http
from core.models import Deal

CANAL_URL = "https://t.me/s/miloderrocha"
_TAG_PARAM_RE = re.compile(r"[?&]tag=[^&]+")
_ASIN_RE = re.compile(r"/(?:dp|product|gp/product)/([A-Z0-9]{10})")


def resolver_enlace_amazon(url: str, timeout: float = 3.0) -> str:
    """Sigue la redireccion de joylink.io para extraer la URL limpia con ASIN de Amazon."""
    if not url:
        return ""
    if "joylink.io" not in url:
        return _TAG_PARAM_RE.sub("", url).rstrip("?&")
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"}, method="HEAD")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            final_url = resp.geturl()
            asin_match = _ASIN_RE.search(final_url)
            if asin_match:
                return f"https://www.amazon.com/dp/{asin_match.group(1)}"
            return _TAG_PARAM_RE.sub("", final_url).rstrip("?&")
    except Exception:
        return url


def parse_monto_cop(texto: str | None) -> float | None:
    """Convierte cadenas de precio en pesos colombianos a float (ej: '117.164' -> 117164.0)."""
    if not texto:
        return None
    digitos = re.sub(r"[^\d]", "", texto)
    try:
        return float(digitos) if digitos else None
    except ValueError:
        return None


def extraer_deals_html(html_str: str, por_canal: int = 20) -> list[Deal]:
    """Parsea el HTML publico de Telegram t.me/s/miloderrocha."""
    if not html_str:
        return []

    bloques = re.findall(r'data-post="([^"]+)"(.*?)(?=data-post="|\Z)', html_str, re.S)
    candidatos_brutos: list[dict] = []

    for post_id, bloque in bloques:
        texto_m = re.search(r'js-message_text"[^>]*>(.*?)</div>', bloque, re.S)
        if not texto_m:
            continue
        raw_texto = texto_m.group(1)

        # Precio de oferta
        precio_m = re.search(r'Precio:\s*(?:&#036;|\$)\s*([0-9.,]+)', raw_texto, re.IGNORECASE)
        if not precio_m:
            continue

        precio = parse_monto_cop(precio_m.group(1))
        if not precio or precio <= 0:
            continue

        # Precio de lista anterior
        antes_m = re.search(r'Antes:\s*(?:&#036;|\$)\s*([0-9.,]+)', raw_texto, re.IGNORECASE)
        precio_antes = parse_monto_cop(antes_m.group(1)) if antes_m else None

        # Título: extraer la primera línea limpia del mensaje antes de la palabra Precio
        lineas = [l.strip() for l in re.split(r'<br\s*/?>|\n', raw_texto) if l.strip()]
        titulo = ""
        for l in lineas:
            limpia = re.sub(r'<[^>]+>', '', l).strip()
            if "precio:" in limpia.lower():
                break
            if limpia:
                titulo = limpia
                break
        if not titulo or len(titulo) < 5:
            idx = raw_texto.lower().find("precio:")
            if idx != -1:
                titulo = re.sub(r'<[^>]+>', '', raw_texto[:idx]).strip()
        titulo = " ".join(html.unescape(titulo).split())[:180]

        # Enlace externo
        enlaces = re.findall(r'href="(https?://[^"]+)"', bloque)
        enlaces_oferta = [l for l in enlaces if "t.me" not in l]
        if not enlaces_oferta:
            continue

        # Foto del producto
        foto_m = re.search(r"background-image:\s*url\(['\"]?(https://[^)'\"]+)", bloque)
        foto = foto_m.group(1) if foto_m else None

        candidatos_brutos.append({
            "post_id": post_id,
            "titulo": titulo,
            "precio": precio,
            "precio_antes": precio_antes,
            "enlace_crudo": enlaces_oferta[0],
            "foto": foto,
        })

    # Tomar las ofertas mas recientes hasta por_canal
    candidatos = candidatos_brutos[-por_canal:] if len(candidatos_brutos) > por_canal else candidatos_brutos

    # Resolver enlaces joylink en paralelo para no frenar la ronda
    with ThreadPoolExecutor(max_workers=5) as pool:
        urls_resueltas = list(pool.map(lambda c: resolver_enlace_amazon(c["enlace_crudo"]), candidatos))

    deals: list[Deal] = []
    for c, url_limpia in zip(candidatos, urls_resueltas):
        m = re.search(r"/(?:dp|gp/product)/([A-Z0-9]{10})", url_limpia, re.IGNORECASE)
        key = f"amazon:{m.group(1).upper()}" if m else f"miloderrocha:{c['post_id']}"
        deal = Deal(
            source="miloderrocha",
            store="Amazon",
            country="CO",
            key=key,
            title=c["titulo"],
            url=url_limpia,
            price=c["precio"],
            currency="COP",
            list_price=c["precio_antes"],
            image=c["foto"],
            free_shipping_co=True,
        )
        deals.append(deal)

    return deals


def fetch(
    por_canal: int = 20,
    incluir: list[str] | None = None,
    excluir: list[str] | None = None,
) -> list[Deal]:
    """Descarga las publicaciones recientes de Milo Derrocha y las convierte en Deals."""
    try:
        html_content = http.get_text(CANAL_URL)
    except Exception as exc:
        print(f"  [miloderrocha] Error al consultar canal: {exc}")
        return []

    candidatas = extraer_deals_html(html_content, por_canal=por_canal)
    ofertas: list[Deal] = []
    vistos: set[str] = set()

    for deal in candidatas:
        if deal.key in vistos:
            continue
        if filtros.descartado(deal.title, excluir):
            continue
        if not filtros.pertinente(deal.title, incluir):
            continue

        vistos.add(deal.key)
        ofertas.append(deal)

    return ofertas
