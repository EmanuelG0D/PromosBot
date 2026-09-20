"""Scraper de ofertas de Descuentos Tech Colombia y Descuentos Home (Telegram).

Extrae ofertas de los canales públicos t.me/s/DescuentosTech y t.me/s/DescuentosHome.
Parsea títulos limpios, precios en USD/COP, cupones alfanuméricos, porcentajes de
descuento y fotos en alta resolución servidas por Telegram.
"""
from __future__ import annotations

import html as html_lib
import re

from core import filtros
from core import http
from core.models import Deal

CANALES_DEFAULT = ["DescuentosTech", "DescuentosHome"]
URL = "https://t.me/s/{canal}"


def extraer_deals_html(html_str: str, canal_nombre: str = "DescuentosTech", por_canal: int = 20) -> list[Deal]:
    """Parsea el HTML público de Telegram t.me/s/{canal} y lo convierte en Deals."""
    if not html_str:
        return []

    bloques = re.findall(r'data-post="([^"]+)"(.*?)(?=data-post="|\Z)', html_str, re.S)
    deals: list[Deal] = []

    for post_id, bloque in bloques:
        texto_m = re.search(r'js-message_text"[^>]*>(.*?)</div>', bloque, re.S)
        if not texto_m:
            continue
        raw_texto = texto_m.group(1)
        texto_plano = " ".join(html_lib.unescape(re.sub(r'<[^>]+>', ' ', raw_texto)).split())

        # 1. Enlace externo
        enlaces = re.findall(r'href="(https?://[^"]+)"', bloque)
        enlaces_externos = [l for l in enlaces if "t.me" not in l and not l.startswith("?")]
        if not enlaces_externos:
            continue
        url_oferta = html_lib.unescape(enlaces_externos[0])

        # 2. Precio y moneda
        m_usd = re.search(r'USD\s*\$?\s*([0-9]+(?:\.[0-9]{1,2})?)', texto_plano, re.IGNORECASE)
        m_cop = re.search(r'\$\s*([0-9]{1,3}(?:\.[0-9]{3})+)\s*COP', texto_plano, re.IGNORECASE)
        m_generico = re.search(r'\$\s*([0-9]+(?:\.[0-9]{1,2})?)', texto_plano)

        if m_usd:
            precio = float(m_usd.group(1))
            moneda = "USD"
        elif m_cop:
            precio = float(re.sub(r'[^\d]', '', m_cop.group(1)))
            moneda = "COP"
        elif m_generico:
            val = float(m_generico.group(1))
            moneda = "USD" if val < 1000 else "COP"
            precio = val
        else:
            continue

        if precio <= 0:
            continue

        # 3. Cupón
        cupon_m = re.search(r'C[oó]digo:\s*([A-Z0-9_-]{4,20})', texto_plano, re.IGNORECASE)
        cupones = [cupon_m.group(1)] if cupon_m else []

        # 4. Descuento porcentual anunciado
        desc_m = re.search(r'([0-9]{1,2})%\s*Descuento', texto_plano, re.IGNORECASE)
        porcentaje_anunciado = float(desc_m.group(1)) if desc_m else None

        # 5. Título limpio
        corte_final = re.search(r'(?:👉|Ver oferta|https?://|¿Viste|\?Viste)', texto_plano, re.IGNORECASE)
        texto_util = texto_plano[:corte_final.start()] if corte_final else texto_plano

        separadores = ["🏷️", "✅", "% Descuento", "% descuento", "Envío gratis", "Envio gratis"]
        idx_inicio = -1
        for sep in separadores:
            pos = texto_util.rfind(sep)
            if pos != -1:
                idx_inicio = max(idx_inicio, pos + len(sep))

        if idx_inicio != -1 and idx_inicio < len(texto_util):
            titulo = texto_util[idx_inicio:].strip(" -:·🚚🏷️✅")
        else:
            limpio = re.sub(r'#[A-Za-z0-9_]+\s*', '', texto_util)
            limpio = re.sub(r'(?:Por solo|Oferta flash|Oferta Prime|USD\s*\$?[0-9.]+)\s*', '', limpio, flags=re.IGNORECASE)
            titulo = limpio.strip(" -:·🚚🏷️✅")

        titulo = " ".join(titulo.split())
        if len(titulo) < 6:
            continue

        # 6. Fotografía servida por Telegram
        foto_m = re.search(r"background-image:\s*url\(['\"]?(https://[^)'\"]+)", bloque)
        foto = foto_m.group(1) if foto_m else None

        # 7. Notas y etiquetas informativas
        notas: list[str] = []
        es_gratis = "envío gratis" in texto_plano.lower() or "envio gratis" in texto_plano.lower()
        if es_gratis:
            notas.append("✈️🇨🇴 Envío gratis")
        if "prime" in texto_plano.lower():
            notas.append("🅿️ Amazon Prime")
        if cupones:
            notas.append(f"Cupón: {cupones[0]}")
        if porcentaje_anunciado:
            notas.append(f"-{int(porcentaje_anunciado)}% OFF")

        deal = Deal(
            source="descuentostech",
            store="DescuentosTech",
            country="US" if moneda == "USD" else "CO",
            key=f"descuentostech:{post_id}",
            title=titulo[:180],
            url=url_oferta,
            price=precio,
            currency=moneda,
            list_price=None,
            coupons=cupones,
            notes=notas,
            image=foto,
            free_shipping_co=es_gratis,
        )
        deals.append(deal)

    return deals[-por_canal:] if len(deals) > por_canal else deals


def fetch(
    canales: list[str] | None = None,
    por_canal: int = 20,
    incluir: list[str] | None = None,
    excluir: list[str] | None = None,
) -> list[Deal]:
    """Descarga las publicaciones recientes de los canales de Descuentos Tech."""
    canales_a_consultar = canales or CANALES_DEFAULT
    ofertas: list[Deal] = []
    vistos: set[str] = set()

    for canal in canales_a_consultar:
        try:
            html_content = http.get_text(URL.format(canal=canal))
        except Exception as exc:
            print(f"  [descuentostech] Error consultando canal {canal}: {exc}")
            continue

        candidatas = extraer_deals_html(html_content, canal_nombre=canal, por_canal=por_canal)
        for deal in candidatas:
            if deal.key in vistos:
                continue
            if filtros.descartado(deal.title, excluir):
                continue
            if not filtros.pertinente(deal.title, incluir):
                continue

            vistos.add(deal.key)
            ofertas.append(deal)

    return ofertas
