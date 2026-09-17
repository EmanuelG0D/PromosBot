"""Scraper de ofertas de tecnología y hardware de República de Descuentos (Telegram @Republicadescuentos).

Extrae las ofertas del canal público t.me/s/Republicadescuentos.
Filtra estrictamente cualquier tipo de anuncio, enlace de referidos o invitación a apps,
y resuelve enlaces canónicos de Amazon (ASIN), AliExpress y Mercado Libre.
"""
from __future__ import annotations

import html
import re
import urllib.request
from concurrent.futures import ThreadPoolExecutor

from core import filtros
from core import http
from core.models import Deal

CANAL_URL = "https://t.me/s/Republicadescuentos"
_TAG_PARAM_RE = re.compile(r"[?&]tag=[^&]+")
_ASIN_RE = re.compile(r"/(?:dp|product|gp/product)/([A-Z0-9]{10})", re.IGNORECASE)

TIENDAS_PERMITIDAS = {
    "amazon": ("Amazon", ["amazon.com", "amzn.to", "a.co"]),
    "aliexpress": ("AliExpress", ["aliexpress.com", "s.click.aliexpress.com"]),
    "mercadolibre": ("Mercado Libre", ["mercadolibre.com.co", "mercadolibre.com"]),
}

PALABRAS_SPAM = [
    "unete", "únete", "mi enlace", "gana dinero", "crea tu cuenta", 
    "bono de", "cashback", "invita a", "primeros 30 dias", "primeros 30 días",
    "morse.link", "app de", "tarjeta de credito", "tarjeta de crédito"
]


def identificar_tienda_y_enlace(enlaces: list[str]) -> tuple[str, str] | None:
    """Identifica la tienda asociada a la lista de enlaces externos del post."""
    for link in enlaces:
        link_lower = link.lower()
        for store_id, (nombre, dominios) in TIENDAS_PERMITIDAS.items():
            if any(dom in link_lower for dom in dominios):
                return store_id, link
    return None


def resolver_enlace_amazon(url: str, timeout: float = 2.5) -> str:
    """Sigue la redirección de enlaces acortados (amzn.to, a.co) para extraer el enlace canónico con ASIN."""
    if not url:
        return ""
    if not any(dom in url for dom in ("amzn.to", "a.co")):
        return _TAG_PARAM_RE.sub("", url).rstrip("?&")
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"}, method="HEAD")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            final_url = resp.geturl() or url
            m = _ASIN_RE.search(final_url)
            if m:
                return f"https://www.amazon.com/dp/{m.group(1).upper()}"
            return _TAG_PARAM_RE.sub("", final_url).rstrip("?&")
    except Exception:
        return url


def parse_monto_cop(texto: str | None) -> float | None:
    """Convierte cadenas de precio en pesos colombianos a float (ej: '68.000' -> 68000.0)."""
    if not texto:
        return None
    # Si viene un rango ej: '43.000 - 68.000', tomar el precio menor
    if "-" in texto:
        texto = texto.split("-")[0]
    digitos = re.sub(r"[^\d]", "", texto)
    try:
        return float(digitos) if digitos else None
    except ValueError:
        return None


def extraer_deals_html(html_str: str, por_canal: int = 20) -> list[Deal]:
    """Parsea el HTML público de Telegram t.me/s/Republicadescuentos descartando anuncios y referidos."""
    if not html_str:
        return []

    bloques = re.findall(r'data-post="([^"]+)"(.*?)(?=data-post="|\Z)', html_str, re.S)
    candidatos_brutos: list[dict] = []

    for post_id, bloque in bloques:
        texto_m = re.search(r'js-message_text"[^>]*>(.*?)</div>', bloque, re.S)
        if not texto_m:
            continue
        raw_texto = texto_m.group(1)

        # 1. Filtro estricto anti-anuncios y referidos
        texto_plano = " ".join(re.sub(r'<[^>]+>', ' ', raw_texto).split()).lower()
        if any(p in texto_plano for p in PALABRAS_SPAM):
            continue

        # 2. Enlaces externos válidos a tiendas autorizadas
        enlaces = re.findall(r'href="(https?://[^"]+)"', bloque)
        if not enlaces:
            enlaces = re.findall(r'(https?://[^\s<>"]+)', raw_texto)
        enlaces_filtrados = [l for l in enlaces if "t.me" not in l and not l.startswith("?")]
        tienda_info = identificar_tienda_y_enlace(enlaces_filtrados)
        if not tienda_info:
            continue

        tienda_id, url_compra = tienda_info
        nombre_tienda = TIENDAS_PERMITIDAS[tienda_id][0]

        # 3. Extracción de precio en COP
        precio_m = re.search(r'(?:⚠️|REGALADO|Barato|\$|COP)?\s*([0-9]{1,3}(?:\.[0-9]{3})+(?:\s*-\s*[0-9]{1,3}(?:\.[0-9]{3})+)?)\s*(?:Env[ií]o|Envio Gratis|CUP[OÓ]N|\n|<br)', raw_texto, re.IGNORECASE)
        if not precio_m:
            precio_m = re.search(r'\b([0-9]{1,3}(?:\.[0-9]{3})+)\b', raw_texto)

        precio = parse_monto_cop(precio_m.group(1)) if precio_m else None
        if not precio or precio < 10000:
            continue

        # 4. Extracción de cupón
        cupon_m = re.search(r'CUP[OÓ]N\s+([A-Z0-9_-]{4,20})', raw_texto, re.IGNORECASE)
        cupon = cupon_m.group(1).strip() if cupon_m else None

        # 5. Limpieza profunda del título
        limpio = re.sub(r'<[^>]+>', ' ', raw_texto)
        limpio = re.sub(r'#\w+', ' ', limpio)
        limpio = re.sub(r'(?i)\b(?:d[oó]lar\s+bajo\s+precios\s+bajos|regalado|barato|precio\s+(?:m[aá]s\s+)?bajo|varios\s+colores\s+y\s+tallas|super\s+precio)\b', ' ', limpio)
        limpio = re.sub(r'\b[0-9]{1,3}(?:\.[0-9]{3})+(?:\s*-\s*[0-9]{1,3}(?:\.[0-9]{3})+)?\b', ' ', limpio)
        limpio = re.sub(r'(?i)\b(?:env[ií]o\s+(?:gratis|incluido|prime|e\s+impuestos\s+incluidos)|envio\s+gratis|prime)\b', ' ', limpio)
        if cupon:
            limpio = re.sub(r'(?i)\bcup[oó]n\s+' + re.escape(cupon) + r'\b', ' ', limpio)
        limpio = re.sub(r'(?i)compra\s+aqu[ií].*$', ' ', limpio)
        limpio = re.sub(r'https?://\S+', ' ', limpio)
        titulo = " ".join(html.unescape(limpio).split())[:180].strip(" -:⚠️,|")

        if not titulo or len(titulo) < 6:
            continue

        # 6. Foto del producto
        foto_m = re.search(r"background-image:\s*url\(['\"]?(https://[^)'\"]+)", bloque)
        foto = foto_m.group(1) if foto_m else None

        candidatos_brutos.append({
            "post_id": post_id,
            "tienda_id": tienda_id,
            "tienda": nombre_tienda,
            "titulo": titulo,
            "precio": precio,
            "cupon": cupon,
            "enlace_crudo": url_compra,
            "foto": foto,
            "raw_texto": raw_texto,
        })

    candidatos = candidatos_brutos[-por_canal:] if len(candidatos_brutos) > por_canal else candidatos_brutos

    # Resolver enlaces acortados de Amazon en paralelo
    def _resolver(item: dict) -> str:
        if item["tienda_id"] == "amazon":
            return resolver_enlace_amazon(item["enlace_crudo"])
        return item["enlace_crudo"]

    with ThreadPoolExecutor(max_workers=5) as pool:
        urls_resueltas = list(pool.map(_resolver, candidatos))

    deals: list[Deal] = []
    for c, url_limpia in zip(candidatos, urls_resueltas):
        if c["tienda_id"] == "amazon":
            m_asin = _ASIN_RE.search(url_limpia)
            key = f"amazon:{m_asin.group(1).upper()}" if m_asin else f"republica:{c['post_id']}"
        elif c["tienda_id"] == "mercadolibre":
            m_meli = re.search(r'(MCO-?[0-9]+)', url_limpia, re.IGNORECASE)
            key = f"meli:{m_meli.group(1).replace('-', '')}" if m_meli else f"republica:{c['post_id']}"
        else:
            key = f"republica:{c['post_id']}"

        notas: list[str] = []
        if c["cupon"]:
            notas.append(f"🎟 Cupón: {c['cupon']}")
        if "prime" in c.get("raw_texto", "").lower():
            notas.append("🅿️ Envío gratis con Amazon Prime")
        elif "gratis" in c.get("raw_texto", "").lower():
            notas.append("✈️🇨🇴 Envío gratis")

        deal = Deal(
            source="republica",
            store=c["tienda"],
            country="CO",
            key=key,
            title=c["titulo"],
            url=url_limpia,
            price=c["precio"],
            currency="COP",
            list_price=None,
            notes=notas,
            coupons=[c["cupon"]] if c["cupon"] else [],
            image=c["foto"],
            free_shipping_co=True if notas else False,
        )
        deals.append(deal)

    return deals


def fetch(
    por_canal: int = 20,
    incluir: list[str] | None = None,
    excluir: list[str] | None = None,
) -> list[Deal]:
    """Descarga las publicaciones recientes de República de Descuentos y las convierte en Deals."""
    try:
        html_content = http.get_text(CANAL_URL)
    except Exception as exc:
        print(f"  [republica] Error al consultar canal: {exc}")
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
