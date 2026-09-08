"""Descarte de ofertas por palabras, para que no se cuele lo que no interesa.

Las busquedas por palabra clave siempre arrastran ruido: buscar "adidas" en un
foro de ofertas puede devolver un set de perfumes que menciona la marca. Este
filtro es la ultima linea: si el titulo contiene una palabra vetada, la oferta
no llega al canal.
"""
from __future__ import annotations

import re
import unicodedata

# Ruido recurrente en foros de ofertas de EE. UU. que no es tecnologia ni hogar.
VETADAS_POR_DEFECTO = (
    "fragrance", "perfume", "cologne", "eau de", "body spray", "deodorant",
    "gift card", "giftcard", "credit card", "insurance", "subscription",
    "vitamin", "supplement", "protein powder", "cbd", "vape",
    "mattress", "life insurance", "car rental", "hotel", "flight",
)

# Lo que acompaña al producto pero no es el producto: uno busca la aspiradora,
# no el empaque de la aspiradora. Vivia en objetivos.py y solo se aplicaba a los
# objetivos de precio; aqui la usan todas las fuentes.
ACCESORIOS = (
    "base", "soporte", "funda", "forro", "cubierta", "estuche", "canasta",
    "repuesto", "repuestos", "accesorio", "accesorios", "kit", "filtro",
    "molde", "moldes", "bandeja", "papel", "adaptador", "cargador", "cable",
    "correa", "empaque", "bolsa", "manual", "protector",
    # El mueble donde va el televisor no es un televisor. Las tiendas por
    # departamento los devuelven al buscar "televisor" y llenan la ronda.
    "rack", "panel", "centro de entretenimiento", "mesa para", "mueble para",
    "organizador", "porta",
)

# Categorias que no interesan aunque la tienda las tenga en oferta. Las tiendas
# colombianas venden de todo, y una busqueda de "licuadora" en un supermercado
# arrastra el vaso de repuesto, el florero y el tetero.
RUIDO_CO = (
    "tetero", "biberon", "chupo", "pañalera", "panalera", "florero",
    "portarretrato", "porta retrato", "adorno", "figura decorativa",
)

# Lo que se veta por defecto en las tiendas colombianas cuando la watchlist no
# trae su propia lista.
VETADAS_CO = ACCESORIOS + RUIDO_CO


def _normalizar(texto: str) -> str:
    plano = unicodedata.normalize("NFKD", (texto or "").lower())
    plano = "".join(c for c in plano if not unicodedata.combining(c))
    return re.sub(r"[^a-z0-9]+", " ", plano)


def pertinente(titulo: str, incluir: tuple | list | None = None) -> bool:
    """True si el titulo menciona algo de la lista de intereses.

    Con la lista vacia deja pasar todo. Sirve para quedarse solo con las marcas
    y categorias que importan cuando la fuente trae de todo, como el feed de
    portada de un foro de ofertas.
    """
    if not incluir:
        return True
    limpio = _normalizar(titulo)
    return any(_normalizar(p) in limpio for p in incluir)


def descartado(titulo: str, vetadas: tuple | list | None = None) -> bool:
    """True si el titulo contiene alguna palabra vetada."""
    palabras = VETADAS_POR_DEFECTO if vetadas is None else vetadas
    if not palabras:
        return False
    limpio = _normalizar(titulo)
    return any(_normalizar(p) in limpio for p in palabras)


def es_accesorio(titulo: str, palabras: tuple | list | None = None) -> bool:
    """True si el titulo ARRANCA con una palabra de accesorio.

    Buscarla en cualquier parte del titulo descartaba el producto que solo la
    menciona: "Aspiradora T-FAL X-FORCE, bateria 45 min, 4 accesorios" es una
    aspiradora, no un accesorio. En los catalogos el accesorio se nombra de
    primero -"Base TECHNOSOPORTES para televisores de 32 a 65"-, asi que esa
    es la señal que sirve.
    """
    palabras = ACCESORIOS if palabras is None else palabras
    if not palabras:
        return False
    limpio = _normalizar(titulo).strip()
    return any(limpio == _normalizar(p).strip()
               or limpio.startswith(_normalizar(p).strip() + " ")
               for p in palabras)
