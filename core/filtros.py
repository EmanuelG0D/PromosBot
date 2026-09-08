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
