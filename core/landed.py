"""Calculadora de costo puesto en Colombia para compras en EE. UU."""
from __future__ import annotations

from dataclasses import dataclass

import config


@dataclass
class Landed:
    fob_usd: float
    flete_usd: float
    iva_usd: float
    arancel_usd: float
    total_usd: float
    total_cop: float
    exento: bool
    trm: float

    @property
    def sobrecosto_pct(self) -> float:
        if self.fob_usd <= 0:
            return 0.0
        return round((self.total_usd / self.fob_usd - 1) * 100, 1)


def calcular(precio_usd: float, trm: float, peso_lb: float | None = None) -> Landed:
    """Estima el costo final en COP sumando flete de casillero e impuestos.

    Regla del TLC: un envio con valor FOB por debajo de USD 200 entra libre de
    IVA y de arancel. Desde USD 200.01 se liquidan ambos sobre FOB + flete.
    """
    peso = peso_lb or config.DEFAULT_WEIGHT_LB
    flete = max(peso * config.FREIGHT_USD_PER_LB, config.FREIGHT_MIN_USD)

    exento = precio_usd < config.DUTY_FREE_LIMIT_USD
    if exento:
        arancel = iva = 0.0
    else:
        base = precio_usd + flete
        arancel = base * config.TARIFF_PCT / 100
        iva = (base + arancel) * config.IVA_PCT / 100

    total_usd = precio_usd + flete + arancel + iva
    return Landed(
        fob_usd=precio_usd,
        flete_usd=round(flete, 2),
        iva_usd=round(iva, 2),
        arancel_usd=round(arancel, 2),
        total_usd=round(total_usd, 2),
        total_cop=round(total_usd * trm),
        exento=exento,
        trm=trm,
    )


def margen_para_exencion(precio_usd: float) -> float:
    """Cuanto falta para perder la exencion de USD 200 (negativo si ya se paso)."""
    return round(config.DUTY_FREE_LIMIT_USD - precio_usd, 2)
