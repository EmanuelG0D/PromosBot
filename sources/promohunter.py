"""Scraper de ofertas verificadas de El Promo Hunter (elpromohunter.com).

Extrae las ofertas estructuradas del payload nativo (Next.js App Router)
de elpromohunter.com. Filtra estrictamente las ofertas con envío gratis directo
a Colombia (sin casilleros), con precio en COP, cupón y enlace limpio a Amazon.
"""
from __future__ import annotations

import datetime as dt
import json
import re
import urllib.request
from urllib.parse import urlparse, parse_qs, urlencode, urlunparse

from core import http
from core import filtros
from core.models import Deal

BASE_URL = "https://elpromohunter.com/?page={p}"
_INITIAL_DEALS_RE = re.compile(r'initialDeals\\":(\[.*?\])(?:,\\|\})')
_INITIAL_DEALS_FALLBACK_RE = re.compile(r'"initialDeals":(\[.*?\])(?:,\s*"|\})')
_TAG_PARAM_RE = re.compile(r"[?&]tag=[^&]+")
_ACORTADORES_AMAZON = ("a.co", "amzn.to", "joylink.io")
_ASIN_RE = re.compile(r"/(?:dp|product|gp/product)/([A-Z0-9]{10})", re.IGNORECASE)


def resolver_enlace_acortado_amazon(url: str, timeout: float = 2.5) -> str:
    """Sigue la redirección de enlaces cortos (a.co, amzn.to, joylink) para extraer el enlace canónico con ASIN."""
    if not url:
        return ""
    if not any(dom in url for dom in _ACORTADORES_AMAZON):
        return url
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"}, method="HEAD")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.geturl() or url
    except Exception:
        return url


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


def parse_deal(item: dict, max_horas: float = 3.0) -> Deal | None:
    """Convierte un objeto de oferta crudo de PromoHunter al modelo Deal."""
    # Filtro de frescura: descartar ofertas con más de max_horas de antigüedad
    ts_str = item.get("ts")
    if ts_str and max_horas > 0:
        try:
            ts_pub = dt.datetime.fromisoformat(ts_str)
            if ts_pub.tzinfo is None:
                ts_pub = ts_pub.replace(tzinfo=dt.timezone(dt.timedelta(hours=-5)))
            ahora = dt.datetime.now(ts_pub.tzinfo)
            edad_horas = (ahora - ts_pub).total_seconds() / 3600.0
            if edad_horas > max_horas:
                return None
        except Exception:
            pass

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

    asin = (item.get("asin") or "").strip().upper()
    if not asin:
        m = _ASIN_RE.search(enlace)
        if m:
            asin = m.group(1).upper()
        elif any(dom in enlace for dom in _ACORTADORES_AMAZON):
            enlace_expandido = resolver_enlace_acortado_amazon(enlace)
            m2 = _ASIN_RE.search(enlace_expandido)
            if m2:
                asin = m2.group(1).upper()

    if asin:
        enlace = f"https://www.amazon.com/dp/{asin}"

    key = f"amazon:{asin}" if asin else f"promohunter:{deal_id}"

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

    prime_mode = (item.get("prime_mode") or "").strip().lower()
    if prime_mode == "shipping":
        notas.append("🅿️ Envío gratis con Amazon Prime")
    elif prime_mode == "exclusive":
        notas.append("🅿️ Oferta exclusiva para miembros Prime")
    else:
        notas.append("✈️🇨🇴 Envío gratis directo a Colombia")

    foto = f"https://elpromohunter.com/api/product-image/{deal_id}"

    return Deal(
        source="promohunter",
        store="Amazon",
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


def obtener_foto_limpia_amazon(asin: str, timeout: float = 2.5) -> str | None:
    """Extrae la imagen original en alta resolución de Amazon sin logos ni marcas de agua."""
    if not asin:
        return None
    url = f"https://www.amazon.com/dp/{asin}"
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "es-CO,es;q=0.9,en;q=0.8",
    }
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            html_text = resp.read().decode("utf-8", errors="ignore")
            m = re.search(r'data-old-hires="([^"]+)"', html_text)
            if m and m.group(1).startswith("http"):
                return m.group(1).replace("\\/", "/")
            m2 = re.search(r'"hiRes"\s*:\s*"([^"]+)"', html_text)
            if m2 and m2.group(1).startswith("http"):
                return m2.group(1).replace("\\/", "/")
            m3 = re.search(r'"large"\s*:\s*"([^"]+)"', html_text)
            if m3 and m3.group(1).startswith("http"):
                return m3.group(1).replace("\\/", "/")
    except Exception:
        pass
    return None


def fetch(
    paginas: int = 1,
    incluir: list[str] | None = None,
    excluir: list[str] | None = None,
    max_horas: float = 3.0,
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
            deal = parse_deal(item, max_horas=max_horas)
            if not deal:
                continue

            if filtros.descartado(deal.title, excluir):
                continue
            if not filtros.pertinente(deal.title, incluir):
                continue

            if deal.key in vistos:
                continue
            vistos.add(deal.key)

            # Intentar obtener la imagen original sin marcas de agua de Amazon
            m_asin = _ASIN_RE.search(deal.url)
            asin_val = m_asin.group(1).upper() if m_asin else (deal.key.replace("amazon:", "") if deal.key.startswith("amazon:") else "")
            if asin_val:
                foto_limpia = obtener_foto_limpia_amazon(asin_val)
                if foto_limpia:
                    deal.image = foto_limpia

            ofertas.append(deal)

    return ofertas
