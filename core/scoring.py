"""Decide que ofertas merecen alerta, con que confianza y con que urgencia.

Hay dos niveles de aviso:

* **Inmediato**: un mensaje propio, apenas se detecta. Se reserva para lo que no
  puede esperar (un objetivo de precio cumplido, un posible error de precio, una
  caida confirmada contra el historial propio, un descuentazo).
* **Resumen**: entra en una lista agrupada que sale cada cierto tiempo. Aqui cae
  la rebaja decente pero no espectacular, que igual interesa ver, y que enviada
  de a un mensaje convertiria el canal en spam.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import config
from core import objetivos as mod_objetivos
from core.models import Deal
from core.veracidad import Veracidad


@dataclass
class Verdict:
    alertar: bool
    motivo: str = ""
    etiquetas: list[str] = field(default_factory=list)
    confianza: str = "media"       # alta | media | baja
    glitch: bool = False
    inmediata: bool = False        # False = va al resumen agrupado


def evaluar(deal: Deal, stats: tuple[int, float | None, float | None],
            objetivo: dict | None = None,
            veracidad: Veracidad | None = None) -> Verdict:
    """stats = (observaciones previas, mediana, minimo) del historial propio."""
    if not deal.in_stock:
        return Verdict(False, "sin stock")
    if deal.price is None or deal.price <= 0:
        return Verdict(False, "sin precio legible")

    n_obs, mediana, minimo = stats
    # Solo cuenta el descuento respaldado por un precio de lista creible.
    descuento = deal.discount_verificable
    etiquetas: list[str] = []

    # Cuanto cayo frente a lo que nosotros mismos hemos visto antes.
    caida_historial = 0.0
    if mediana and n_obs >= config.MIN_OBSERVATIONS_FOR_HISTORY and mediana > deal.price:
        caida_historial = round((1 - deal.price / mediana) * 100, 1)

    # Confianza: el precio de lista de marketplace suele venir inflado.
    if caida_historial >= config.HISTORY_DROP_PCT:
        confianza = "alta"
    elif deal.marketplace and n_obs < config.MIN_OBSERVATIONS_FOR_HISTORY:
        confianza = "baja"
    else:
        confianza = "media"

    if not deal.list_price_trusted:
        etiquetas.append("precio de lista no verificable")
    if minimo and deal.price <= minimo:
        etiquetas.append("minimo historico registrado")
    if caida_historial:
        etiquetas.append(f"-{caida_historial:g}% vs. mediana propia")
    if deal.coupons:
        etiquetas.append("rebaja + cupon" if descuento else "con cupon")
    if deal.vence_pronto:
        etiquetas.insert(0, f"termina en {deal.horas_restantes:g} h")
    if deal.marketplace:
        etiquetas.append("vendedor marketplace")

    # El precio de lista lo pone la tienda; el minimo de la ventana lo medimos
    # nosotros. Cuando hay historial suficiente, manda el segundo.
    if veracidad is not None and veracidad.es_real:
        confianza = "alta"
        etiquetas.append(f"-{veracidad.descuento_real:g}% bajo el minimo de "
                         f"{veracidad.dias_observados} dias")
    elif veracidad is not None and veracidad.es_falsa:
        etiquetas.append(f"rebaja dudosa: {veracidad.detalle}")

    glitch = (descuento >= config.GLITCH_DISCOUNT_PCT
              or caida_historial >= config.GLITCH_DISCOUNT_PCT)
    if glitch and config.REQUIRE_HISTORY_FOR_GLITCH and confianza != "alta":
        glitch = False
    if glitch:
        etiquetas.insert(0, "posible error de precio")

    # Un objetivo de precio manda sobre cualquier porcentaje: es un precio que
    # el usuario declaro que quiere saber si o si.
    if objetivo is not None:
        etiquetas.insert(0, mod_objetivos.describir(objetivo, deal))
        return Verdict(True, "objetivo de precio alcanzado", etiquetas,
                       confianza, glitch, inmediata=True)

    # Una "rebaja" que no baja del minimo real de la ventana no es una oferta,
    # por mucho que la tienda anuncie un porcentaje enorme.
    if veracidad is not None and veracidad.es_falsa:
        if config.SUPPRESS_FAKE_DISCOUNTS:
            return Verdict(False, f"rebaja falsa: {veracidad.detalle}", etiquetas)
        return Verdict(True, f"rebaja dudosa: {veracidad.detalle}", etiquetas,
                       "baja", glitch, inmediata=False)

    # Un precio con hora de caducidad (trasnochon, relampago) no puede esperar
    # al resumen: para cuando salga, ya se vencio.
    urgente = (glitch
               or deal.vence_pronto
               or (veracidad is not None and veracidad.es_real)
               or confianza == "alta"
               or descuento >= config.INSTANT_DISCOUNT_PCT
               or (bool(deal.coupons) and descuento >= config.MIN_DISCOUNT_PCT))

    # Slickdeals ya viene filtrado por votos de la comunidad: entra aunque no
    # podamos calcular el porcentaje, pero solo interrumpe si trae cupon.
    if deal.source == "slickdeals":
        return Verdict(True, "destacada en Slickdeals", etiquetas, confianza,
                       glitch, inmediata=urgente or bool(deal.coupons))

    # Un precio con fecha de caducidad entra con un listón mas bajo: si espera
    # al umbral normal, para cuando alertemos ya se vencio.
    if deal.vence_pronto and descuento >= config.FLASH_MIN_DISCOUNT_PCT:
        return Verdict(True,
                       f"termina en {deal.horas_restantes:g} h (-{descuento:g}%)",
                       etiquetas, confianza, glitch, inmediata=True)

    # Bajar del minimo real de la ventana pesa mas que el porcentaje anunciado:
    # es una caida medida por nosotros, no declarada por la tienda. Vale incluso
    # cuando el descuento publicado es modesto.
    if (veracidad is not None and veracidad.es_real
            and veracidad.descuento_real >= config.VERACITY_MIN_DROP_PCT):
        return Verdict(True,
                       f"-{veracidad.descuento_real:g}% bajo el minimo de "
                       f"{veracidad.dias_observados} dias",
                       etiquetas, "alta", glitch, inmediata=True)

    if descuento >= config.MIN_DISCOUNT_PCT:
        return Verdict(True, f"-{descuento:g}% sobre precio de lista", etiquetas,
                       confianza, glitch, inmediata=urgente)
    if caida_historial >= config.HISTORY_DROP_PCT:
        return Verdict(True, f"-{caida_historial:g}% frente a su historial", etiquetas,
                       confianza, glitch, inmediata=True)
    if deal.coupons:
        return Verdict(True, "cupon detectado", etiquetas, confianza, glitch,
                       inmediata=False)

    if not deal.list_price_trusted:
        return Verdict(False, "sin referencia fiable; queda en observacion")
    return Verdict(False, f"descuento insuficiente ({descuento:g}%)")
