"""Retail colombiano montado sobre VTEX (Exito, Carulla, Olimpica).

Estas tiendas exponen su catalogo como JSON, sin necesidad de parsear HTML.
El parametro O=OrderByBestDiscountDESC devuelve el catalogo ya ordenado por
porcentaje de descuento, asi que las gangas llegan en las primeras posiciones.

Es la fuente mas lenta: una peticion por cada par (tienda, busqueda). Por eso
las consultas salen en paralelo, con pocos hilos para no atropellar la tienda.
"""
from __future__ import annotations

import re
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import quote

import config
from core import http
from core.models import Deal

TIENDAS = {
    "exito":    {"nombre": "Exito",    "api": "https://www.exito.com/io",    "web": "https://www.exito.com"},
    "carulla":  {"nombre": "Carulla",  "api": "https://www.carulla.com/io",  "web": "https://www.carulla.com"},
    "olimpica": {"nombre": "Olimpica", "api": "https://www.olimpica.com",    "web": "https://www.olimpica.com"},
    # Verificadas el 2026-09-08 contra el endpoint de catalogo: las cinco
    # responden JSON con Price y ListPrice. Metro no esta porque redirige al
    # mismo catalogo de Jumbo: seria pedir dos veces lo mismo.
    "jumbo":        {"nombre": "Jumbo",        "api": "https://www.jumbocolombia.com",   "web": "https://www.jumbocolombia.com"},
    "panamericana": {"nombre": "Panamericana", "api": "https://www.panamericana.com.co", "web": "https://www.panamericana.com.co"},
    "pepeganga":    {"nombre": "Pepe Ganga",   "api": "https://www.pepeganga.com",       "web": "https://www.pepeganga.com"},
    "arturocalle":  {"nombre": "Arturo Calle", "api": "https://www.arturocalle.com",     "web": "https://www.arturocalle.com"},
    "larebaja":     {"nombre": "La Rebaja",    "api": "https://www.larebajavirtual.com", "web": "https://www.larebajavirtual.com"},
}

RUTA = "/api/catalog_system/pub/products/search?ft={q}&O=OrderByBestDiscountDESC&_from=0&_to={hasta}"

# La consulta especial "rebajas" pide el catalogo entero ordenado por descuento,
# sin termino de busqueda. Sirve para las tiendas donde uno no busca un producto
# concreto sino "a ver que hay rebajado hoy", como la drogueria.
CATALOGO = "rebajas"
RUTA_CATALOGO = "/api/catalog_system/pub/products/search?O=OrderByBestDiscountDESC&_from=0&_to={hasta}"
HILOS = 6

# VTEX mete en clusterHighlights tanto promociones reales como basura interna
# ("reindex total", "cont-pequenos-4431"). Por eso se usa lista blanca: solo
# se muestra lo que reconocemos, y lo demas se ignora en silencio.
PROMOCIONES = (
    ("envio-gratis", "Envio gratis"),
    ("envio gratis", "Envio gratis"),
    ("enviogratis", "Envio gratis"),
    ("0interes", "0% interes"),
    ("0int_", "0% interes"),
    ("sin-interes", "0% interes"),
    ("sininteres", "0% interes"),
    ("dias_amarillos", "Dias Amarillos"),
    ("dias-amarillos", "Dias Amarillos"),
    ("por hora", "Vitrina por hora"),
    ("trasnoch", "Trasnochon"),
    ("madrug", "Madrugon"),
    ("relampago", "Oferta relampago"),
    ("flash", "Oferta flash"),
    ("black", "Black Friday"),
    ("cyber", "Cyber"),
    ("addi", "Financiacion con Addi"),
    ("sistecredito", "Financiacion con Sistecredito"),
)

BANCOS = ("davivienda", "occidente", "bbva", "tuya", "visa", "bogota",
          "falabella", "nequi", "colpatria", "scotiabank", "avvillas", "mastercard")


def _promociones(producto: dict) -> list[str]:
    """Traduce los clusterHighlights a etiquetas legibles."""
    etiquetas: list[str] = []
    for bruto in (producto.get("clusterHighlights") or {}).values():
        texto = str(bruto).lower()
        for patron, nombre in PROMOCIONES:
            if patron not in texto:
                continue
            etiqueta = nombre
            if nombre.startswith("0%"):
                banco = next((b for b in BANCOS if b in texto), None)
                if banco:
                    etiqueta = f"0% interes con {banco.title()}"
            if etiqueta not in etiquetas:
                etiquetas.append(etiqueta)
            break
    return etiquetas


def enlace_publico(tienda: dict, producto: dict) -> str:
    """Arma el enlace usando el dominio publico de la tienda.

    El campo "link" de la API apunta a dominios internos (tienda.exito.com,
    secure.carulla.com) cuyas paginas traen un redirect en JavaScript hacia su
    propia portada: el enlace se abre y rebota al inicio sin mostrar nada. El
    dominio publico si sirve la ficha del producto.
    """
    ruta = producto.get("linkText") or ""
    if ruta:
        return tienda["web"] + "/" + ruta + "/p"
    return producto.get("link") or ""


def _oferta(producto: dict):
    """Devuelve (vendedor, commertialOffer) del SKU disponible mas barato."""
    mejor = None
    for item in producto.get("items", []):
        for vendedor in item.get("sellers", []):
            oferta = vendedor.get("commertialOffer") or {}
            precio = oferta.get("Price")
            if not precio or not oferta.get("IsAvailable", True):
                continue
            if mejor is None or precio < mejor[1]["Price"]:
                mejor = (vendedor, oferta)
    return mejor


# Palabras que delatan un nombre interno de campana, no una promocion visible.
_INTERNOS = ("reindex", "cont-", "feed", "almacen", "excepto", "prime-", "biggy",
             "googleshopping", "aliado", "tabladeprecios", "bandering", "mkp")


def _legible(nombre: str) -> bool:
    """Filtra los nombres internos que VTEX mezcla con las promociones reales."""
    limpio = (nombre or "").strip()
    if not 3 <= len(limpio) <= 60:
        return False
    if " " not in limpio:            # los slugs internos no llevan espacios
        return False
    if re.search(r"_|\d{3,}", limpio):
        return False
    bajo = limpio.lower()
    return not any(marca in bajo for marca in _INTERNOS)


def _notas(oferta: dict) -> list[str]:
    """Teasers de VTEX: promociones bancarias o condicionadas."""
    notas: list[str] = []
    crudos = (oferta.get("Teasers") or []) + (oferta.get("PromotionTeasers") or [])
    crudos += oferta.get("DiscountHighLight") or []
    for teaser in crudos:
        nombre = teaser.get("Name") if isinstance(teaser, dict) else str(teaser)
        if nombre and _legible(nombre) and nombre not in notas:
            notas.append(nombre)
    return notas


def _consultar(clave: str, tienda: dict, consulta: str, hasta: int) -> list[Deal]:
    if consulta == CATALOGO:
        url = tienda["api"] + RUTA_CATALOGO.format(hasta=hasta)
    else:
        url = tienda["api"] + RUTA.format(q=quote(consulta), hasta=hasta)
    try:
        productos = http.get_json(url)
    except Exception as exc:
        print(f"  [vtex] {clave} / {consulta}: {exc}")
        return []
    if not isinstance(productos, list):
        return []

    ofertas: list[Deal] = []
    for producto in productos:
        elegido = _oferta(producto)
        if not elegido:
            continue
        vendedor, oferta = elegido

        precio = float(oferta["Price"])
        lista = float(oferta.get("ListPrice") or oferta.get("PriceWithoutDiscount") or precio)
        es_marketplace = str(vendedor.get("sellerId")) != "1"
        lista_confiable = lista <= precio * 10   # un 10x no es rebaja, es basura
        if es_marketplace and lista > 0:
            # Al vendedor externo se le cree hasta cierto punto: un 55% suele
            # ser real (mismo precio sugerido que en la tienda propia), un 95%
            # nunca lo es.
            declarado = (1 - precio / lista) * 100
            lista_confiable = (lista_confiable
                               and declarado <= config.MARKETPLACE_MAX_DISCOUNT_PCT
                               and precio >= config.MARKETPLACE_MIN_PRICE_COP)

        imagenes = (producto.get("items") or [{}])[0].get("images") or []
        foto = imagenes[0].get("imageUrl") if imagenes else None

        enlace = enlace_publico(tienda, producto)

        ofertas.append(Deal(
            source="vtex",
            store=tienda["nombre"],
            country="CO",
            key="vtex:" + clave + ":" + str(producto.get("productId")),
            title=producto.get("productName") or producto.get("productTitle") or "",
            url=enlace,
            price=precio,
            currency="COP",
            list_price=lista,
            notes=_notas(oferta) + _promociones(producto),
            expires_at=oferta.get("PriceValidUntil"),
            image=foto,
            seller=vendedor.get("sellerName"),
            marketplace=es_marketplace,
            in_stock=bool(oferta.get("AvailableQuantity", 0)),
            list_price_trusted=lista_confiable,
        ))
    return ofertas


def fetch(consultas: list[str], tiendas: list[str] | None = None, por_consulta: int = 24) -> list[Deal]:
    hasta = min(por_consulta, 49) - 1   # VTEX topa en 50 resultados por peticion

    tareas = []
    for clave in (tiendas or list(TIENDAS)):
        tienda = TIENDAS.get(clave)
        if not tienda:
            print(f"  [vtex] tienda desconocida: {clave}")
            continue
        tareas += [(clave, tienda, consulta) for consulta in consultas]

    if not tareas:
        return []

    ofertas: list[Deal] = []
    with ThreadPoolExecutor(max_workers=HILOS) as pool:
        for lote in pool.map(lambda t: _consultar(t[0], t[1], t[2], hasta), tareas):
            ofertas += lote
    return ofertas
