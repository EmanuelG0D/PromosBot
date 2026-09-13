"""Modelo unico de oferta compartido por todas las fuentes."""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field


@dataclass
class Deal:
    source: str                 # slickdeals | vtex | algolia_co | ebay
    store: str                  # nombre visible de la tienda
    country: str                # "US" o "CO"
    key: str                    # identificador estable para deduplicar
    title: str
    url: str
    price: float | None
    currency: str               # "USD" o "COP"
    list_price: float | None = None
    coupons: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)   # promos bancarias, teasers
    seller: str | None = None
    marketplace: bool = False    # vendedor externo: ListPrice poco confiable
    in_stock: bool = True
    weight_lb: float | None = None
    # Los vendedores de marketplace inflan el precio de lista para simular
    # descuentos. Cuando no es confiable, el porcentaje no se usa para decidir.
    list_price_trusted: bool = True
    # Fecha ISO en que vence el precio. Es la firma de una oferta relampago
    # o de un trasnochon: precio con hora de caducidad.
    expires_at: str | None = None
    # Foto del producto: convierte la alerta en una tarjeta con imagen.
    image: str | None = None
    # True si el envio internacional es directo y gratis a Colombia (sin casillero).
    free_shipping_co: bool = False

    @property
    def discount_pct(self) -> float:
        """Descuento nominal frente al precio de lista publicado."""
        if not self.price or not self.list_price:
            return 0.0
        if self.list_price <= self.price:
            return 0.0
        return round((1 - self.price / self.list_price) * 100, 1)

    @property
    def horas_restantes(self) -> float | None:
        """Horas hasta que venza el precio, o None si no tiene vencimiento."""
        if not self.expires_at:
            return None
        try:
            vence = dt.datetime.fromisoformat(self.expires_at.replace("Z", "+00:00"))
        except (TypeError, ValueError):
            return None
        if vence.tzinfo is None:
            vence = vence.replace(tzinfo=dt.timezone.utc)
        restantes = (vence - dt.datetime.now(dt.timezone.utc)).total_seconds() / 3600
        return round(restantes, 1) if restantes > 0 else 0.0

    @property
    def vence_pronto(self) -> bool:
        horas = self.horas_restantes
        return horas is not None and 0 < horas <= 48

    @property
    def discount_verificable(self) -> float:
        """Descuento utilizable para decidir: 0 si el precio de lista no es fiable."""
        return self.discount_pct if self.list_price_trusted else 0.0
