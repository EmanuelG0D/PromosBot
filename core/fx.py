"""Tasa de cambio USD -> COP. Fuente oficial (TRM) con respaldos."""
from __future__ import annotations

import datetime as dt

import config
from core import http

TRM_OFICIAL = (
    "https://www.datos.gov.co/resource/32sa-8pi3.json"
    "?$limit=1&$order=vigenciadesde%20DESC"
)
TRM_RESPALDO = "https://open.er-api.com/v6/latest/USD"


def _desde_datos_gov() -> float:
    data = http.get_json(TRM_OFICIAL)
    return float(data[0]["valor"])


def _desde_er_api() -> float:
    data = http.get_json(TRM_RESPALDO)
    return float(data["rates"]["COP"])


def get_trm(store=None) -> tuple[float, str]:
    """Devuelve (valor, origen). Cachea un valor por dia en SQLite."""
    hoy = dt.date.today().isoformat()
    if store is not None:
        cached = store.get_meta(f"trm:{hoy}")
        if cached:
            valor, origen = cached.split("|", 1)
            return float(valor), origen

    for nombre, fn in (("TRM oficial", _desde_datos_gov), ("exchangerate-api", _desde_er_api)):
        try:
            valor = fn()
            if valor > 0:
                if store is not None:
                    store.set_meta(f"trm:{hoy}", f"{valor}|{nombre}")
                return valor, nombre
        except Exception:
            continue

    return config.TRM_FALLBACK, "valor de respaldo"
