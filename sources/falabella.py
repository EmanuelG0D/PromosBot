"""Falabella y Homecenter, leyendo el JSON que sus paginas ya traen dentro.

Ninguna de las dos publica una API abierta: los endpoints que se suelen citar
devuelven 400 o 503. Pero las dos estan hechas en Next.js, y Next.js incrusta
el estado de la pagina como JSON dentro de un <script id="__NEXT_DATA__">. De
ahi sale el catalogo entero, ya estructurado.

No es un contrato como el de VTEX -es el estado interno de su interfaz- pero es
mucho mas estable que raspar clases CSS: cambia cuando cambia la logica, no
cada vez que rediseñan. Por eso el buscador del bloque es recursivo y no una
ruta fija: Falabella deja los productos en un sitio del arbol y Homecenter en
otro, y esa ruta si puede moverse sin aviso.

Son la misma familia (Homecenter es Sodimac, del grupo Falabella), asi que
comparten casi todo el formato. Lo que cambia esta anotado abajo.
"""
from __future__ import annotations

import json
import re
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import quote_plus

from core import filtros, http
from core.models import Deal

TIENDAS = {
    "falabella": {
        "nombre": "Falabella",
        "base": "https://www.falabella.com.co",
        "buscar": "/falabella-co/search?Ntt={q}",
    },
    "homecenter": {
        "nombre": "Homecenter",
        "base": "https://www.homecenter.com.co",
        "buscar": "/homecenter-co/search?Ntt={q}",
        # Homecenter no trae el enlace en el JSON; con el id basta, la tienda
        # redirige sola a la ficha completa.
        "producto": "/homecenter-co/product/{pid}/",
    },
}

HILOS = 4
_BLOQUE = re.compile(r'id="__NEXT_DATA__"[^>]*>(.*?)</script>', re.S)


def _productos(arbol, profundidad: int = 0):
    """Busca la lista de productos dentro del JSON, venga por donde venga.

    Falabella la deja colgando de "results" cerca de la raiz; Homecenter la
    esconde en searchProps.searchData.results. Fijar la ruta seria atarse a un
    detalle que ellos pueden mover; se busca por la forma del dato.
    """
    if profundidad > 9:
        return None
    if isinstance(arbol, dict):
        lista = arbol.get("results")
        if (isinstance(lista, list) and lista
                and isinstance(lista[0], dict) and "displayName" in lista[0]):
            return lista
        for valor in arbol.values():
            hallado = _productos(valor, profundidad + 1)
            if hallado:
                return hallado
    elif isinstance(arbol, list):
        for valor in arbol[:6]:
            hallado = _productos(valor, profundidad + 1)
            if hallado:
                return hallado
    return None


def _numero(entrada: dict) -> float | None:
    """El precio de una entrada, que cada tienda formatea distinto.

    Homecenter regala el numero limpio; Falabella solo el texto "3.199.900" y
    a veces dentro de una lista. Se quitan los puntos de miles: en Colombia los
    precios no llevan centavos.
    """
    crudo = entrada.get("priceWithoutFormatting")
    if isinstance(crudo, (int, float)):
        return float(crudo)
    valor = entrada.get("price")
    if isinstance(valor, list):
        valor = valor[0] if valor else None
    if not isinstance(valor, str):
        return None
    digitos = re.sub(r"[^\d]", "", valor)
    return float(digitos) if digitos else None


def _precios(entradas) -> tuple[float | None, float | None, float | None]:
    """(precio, precio de lista, precio con tarjeta).

    El precio de lista es el tachado (Falabella lo marca con "crossed") o el
    rotulado "NORMAL" (Homecenter). El precio con tarjeta CMR **no** se usa
    como precio: exige tener esa tarjeta, y presentarlo como el precio del
    producto seria inflar el descuento. Va como nota.
    """
    precio = lista = tarjeta = None
    for entrada in entradas or []:
        if not isinstance(entrada, dict):
            continue
        valor = _numero(entrada)
        if valor is None or valor <= 0:
            continue
        tipo = str(entrada.get("type") or "").lower()
        if "cmr" in tipo:
            tarjeta = valor if tarjeta is None else min(tarjeta, valor)
        elif entrada.get("crossed") or tipo == "normal" or tipo == "normalprice":
            lista = valor if lista is None else max(lista, valor)
        elif precio is None or valor < precio:
            precio = valor
    return precio, lista, tarjeta


def _foto(urls) -> str | None:
    """La primera imagen, pidiendola en JPEG.

    El CDN de Falabella sirve WebP por defecto, y el sendPhoto de Telegram no
    lo acepta: lo trata como sticker y responde "failed to get HTTP URL
    content". La oferta llegaba igual, pero sin foto. Con el parametro de
    formato devuelve JPEG y la tarjeta sale completa.
    """
    if not urls:
        return None
    url = str(urls[0])
    return url + ("&" if "?" in url else "?") + "format=jpg"


def _notas(producto: dict, tarjeta: float | None) -> list[str]:
    notas: list[str] = []
    if tarjeta:
        notas.append(f"Con tarjeta CMR: ${tarjeta:,.0f}".replace(",", "."))
    # El sello del banco viene como imagen, sin texto: se puede avisar que la
    # promocion existe, no de que banco es.
    if isinstance(producto.get("bankBadge"), dict):
        notas.append("Tiene promocion con tarjeta del banco")
    if producto.get("installmentsWithoutInterest"):
        notas.append("Cuotas sin interes")
    return notas


def _una_consulta(clave: str, tienda: dict, consulta: str,
                  por_consulta: int, exigir: bool = False) -> list[Deal]:
    url = tienda["base"] + tienda["buscar"].format(q=quote_plus(consulta))
    try:
        html = http.get_text(url)
        bloque = _BLOQUE.search(html)
        if not bloque:
            print(f"  [{clave}] {consulta}: la pagina no trae __NEXT_DATA__")
            return []
        crudos = _productos(json.loads(bloque.group(1)))
    except Exception as exc:
        print(f"  [{clave}] {consulta}: {exc}")
        return []
    if not crudos:
        return []

    ofertas: list[Deal] = []
    for producto in crudos[:por_consulta]:
        titulo = (producto.get("displayName") or "").strip()
        pid = producto.get("productId") or producto.get("skuId")
        if not titulo or not pid:
            continue

        precio, lista, tarjeta = _precios(producto.get("prices"))
        if precio is None:
            continue

        enlace = producto.get("url")
        if not enlace and tienda.get("producto"):
            enlace = tienda["base"] + tienda["producto"].format(pid=pid)

        fotos = producto.get("mediaUrls") or []
        marca = (producto.get("brand") or "").strip()

        ofertas.append(Deal(
            source="falabella",
            store=tienda["nombre"],
            country="CO",
            key=f"falabella:{clave}:{pid}",
            title=f"{marca} {titulo}".strip() if marca not in titulo else titulo,
            url=enlace or "",
            price=precio,
            currency="COP",
            list_price=lista or precio,
            notes=_notas(producto, tarjeta),
            image=_foto(fotos),
            seller=producto.get("sellerName"),
            # El buscador solo devuelve lo que se puede comprar; el campo
            # availability viene vacio y no sirve para decidir.
            in_stock=True,
        ))
    if exigir:
        ofertas = [d for d in ofertas if filtros.menciona(d.title, consulta)]
    return ofertas


def fetch(consultas: list[str], tiendas: list[str] | None = None,
          por_consulta: int = 30, marcas=None) -> list[Deal]:
    """Una peticion por cada par (tienda, busqueda), en paralelo.

    Las consultas listadas en `marcas` exigen que el titulo las nombre como
    palabra: una marca corta se cuela dentro de palabras corrientes.
    """
    exigidas = {m.lower() for m in (marcas or [])}
    tareas = []
    for clave in (tiendas or list(TIENDAS)):
        tienda = TIENDAS.get(clave)
        if not tienda:
            print(f"  [falabella] tienda desconocida: {clave}")
            continue
        tareas += [(clave, tienda, c, c.lower() in exigidas)
                   for c in consultas]

    ofertas: list[Deal] = []
    vistos: set[str] = set()
    with ThreadPoolExecutor(max_workers=HILOS) as pool:
        for lote in pool.map(
                lambda t: _una_consulta(t[0], t[1], t[2], por_consulta, t[3]), tareas):
            for deal in lote:
                if deal.key in vistos:
                    continue
                vistos.add(deal.key)
                ofertas.append(deal)
    return ofertas
