"""Koaj Colombia: scraper HTML sobre PrestaShop para capturar ofertas y outlets de ropa.

Koaj opera sobre PrestaShop sin API publica de catalogo. Las ofertas se
extraen tanto de sus secciones especializadas de Outlet (Hombre y Mujer)
como de busquedas por termino en su buscador web.
"""
from __future__ import annotations

import html
import re
import urllib.parse
from concurrent.futures import ThreadPoolExecutor

from core import http
from core.models import Deal

BASE_URL = "https://www.koaj.co"

RUTAS_OUTLET = [
    ("/hombre/ropa/outlet/", "Outlet Hombre"),
    ("/mujer/ropa/outlet/", "Outlet Mujer"),
]

# Expresiones regulares para extraer componentes de un <article class="... product-miniature ...">
_RE_ARTICULO = re.compile(
    r'<article[^>]*class="[^"]*product-miniature[^"]*"[^>]*>(.*?)</article>',
    re.S,
)
_RE_ID_PRODUCTO = re.compile(r'data-id-product="(\d+)"')
_RE_ID_DESDE_URL = re.compile(r"/(\d+)-[^/]+\.html")
_RE_TITULO_Y_LINK = re.compile(
    r'<h3[^>]*class="[^"]*s_title_block[^"]*"[^>]*>\s*<a[^>]*href="([^"]+)"[^>]*title="([^"]*)"',
    re.S,
)
_RE_TITULO_Y_LINK_FALLBACK = re.compile(
    r'<h3[^>]*class="[^"]*s_title_block[^"]*"[^>]*>\s*<a[^>]*href="([^"]+)"[^>]*>(.*?)</a>',
    re.S,
)
_RE_IMAGEN_FULL = re.compile(r'data-full-size-image-url="([^"]+)"')
_RE_IMAGEN_SRC = re.compile(r'<img[^>]+(?:data-src|src)="([^"]+)"')
_RE_PRECIO = re.compile(r'class="[^"]*price[^"]*"[^>]*>[^<]*?([0-9]{1,3}(?:\.[0-9]{3})+)')
_RE_PRECIO_REGULAR = re.compile(r'class="[^"]*regular-price[^"]*"[^>]*>[^<]*?([0-9]{1,3}(?:\.[0-9]{3})+)')
_RE_DESCUENTO = re.compile(r'class="[^"]*discount-percentage[^"]*"[^>]*>\s*(-?\d+%)')


def _limpiar_texto(texto: str) -> str:
    """Decodifica entidades HTML y normaliza espacios."""
    if not texto:
        return ""
    limpio = html.unescape(texto)
    limpio = re.sub(r"<[^>]+>", "", limpio)  # eliminar cualquier etiqueta html interna
    return " ".join(limpio.split())


def _extraer_precio(match: re.Match | None) -> float | None:
    """Convierte cadena de precio colombiana (ej. 89.900) a float."""
    if not match:
        return None
    numero_str = match.group(1).replace(".", "")
    try:
        return float(numero_str)
    except ValueError:
        return None


def parsear_articulos(contenido_html: str, etiqueta: str = "") -> list[Deal]:
    """Parsea el HTML de un listado de productos de Koaj y retorna lista de Deal."""
    articulos = _RE_ARTICULO.findall(contenido_html)
    ofertas: list[Deal] = []

    for bloque in articulos:
        # 1. Título y Enlace
        match_tit = _RE_TITULO_Y_LINK.search(bloque)
        if match_tit:
            enlace = match_tit.group(1)
            titulo = _limpiar_texto(match_tit.group(2))
        else:
            match_fb = _RE_TITULO_Y_LINK_FALLBACK.search(bloque)
            if not match_fb:
                continue
            enlace = match_fb.group(1)
            titulo = _limpiar_texto(match_fb.group(2))

        if not enlace or not titulo:
            continue

        if enlace.startswith("/"):
            enlace = BASE_URL + enlace

        # 2. Identificador único
        match_id = _RE_ID_PRODUCTO.search(bloque)
        if match_id:
            pid = match_id.group(1)
        else:
            match_url_id = _RE_ID_DESDE_URL.search(enlace)
            pid = match_url_id.group(1) if match_url_id else re.sub(r"\W+", "", enlace)[-20:]

        # 3. Precios
        precio_actual = _extraer_precio(_RE_PRECIO.search(bloque))
        if not precio_actual or precio_actual <= 0:
            continue

        precio_regular = _extraer_precio(_RE_PRECIO_REGULAR.search(bloque)) or precio_actual

        # 4. Imagen
        match_img = _RE_IMAGEN_FULL.search(bloque) or _RE_IMAGEN_SRC.search(bloque)
        imagen = match_img.group(1) if match_img else None
        if imagen and imagen.startswith("/"):
            imagen = BASE_URL + imagen

        # 5. Notas y etiquetas
        notas: list[str] = []
        if etiqueta:
            notas.append(etiqueta)
        match_desc = _RE_DESCUENTO.search(bloque)
        if match_desc:
            desc_str = match_desc.group(1)
            if not desc_str.startswith("-"):
                desc_str = f"-{desc_str}"
            notas.append(f"{desc_str} descuento")

        # 6. Disponibilidad (stock)
        bloque_min = bloque.lower()
        agotado = "agotado" in bloque_min or "out of stock" in bloque_min or "product-unavailable" in bloque_min

        ofertas.append(
            Deal(
                source="koaj",
                store="Koaj",
                country="CO",
                key=f"koaj:{pid}",
                title=titulo,
                url=enlace,
                price=precio_actual,
                currency="COP",
                list_price=precio_regular,
                list_price_trusted=True,
                marketplace=False,
                seller="Koaj",
                in_stock=not agotado,
                image=imagen,
                notes=notas,
            )
        )

    return ofertas


def _cargar_url(url: str, etiqueta: str = "") -> list[Deal]:
    """Carga una URL de Koaj de forma segura y devuelve sus ofertas."""
    try:
        texto = http.get_text(url, timeout=12)
        return parsear_articulos(texto, etiqueta=etiqueta)
    except Exception as exc:
        print(f"  [koaj] Error consultando {url}: {exc}")
        return []


def fetch(
    consultas: list[str] | None = None,
    incluir_outlet: bool = True,
    por_consulta: int = 24,
) -> list[Deal]:
    """Recolecta ofertas de Koaj desde outlets y busquedas por termino."""
    urls_a_consultar: list[tuple[str, str]] = []

    # 1. Secciones de Outlet
    if incluir_outlet:
        for ruta, etiqueta in RUTAS_OUTLET:
            urls_a_consultar.append((f"{BASE_URL}{ruta}", etiqueta))
            # Opcional: página 2 si se desea más profundidad
            if por_consulta > 24:
                urls_a_consultar.append((f"{BASE_URL}{ruta}?page=2", f"{etiqueta} Pág 2"))

    # 2. Búsquedas por término
    for consulta in consultas or []:
        termino = consulta.strip()
        if not termino:
            continue
        param = urllib.parse.quote_plus(termino)
        url_busqueda = f"{BASE_URL}/busqueda?controller=search&s={param}"
        urls_a_consultar.append((url_busqueda, f"Búsqueda: {termino}"))

    if not urls_a_consultar:
        return []

    ofertas: list[Deal] = []
    # Consultar con hilos controlados (máximo 4 simultáneos para no saturar)
    hilos = min(4, len(urls_a_consultar))
    with ThreadPoolExecutor(max_workers=hilos) as executor:
        futuros = [
            executor.submit(_cargar_url, url, etiqueta)
            for url, etiqueta in urls_a_consultar
        ]
        for f in futuros:
            ofertas.extend(f.result())

    # Deduplicar por clave única
    vistas: dict[str, Deal] = {}
    for d in ofertas:
        existente = vistas.get(d.key)
        if existente is None or (d.price or 0) < (existente.price or 0):
            vistas[d.key] = d

    return list(vistas.values())
