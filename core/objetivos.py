"""Objetivos de precio: avisar cuando algo cruza por debajo de un tope fijado.

El porcentaje de descuento no basta. Una freidora a $99.900 puede figurar como
"-45%" y pasar desapercibida entre cientos de rebajas mediocres, mientras que
ese precio absoluto es justamente la ganga que interesa. Un objetivo dice:
"avisame de cualquier freidora por debajo de $150.000", sin importar el
porcentaje que la tienda declare.
"""
from __future__ import annotations

import re
import unicodedata

from core import filtros
from core.models import Deal

# Palabras que delatan un accesorio y no el producto buscado. La lista vive en
# filtros.py porque ahora la comparten los objetivos y todas las fuentes.
ACCESORIOS_POR_DEFECTO = filtros.ACCESORIOS


def normalizar(texto: str) -> str:
    """Minusculas, sin tildes y sin puntuacion, para comparar titulos."""
    plano = unicodedata.normalize("NFKD", (texto or "").lower())
    plano = "".join(c for c in plano if not unicodedata.combining(c))
    return re.sub(r"[^a-z0-9]+", " ", plano).strip()


def terminos_de_busqueda(objetivos: list[dict]) -> list[str]:
    """Terminos que hay que buscar si o si para que los objetivos sirvan.

    Sin esto, fijar un objetivo para un producto que no esta en las busquedas
    de la watchlist no serviria de nada: nunca se consultaria a la tienda.
    """
    vistos: list[str] = []
    for objetivo in objetivos:
        termino = (objetivo.get("termino") or "").strip()
        if termino and termino not in vistos:
            vistos.append(termino)
    return vistos


def _es_accesorio(titulo: str, objetivo: dict) -> bool:
    excluir = objetivo.get("excluir")
    if excluir is not None:
        return any(normalizar(p) in titulo for p in excluir)
    return filtros.es_accesorio(titulo)


def alcanzado(deal: Deal, objetivos: list[dict]) -> dict | None:
    """Devuelve el objetivo que cumple esta oferta, o None."""
    if not objetivos or deal.price is None:
        return None

    titulo = normalizar(deal.title)
    for objetivo in objetivos:
        termino = normalizar(objetivo.get("termino", ""))
        if not termino or termino not in titulo:
            continue
        if _es_accesorio(titulo, objetivo):
            continue

        tope = objetivo.get("max_cop") if deal.currency == "COP" else objetivo.get("max_usd")
        if tope and deal.price <= float(tope):
            return objetivo
    return None


def describir(objetivo: dict, deal: Deal) -> str:
    tope = objetivo.get("max_cop") if deal.currency == "COP" else objetivo.get("max_usd")
    if deal.currency == "COP":
        formato = "$" + format(float(tope), ",.0f").replace(",", ".")
    else:
        formato = f"US${float(tope):,.2f}"
    return f"objetivo cumplido: {objetivo.get('termino')} bajo {formato}"
