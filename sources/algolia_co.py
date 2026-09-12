"""Alkosto y K-tronix: no son VTEX, su buscador corre sobre Algolia.

La web publica embebe una llave de solo lectura para consultar el indice; es la
misma peticion que hace el navegador al usar el buscador del sitio. Ademas del
precio normal, el indice expone el precio exclusivo con tarjeta de la tienda,
que es justo el descuento que no se ve en el listado.
"""
from __future__ import annotations

import os
import re

from core import http
from core.models import Deal

APP_ID = os.environ.get("ALGOLIA_APP_ID", "QX5IPS1B1Q")
API_KEY = os.environ.get("ALGOLIA_API_KEY", "7a8800d62203ee3a9ff1cdf74f99b268")

TIENDAS = {
    "alkosto": {"nombre": "Alkosto", "indice": "alkostoIndexAlgoliaPRD", "web": "https://www.alkosto.com"},
    "ktronix": {"nombre": "K-tronix", "indice": "ktronixIndexAlgoliaPRD", "web": "https://www.ktronix.com"},
    "alkomprar": {"nombre": "Alkomprar", "indice": "alkomprarIndexAlgoliaPRD", "web": "https://www.alkomprar.com"},
}

URL = "https://{app}-dsn.algolia.net/1/indexes/*/queries"

# El campo subcat-mediospago mezcla beneficios reales con etiquetas internas
# ("BI_ELHO_ALKOS", "recien-casados", "temporadas"). Lista blanca: solo se
# muestra lo que reconocemos.
MEDIOS_PAGO = (
    ("davivienda-0-interes", "0% interes con Davivienda"),
    ("0-interes", "0% interes"),
    ("sin-interes", "0% interes"),
    ("credito-facil-codensa", "Credito Facil Codensa"),
    ("codensa", "Credito Facil Codensa"),
    ("addi", "Financiacion con Addi"),
    ("sistecredito", "Financiacion con Sistecredito"),
    ("envios-rapidos-express", "Envio rapido express"),
    ("ofertas-medios-pago", "Descuento con medios de pago"),
)


def _medios_pago(hit: dict) -> list[str]:
    """Beneficios de pago y envio que publica la tienda en su indice."""
    etiquetas: list[str] = []
    for bruto in hit.get("subcat-mediospago_string_mv") or []:
        texto = str(bruto).lower()
        for patron, nombre in MEDIOS_PAGO:
            if patron in texto:
                if nombre not in etiquetas:
                    etiquetas.append(nombre)
                break
    # La tarjeta solo muestra tres notas: primero lo concreto (que banco, que
    # financiacion) y de ultimo el aviso generico.
    generico = "Descuento con medios de pago"
    if generico in etiquetas:
        etiquetas.remove(generico)
        etiquetas.append(generico)
    return etiquetas
_SOLO_DIGITOS = re.compile(r"[^\d]")


def _precio_promo(bruto: str):
    """Convierte el campo tarjeta_x:$1.488.900:/medias/... en (etiqueta, valor)."""
    partes = bruto.split(":")
    if len(partes) < 2:
        return None
    etiqueta = partes[0].replace("_", " ").strip().title()
    digitos = _SOLO_DIGITOS.sub("", partes[1])
    if not digitos:
        return None
    return etiqueta, float(digitos)


def _formato_cop(valor: float) -> str:
    return "$" + format(valor, ",.0f").replace(",", ".")


def fetch(consultas: list[str], tiendas: list[str] | None = None, por_consulta: int = 60) -> list[Deal]:
    ofertas: list[Deal] = []
    cabeceras = {"X-Algolia-Application-Id": APP_ID, "X-Algolia-API-Key": API_KEY}

    for clave in (tiendas or list(TIENDAS)):
        tienda = TIENDAS.get(clave)
        if not tienda:
            print(f"  [algolia] tienda desconocida: {clave}")
            continue

        peticiones = [
            {"indexName": tienda["indice"], "params": f"query={c}&hitsPerPage={por_consulta}"}
            for c in consultas
        ]
        try:
            respuesta = http.post_json(
                URL.format(app=APP_ID), {"requests": peticiones}, headers=cabeceras
            )
        except Exception as exc:
            print(f"  [algolia] {clave}: {exc}")
            continue

        for resultado in respuesta.get("results", []):
            for hit in resultado.get("hits", []):
                precio = hit.get("discountprice_double") or hit.get("lowestprice_double")
                if not precio:
                    continue
                lista = float(hit.get("pricevalue_cop_double") or precio)

                notas: list[str] = []
                for promo in hit.get("paymentpromotion_text_mv") or []:
                    if not promo:
                        continue
                    parsed = _precio_promo(promo)
                    if parsed:
                        etiqueta, valor = parsed
                        notas.append(etiqueta + ": " + _formato_cop(valor))

                foto = (hit.get("img-750wx750h_string")
                        or hit.get("img-1400wx1400h_string")
                        or hit.get("img-310wx310h_string"))
                for etiqueta in _medios_pago(hit):
                    if etiqueta not in notas:
                        notas.append(etiqueta)

                ruta = hit.get("url_es_string") or ""
                enlace = tienda["web"] + ruta if ruta.startswith("/") else ruta
                identificador = hit.get("objectID") or hit.get("code_string")

                ofertas.append(Deal(
                    source="algolia_co",
                    store=tienda["nombre"],
                    country="CO",
                    key="algolia:" + clave + ":" + str(identificador),
                    title=hit.get("name_text_es") or "",
                    url=enlace,
                    price=float(precio),
                    currency="COP",
                    list_price=lista,
                    list_price_trusted=lista <= float(precio) * 10,
                    notes=notas,
                    image=foto,
                    seller=hit.get("marca_text"),
                    in_stock=hit.get("stocklevelstatus_string") != "outOfStock",
                ))
    return ofertas
