"""eBay Browse API: outlets oficiales y reacondicionados certificados.

Fuente opcional. Necesita credenciales gratuitas de developer.ebay.com
(5.000 llamadas diarias sin costo). Sin credenciales el radar la omite.
"""
from __future__ import annotations

import base64
import time
from urllib.parse import quote_plus

import config
from core import http
from core.models import Deal

TOKEN_URL = "https://api.ebay.com/identity/v1/oauth2/token"
BUSCAR_URL = "https://api.ebay.com/buy/browse/v1/item_summary/search"

_token_cache: dict = {"valor": None, "expira": 0.0}


def disponible() -> bool:
    return bool(config.EBAY_CLIENT_ID and config.EBAY_CLIENT_SECRET)


def _token() -> str:
    if _token_cache["valor"] and time.time() < _token_cache["expira"]:
        return _token_cache["valor"]

    credencial = f"{config.EBAY_CLIENT_ID}:{config.EBAY_CLIENT_SECRET}".encode()
    respuesta = http.post_form(
        TOKEN_URL,
        "grant_type=client_credentials&scope=" + quote_plus("https://api.ebay.com/oauth/api_scope"),
        headers={"Authorization": "Basic " + base64.b64encode(credencial).decode()},
    )
    _token_cache["valor"] = respuesta["access_token"]
    _token_cache["expira"] = time.time() + int(respuesta.get("expires_in", 7200)) - 120
    return _token_cache["valor"]


def _filtro(vendedores: list[str], precio_max: float | None, condiciones: list[str]) -> str:
    partes = []
    if vendedores:
        partes.append("sellers:{" + "|".join(vendedores) + "}")
    if condiciones:
        partes.append("conditions:{" + "|".join(condiciones) + "}")
    if precio_max:
        partes.append(f"price:[..{precio_max}]")
        partes.append("priceCurrency:USD")
    return ",".join(partes)


def fetch(
    consultas: list[str],
    vendedores: list[str] | None = None,
    precio_max: float | None = None,
    condiciones: list[str] | None = None,
    por_consulta: int = 50,
) -> list[Deal]:
    if not disponible():
        print("  [ebay] sin credenciales: fuente omitida")
        return []

    try:
        cabeceras = {
            "Authorization": "Bearer " + _token(),
            "X-EBAY-C-MARKETPLACE-ID": "EBAY_US",
        }
    except Exception as exc:
        print(f"  [ebay] no se pudo autenticar: {exc}")
        return []

    filtro = _filtro(vendedores or [], precio_max, condiciones or ["NEW"])
    ofertas: list[Deal] = []

    for consulta in consultas:
        url = (
            f"{BUSCAR_URL}?q={quote_plus(consulta)}&limit={min(por_consulta, 200)}"
            f"&filter={quote_plus(filtro)}"
        )
        try:
            respuesta = http.get_json(url, headers=cabeceras)
        except Exception as exc:
            print(f"  [ebay] {consulta}: {exc}")
            continue

        for item in respuesta.get("itemSummaries", []) or []:
            precio = item.get("price", {}).get("value")
            if precio is None:
                continue
            precio = float(precio)

            mercadeo = item.get("marketingPrice") or {}
            original = (mercadeo.get("originalPrice") or {}).get("value")
            lista = float(original) if original else None

            vendedor = (item.get("seller") or {}).get("username")
            condicion = item.get("condition")
            notas = [f"Condicion: {condicion}"] if condicion else []
            if vendedor:
                notas.append(f"Vendedor: {vendedor}")

            ofertas.append(Deal(
                source="ebay",
                store="eBay",
                country="US",
                key="ebay:" + str(item.get("itemId")),
                title=item.get("title") or "",
                url=item.get("itemWebUrl") or "",
                price=precio,
                currency="USD",
                list_price=lista,
                notes=notas,
                image=(item.get("image") or {}).get("imageUrl"),
                seller=vendedor,
            ))
    return ofertas
