"""Mercado Libre Colombia por su API oficial. FUENTE INUTILIZABLE HOY.

Probado el 2026-09-08 con credenciales reales de una app propia. El resultado
fue concluyente:

    users/me              -> 200  (el token es valido y autentica bien)
    sites/MCO             -> 403  PolicyAgent
    sites/MCO/categories  -> 403  PolicyAgent
    sites/MCO/search      -> 403  PolicyAgent

No es un problema de scopes ni de credenciales: hasta la metadata publica del
sitio esta bloqueada. Mercado Libre cerro el catalogo a las aplicaciones **no
certificadas**, y esa certificacion esta pensada para integradores comerciales.

La pagina web de ofertas si carga, pero sus datos vienen incrustados como
fragmentos de interfaz, sin contrato estable; raspar eso se romperia en
silencio, que es la peor falla posible en un bot desatendido.

El modulo se conserva funcionando por si algun dia la app queda certificada:
basta con volver a poner terminos en "queries" dentro de watchlist.json. Sin
credenciales, o ante el 403 de politica, la fuente se apaga sola.
"""
from __future__ import annotations

import os
import time
from urllib.parse import quote_plus

from core import http
from core.models import Deal

CLIENT_ID = os.environ.get("ML_CLIENT_ID", "").strip()
CLIENT_SECRET = os.environ.get("ML_CLIENT_SECRET", "").strip()
SITIO = os.environ.get("ML_SITE", "MCO").strip()      # MCO = Colombia

TOKEN_URL = "https://api.mercadolibre.com/oauth/token"
BUSCAR_URL = "https://api.mercadolibre.com/sites/{sitio}/search"

_token_cache: dict = {"valor": None, "expira": 0.0}


def disponible() -> bool:
    return bool(CLIENT_ID and CLIENT_SECRET)


def _token() -> str:
    if _token_cache["valor"] and time.time() < _token_cache["expira"]:
        return _token_cache["valor"]

    cuerpo = (f"grant_type=client_credentials&client_id={quote_plus(CLIENT_ID)}"
              f"&client_secret={quote_plus(CLIENT_SECRET)}")
    respuesta = http.post_form(TOKEN_URL, cuerpo, headers={"Accept": "application/json"})
    _token_cache["valor"] = respuesta["access_token"]
    _token_cache["expira"] = time.time() + int(respuesta.get("expires_in", 21600)) - 300
    return _token_cache["valor"]


def _a_oferta(item: dict) -> Deal | None:
    precio = item.get("price")
    if not precio:
        return None
    precio = float(precio)

    original = item.get("original_price")
    lista = float(original) if original else precio

    # En Mercado Libre todo lo publica un tercero, y el precio "antes" lo pone
    # el propio vendedor. Solo se cree cuando viene de una tienda oficial de
    # marca, igual que se hace con los vendedores externos de VTEX.
    tienda_oficial = bool(item.get("official_store_id"))
    lista_confiable = tienda_oficial and lista <= precio * 10

    notas: list[str] = []
    if (item.get("shipping") or {}).get("free_shipping"):
        notas.append("Envio gratis")
    if tienda_oficial:
        notas.append("Tienda oficial")
    if item.get("condition") == "used":
        notas.append("Producto usado")

    return Deal(
        source="mercadolibre",
        store="Mercado Libre",
        country="CO",
        key="ml:" + str(item.get("id")),
        title=item.get("title") or "",
        url=item.get("permalink") or "",
        price=precio,
        currency=item.get("currency_id") or "COP",
        list_price=lista,
        notes=notas,
        seller=str((item.get("seller") or {}).get("nickname") or ""),
        marketplace=not tienda_oficial,
        in_stock=bool(item.get("available_quantity", 1)),
        list_price_trusted=lista_confiable,
    )


def fetch(consultas: list[str], por_consulta: int = 50,
          solo_tienda_oficial: bool = False) -> list[Deal]:
    if not disponible():
        print("  [mercadolibre] sin credenciales: fuente omitida")
        return []

    try:
        cabeceras = {"Authorization": "Bearer " + _token()}
    except Exception as exc:
        print(f"  [mercadolibre] no se pudo autenticar: {exc}")
        return []

    ofertas: list[Deal] = []
    base = BUSCAR_URL.format(sitio=SITIO)

    for consulta in consultas:
        url = f"{base}?q={quote_plus(consulta)}&limit={min(por_consulta, 50)}"
        if solo_tienda_oficial:
            url += "&official_store=all"
        try:
            respuesta = http.get_json(url, headers=cabeceras)
        except Exception as exc:
            if "403" in str(exc):
                # Mercado Libre bloquea el catalogo para apps sin certificar.
                # No tiene sentido repetir la misma negativa por cada busqueda.
                print("  [mercadolibre] catalogo bloqueado para apps no certificadas "
                      "(403 PolicyAgent): fuente desactivada en esta ronda")
                return ofertas
            print(f"  [mercadolibre] {consulta}: {exc}")
            continue

        for item in respuesta.get("results", []) or []:
            try:
                deal = _a_oferta(item)
            except Exception:
                continue          # un item raro no puede tumbar la ronda
            if deal:
                ofertas.append(deal)

    return ofertas
