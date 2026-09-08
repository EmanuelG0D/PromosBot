"""PROMOCAJITA: la comunidad colombiana de ofertas, leida por su canal publico.

Es lo mas parecido a un Slickdeals colombiano que existe. Publica varias veces
al hora, con el precio y el codigo del cupon escritos en texto plano.

Se lee por `t.me/s/<canal>`, la vista previa que Telegram sirve como HTML
estatico. No hace falta cuenta, ni sesion, ni librerias de MTProto -que
obligarian a autenticarse como persona y a guardar un archivo con acceso total
a la cuenta personal-. Basta una peticion GET.

Su web no sirve: es una aplicacion de JavaScript y la pagina llega vacia. El
canal si, porque Telegram lo renderiza en el servidor.

Dos limitaciones que conviene tener presentes:

* **No traen precio de lista**, asi que no hay porcentaje de descuento que
  medir. Entran por la curaduria de quien las publica, igual que Slickdeals, no
  por el criterio del bot.
* **No dicen de que tienda son.** Sus enlaces pasan por la ficha en
  promocajita.com antes de llegar al comercio, asi que no se intenta adivinar:
  se presentan como lo que son, ofertas via PROMOCAJITA.
"""
from __future__ import annotations

import datetime as dt
import html
import re

from core import filtros
from core.coupons import extract_coupons
from core.models import Deal
from core import http

CANAL = "cajitatech"
URL = "https://t.me/s/{canal}"

# Cada publicacion es un bloque con su identificador, su hora, su foto y su
# texto. El identificador (canal/numero) es lo que da una clave estable.
_BLOQUE = re.compile(r'data-post="([^"]+)"(.*?)(?=data-post="|\Z)', re.S)
_TEXTO = re.compile(r'js-message_text"[^>]*>(.*?)</div>', re.S)
_FECHA = re.compile(r'datetime="([^"]+)"')
_FOTO = re.compile(r"background-image:\s*url\(['\"]?(https://[^)'\"]+)")
_ETIQUETAS = re.compile(r"<[^>]+>")

# "$679915 COP" o "$10 USD". El simbolo va pegado o separado.
_PRECIO = re.compile(r"\$\s*([\d.,]+)\s*(COP|USD)", re.IGNORECASE)
# La categoria viaja como etiqueta al inicio: "#Tablet", "#RopaInfantil".
# Dice cosas que el titulo no -"Redmi Pad 2" no menciona que es una tablet-,
# asi que cuenta para decidir si la oferta interesa.
_ETIQUETA = re.compile(r"#(\w+)")
# Donde termina el titulo y empieza la mecanica de la oferta.
_CORTE = re.compile(r"(🛍|Oferta\s*➡|Comparte|https?://)")


def _plano(fragmento: str) -> str:
    limpio = _ETIQUETAS.sub(" ", fragmento.replace("<br/>", " ").replace("<br>", " "))
    return " ".join(html.unescape(limpio).split())


def _monto(texto: str, moneda: str) -> float | None:
    """El numero, interpretado segun la moneda.

    En COP el punto separa miles y no hay centavos: "679.915" son seiscientos
    setenta y nueve mil. En USD es al reves, el punto son los centavos y la
    coma separa miles: "1,299.99" son mil doscientos noventa y nueve con
    noventa y nueve. Tratarlos igual multiplicaba el precio por cien.
    """
    limpio = texto.strip().replace(" ", "")
    if moneda == "USD":
        limpio = limpio.replace(",", "")
        return float(limpio) if re.fullmatch(r"\d+(\.\d{1,2})?", limpio) else None
    digitos = re.sub(r"[^\d]", "", limpio)
    return float(digitos) if digitos else None


def _una_publicacion(identificador: str, bloque: str,
                     incluir=None, vetadas=None) -> Deal | None:
    texto_html = _TEXTO.search(bloque)
    if not texto_html:
        return None
    texto = _plano(texto_html.group(1))

    precio = _PRECIO.search(texto)
    if not precio:
        return None                       # sin precio no hay oferta que evaluar
    moneda = precio.group(2).upper()
    monto = _monto(precio.group(1), moneda)
    if not monto or monto <= 0:
        return None

    # El titulo va entre el precio y la mecanica de la oferta.
    resto = texto[precio.end():].lstrip(" -–—")
    corte = _CORTE.search(resto)
    titulo = (resto[:corte.start()] if corte else resto).strip(" -·")
    if len(titulo) < 6:
        return None

    categorias = " ".join(_ETIQUETA.findall(texto[:precio.start()]))
    if (filtros.descartado(titulo, vetadas)
            or not filtros.pertinente(f"{titulo} {categorias}", incluir)):
        return None

    enlaces = re.findall(r'href="(https?://[^"]+)"', bloque)
    enlace = next((u for u in enlaces if "t.me" not in u), "")
    if not enlace:
        return None

    fecha = _FECHA.search(bloque)
    foto = _FOTO.search(bloque)

    return Deal(
        source="promocajita",
        store="PROMOCAJITA",
        country="CO" if moneda == "COP" else "US",
        key="promocajita:" + identificador,
        title=titulo[:200],
        url=enlace,
        price=monto,
        currency=moneda,
        # Nadie publica el precio anterior: sin lista no hay porcentaje, y
        # fingir uno seria inventarse el descuento.
        list_price=None,
        coupons=extract_coupons(texto),
        notes=[f"Categoria: {categorias}"] if categorias else [],
        image=foto.group(1) if foto else None,
        expires_at=fecha.group(1) if fecha else None,
    )


def fetch(canales: list[str] | None = None, por_canal: int = 20,
          incluir=None, vetadas=None) -> list[Deal]:
    """Las publicaciones recientes del canal, ya convertidas en ofertas.

    Telegram sirve las ultimas 20 por carga, que con rondas cada 15 minutos
    sobra: publican unas diez por hora.
    """
    ofertas: list[Deal] = []
    vistos: set[str] = set()

    for canal in (canales or [CANAL]):
        try:
            pagina = http.get_text(URL.format(canal=canal))
        except Exception as exc:
            print(f"  [promocajita] {canal}: {exc}")
            continue

        encontradas = 0
        for identificador, bloque in _BLOQUE.findall(pagina):
            if encontradas >= por_canal:
                break
            deal = _una_publicacion(identificador, bloque, incluir, vetadas)
            if not deal or deal.key in vistos:
                continue
            vistos.add(deal.key)
            ofertas.append(deal)
            encontradas += 1
    return ofertas
