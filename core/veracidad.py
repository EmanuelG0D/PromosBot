"""Distingue una rebaja real de la maniobra de subir el precio y luego bajarlo.

El truco es viejo: dos semanas antes de la promocion el precio sube de $300.000
a $500.000, y el dia de la oferta lo "rebajan" a $320.000 anunciando -36%. La
tienda declara un descuento enorme frente a un precio de lista que nadie pago.

Contra eso solo sirve una cosa: **el precio mas bajo que el producto tuvo de
verdad en la ventana reciente**, medido por nosotros. Es el mismo criterio que
la directiva europea Omnibus le impone a las tiendas. Si la "oferta" no baja de
ese minimo, no es una oferta.

Limitacion honesta: esto solo funciona con historial acumulado. Los primeros
dias el veredicto sera "sin datos" y el bot dependera de sus otras senales.
"""
from __future__ import annotations

from dataclasses import dataclass
import datetime as dt

import config

SIN_DATOS = "sin_datos"
REAL = "real"
SOSPECHOSA = "sospechosa"

# Margen para no discutir por diferencias de centavos.
TOLERANCIA = 0.99


@dataclass
class Veracidad:
    veredicto: str = SIN_DATOS
    minimo_ventana: float | None = None
    descuento_real: float = 0.0     # % por debajo del minimo de la ventana
    alza_previa: float = 0.0        # % que subio el precio antes de la rebaja
    dias_observados: int = 0
    detalle: str = ""

    @property
    def es_falsa(self) -> bool:
        return self.veredicto == SOSPECHOSA

    @property
    def es_real(self) -> bool:
        return self.veredicto == REAL


def _dias_cubiertos(historial: list[tuple[str, float]]) -> int:
    try:
        inicio = dt.datetime.fromisoformat(historial[0][0])
        fin = dt.datetime.fromisoformat(historial[-1][0])
        return max((fin - inicio).days, 0)
    except (TypeError, ValueError, IndexError):
        return 0


def analizar(precio_actual: float | None,
             historial: list[tuple[str, float]]) -> Veracidad:
    """historial = [(fecha_iso, precio), ...] ordenado de viejo a nuevo."""
    if precio_actual is None or precio_actual <= 0 or not historial:
        return Veracidad()

    precios = [p for _ts, p in historial]
    dias = _dias_cubiertos(historial)

    # Sin suficiente historia cualquier veredicto seria inventado.
    if len(precios) < config.VERACITY_MIN_OBS or dias < config.VERACITY_MIN_DAYS:
        return Veracidad(
            dias_observados=dias,
            detalle=f"solo {len(precios)} observaciones en {dias} dias",
        )

    minimo = min(precios)
    maximo = max(precios)
    idx_min = precios.index(minimo)
    idx_max = len(precios) - 1 - precios[::-1].index(maximo)   # el maximo mas reciente

    # El precio subio despues de haber estado en su minimo: patron de inflado.
    alza = 0.0
    if idx_max > idx_min and minimo > 0 and maximo > minimo * (1 + config.HIKE_PCT / 100):
        alza = round((maximo / minimo - 1) * 100, 1)

    if precio_actual < minimo * TOLERANCIA:
        descuento = round((1 - precio_actual / minimo) * 100, 1)
        return Veracidad(
            veredicto=REAL,
            minimo_ventana=minimo,
            descuento_real=descuento,
            alza_previa=alza,
            dias_observados=dias,
            detalle=f"{descuento:g}% por debajo del minimo de {dias} dias",
        )

    if alza:
        detalle = (f"subieron el precio {alza:g}% antes de la rebaja; "
                   f"hoy no mejora el minimo de {dias} dias")
    else:
        detalle = f"no baja del minimo de {dias} dias"

    return Veracidad(
        veredicto=SOSPECHOSA,
        minimo_ventana=minimo,
        alza_previa=alza,
        dias_observados=dias,
        detalle=detalle,
    )
