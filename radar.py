#!/usr/bin/env python3
"""Radar de ofertas: recolecta, filtra y avisa por Telegram.

Uso tipico:
    python radar.py --dry-run          # ver que encontraria, sin enviar nada
    python radar.py                    # ronda completa con alertas
    python radar.py --source vtex      # una sola fuente
"""
from __future__ import annotations

import argparse
import copy
import datetime as dt
import re
import sys
import time
import unicodedata

import config
from core import filtros, fx, telegram, whitelist
from core import comandos as mod_comandos
from core import objetivos as mod_objetivos
from core import veracidad as mod_veracidad
from core.landed import calcular
from core.models import Deal
from core.scoring import SIN_PRECIO_DE_LISTA, Verdict, evaluar
from core.store import Store
from sources import algolia_co, falabella, promocajita, ebay, mercadolibre, slickdeals, vtex

FUENTES = ("slickdeals", "promocajita", "vtex", "algolia_co", "falabella",
           "droguerias", "mercadolibre", "ebay")

# Los colores no distinguen productos: solo variantes del mismo modelo.
COLORES = {
    "negro", "negra", "blanco", "blanca", "azul", "rojo", "roja", "gris",
    "verde", "rosado", "rosa", "plateado", "plata", "dorado", "oro", "morado",
    "amarillo", "beige", "cafe", "marron", "naranja", "violeta", "turquesa",
    "black", "white", "blue", "red", "gray", "grey", "green", "pink",
    "silver", "gold", "purple", "yellow", "orange", "brown",
}


def recolectar(watchlist: dict, activas: list[str]) -> list[Deal]:
    ofertas: list[Deal] = []

    if "slickdeals" in activas:
        cfg = watchlist.get("slickdeals", {})
        consultas = cfg.get("queries", [])
        if consultas:
            print(f"-> Slickdeals: {len(consultas)} busquedas")
            ofertas += _sin_ruido(
                slickdeals.fetch(consultas, cfg.get("por_consulta", 12),
                                 cfg.get("excluir"), cfg.get("incluir")), cfg)

    if "vtex" in activas:
        cfg = watchlist.get("vtex", {})
        consultas = cfg.get("queries", [])
        if consultas:
            print(f"-> VTEX ({', '.join(cfg.get('tiendas', vtex.TIENDAS))}): {len(consultas)} busquedas")
            ofertas += _sin_ruido(
                vtex.fetch(consultas, cfg.get("tiendas"), cfg.get("por_consulta", 24),
                           cfg.get("marcas")), cfg)

    if "algolia_co" in activas:
        cfg = watchlist.get("algolia_co", {})
        consultas = cfg.get("queries", [])
        if consultas:
            print(f"-> Alkosto/K-tronix: {len(consultas)} busquedas")
            ofertas += _sin_ruido(
                algolia_co.fetch(consultas, cfg.get("tiendas"),
                                 cfg.get("por_consulta", 60)), cfg)

    if "promocajita" in activas:
        cfg = watchlist.get("promocajita", {})
        if cfg.get("canales"):
            print("-> PROMOCAJITA: canal de Telegram")
            ofertas += promocajita.fetch(cfg.get("canales"),
                                         cfg.get("por_canal", 20),
                                         cfg.get("incluir"), cfg.get("excluir"))

    if "falabella" in activas:
        cfg = watchlist.get("falabella", {})
        consultas = cfg.get("queries", [])
        if consultas:
            print(f"-> Falabella/Homecenter: {len(consultas)} busquedas")
            ofertas += _sin_ruido(
                falabella.fetch(consultas, cfg.get("tiendas"),
                                cfg.get("por_consulta", 30),
                                cfg.get("marcas")), cfg)

    # La drogueria va aparte de las demas VTEX: no se le buscan terminos sino el
    # catalogo entero, y necesita su propio veto (en una tienda de tecnologia
    # "Base" es un soporte de TV; en una drogueria es maquillaje).
    if "droguerias" in activas:
        cfg = watchlist.get("droguerias", {})
        consultas = cfg.get("queries", [])
        if consultas:
            print(f"-> Droguerias ({', '.join(cfg.get('tiendas', []))}): {len(consultas)} busquedas")
            ofertas += _sin_ruido(
                vtex.fetch(consultas, cfg.get("tiendas"),
                           cfg.get("por_consulta", 40)), cfg)

    if "mercadolibre" in activas:
        cfg = watchlist.get("mercadolibre", {})
        consultas = cfg.get("queries", [])
        if consultas:
            print(f"-> Mercado Libre: {len(consultas)} busquedas")
            ofertas += _sin_ruido(
                mercadolibre.fetch(consultas, por_consulta=cfg.get("por_consulta", 48)), cfg)
        else:
            print("-> Mercado Libre: ofertas destacadas")
            ofertas += _sin_ruido(
                mercadolibre.fetch(por_consulta=cfg.get("por_consulta", 48)), cfg)

    if "ebay" in activas:
        cfg = watchlist.get("ebay", {})
        consultas = cfg.get("queries", [])
        if consultas:
            print(f"-> eBay: {len(consultas)} busquedas")
            ofertas += ebay.fetch(
                consultas,
                cfg.get("vendedores"),
                cfg.get("precio_max"),
                cfg.get("condiciones"),
                cfg.get("por_consulta", 50),
            )

    return ofertas


def _sin_repetidas(ofertas: list[Deal]) -> list[Deal]:
    """Un producto puede caer en varias busquedas de la misma ronda."""
    vistas: dict[str, Deal] = {}
    for deal in ofertas:
        previa = vistas.get(deal.key)
        if previa is None or (deal.price or 0) < (previa.price or 0):
            vistas[deal.key] = deal
    return list(vistas.values())


# Palabras que aparecen en medio catalogo y no distinguen un producto de otro.
GENERICAS = {
    "tv", "televisor", "smart", "pulgadas", "pulgada", "cm", "led", "uhd",
    "qled", "4k", "4kuhd", "hd", "fhd", "google", "con", "de", "para", "y",
    "el", "la", "los", "las", "un", "una", "por", "en", "negro", "blanco",
    "nuevo", "original", "pulg",
}


def _senas(titulo: str) -> set:
    """Palabras que si identifican el producto: marca, modelo, medidas."""
    limpio = unicodedata.normalize("NFKD", titulo.lower())
    limpio = "".join(c for c in limpio if not unicodedata.combining(c))
    return {p for p in re.findall(r"[a-z0-9]+", limpio)
            if len(p) >= 2 and p not in GENERICAS and p not in COLORES}


def _marketplace_solo_si_mejora(ofertas: list[Deal]) -> list[Deal]:
    """Un revendedor solo interesa si le gana el precio a la tienda.

    El mismo televisor aparece en la tienda propia y en el marketplace, muchas
    veces al mismo precio exacto. Verlo dos veces no aporta nada: si el precio
    no es mejor, se prefiere el original.
    """
    propias = [(d.price, _senas(d.title)) for d in ofertas
               if not d.marketplace and d.price]
    if not propias:
        return ofertas

    salida: list[Deal] = []
    for deal in ofertas:
        if deal.marketplace and deal.price:
            redundante = any(
                precio <= deal.price and len(senas & _senas(deal.title)) >= 2
                for precio, senas in propias
            )
            if redundante:
                continue
        salida.append(deal)
    return salida


def _colapsar_variantes(candidatas):
    """Una sola alerta por producto, aunque cada tienda lo escriba distinto.

    Comparar el titulo palabra por palabra no sirve: el mismo televisor es
    "TV KALLEY 50 Pulgadas 126 cm 50G315" en una tienda y "Televisor Kalley
    50G315a 50 Pulgadas" en otra. Lo que si coincide es el precio exacto y un
    par de senas propias (marca, modelo), asi que se agrupa por eso.

    Devuelve (unicas, hermanas). Las hermanas hay que marcarlas como avisadas
    junto con su representante, o el mismo producto reaparece como novedad.
    """
    por_precio: dict = {}
    hermanas: dict = {}
    unicas = []

    for par in candidatas:
        deal = par[0]
        senas = _senas(deal.title)
        grupo = por_precio.setdefault(round(deal.price or 0), [])

        representante = None
        for senas_previas, deal_previo in grupo:
            if len(senas & senas_previas) >= 2:
                representante = deal_previo
                break

        if representante is not None:
            otra = f"Tambien en {deal.store}"
            if deal.store != representante.store and otra not in representante.notes:
                representante.notes.append(otra)
            hermanas[representante.key].append(deal)
            continue

        grupo.append((senas, deal))
        hermanas[deal.key] = []
        unicas.append(par)

    return unicas, hermanas


def _marcar_avisada(store, deal: Deal, hermanas: dict) -> None:
    """Marca la oferta y todas sus variantes colapsadas."""
    store.mark_alerted(deal)
    for hermana in hermanas.get(deal.key, []):
        store.mark_alerted(hermana)


def _orden(par: tuple[Deal, Verdict]) -> tuple:
    """Primero lo urgente y verificado; ante un empate, la tienda propia."""
    deal, verdict = par
    return (not verdict.glitch,
            verdict.confianza != "alta",
            deal.marketplace,
            -deal.discount_verificable)


def _con_objetivos(watchlist: dict, objetivos: list[dict]) -> dict:
    """Agrega los terminos de los objetivos a las busquedas de cada fuente.

    Fijar un objetivo sobre un producto que nadie esta buscando no serviria de
    nada: la tienda jamas se consultaria por ese termino.
    """
    if not objetivos:
        return watchlist
    en_pesos = [o for o in objetivos if o.get("max_cop")]
    en_dolares = [o for o in objetivos if o.get("max_usd")]

    copia = copy.deepcopy(watchlist)
    # Mercado Libre queda fuera: su catalogo responde 403 a las apps no
    # certificadas, asi que inyectarle terminos solo gastaria peticiones.
    for fuente, lista in (("vtex", en_pesos), ("algolia_co", en_pesos),
                          ("slickdeals", en_dolares), ("ebay", en_dolares)):
        if not lista:
            continue
        cfg = copia.setdefault(fuente, {})
        consultas = cfg.setdefault("queries", [])
        for termino in mod_objetivos.terminos_de_busqueda(lista):
            if termino not in consultas:
                consultas.append(termino)
    return copia


def _toca_resumen(store) -> bool:
    """El resumen agrupado sale cada tantas horas, no en cada ronda."""
    ultimo = store.get_meta("ultimo_resumen")
    if not ultimo:
        return True
    try:
        pasado = dt.datetime.now(dt.timezone.utc) - dt.datetime.fromisoformat(ultimo)
    except (TypeError, ValueError):
        return True
    return pasado >= dt.timedelta(hours=config.DIGEST_EVERY_HOURS)


def _sin_ruido(ofertas: list[Deal], cfg: dict) -> list[Deal]:
    """Quita accesorios y categorias que no vienen al caso.

    Buscar "televisor" en una tienda grande devuelve tambien la base de pared,
    el cable y el control de repuesto; buscar "licuadora" en un supermercado
    arrastra el vaso suelto. La lista sale de la watchlist si la fuente trae
    "excluir"; si no, se usa el veto por defecto de las tiendas colombianas.
    """
    vetadas = cfg.get("excluir")
    if vetadas is not None:
        return [d for d in ofertas if not filtros.descartado(d.title, vetadas)]
    return [d for d in ofertas
            if not filtros.descartado(d.title, filtros.RUIDO_CO)
            and not filtros.es_accesorio(d.title)]


# --- Cache en memoria RAM para busquedas de tiendas (TTL 35 min, max 60 entradas) ---
_CACHE_OFERTAS: dict[tuple, tuple[float, list[Deal]]] = {}
CACHE_TTL_SEGUNDOS: float = 35.0 * 60.0  # 35 minutos
MAX_CACHE_ENTRIES: int = 60


def limpiar_cache() -> None:
    """Vacia la cache en memoria de busquedas."""
    _CACHE_OFERTAS.clear()


def _clave_cache(fuente: str, tiendas, consultas_custom: list[str] | None) -> tuple:
    tiendas_tupla = tuple(sorted(tiendas)) if isinstance(tiendas, (list, set, tuple)) else (str(tiendas) if tiendas else "")
    consultas_tupla = tuple(sorted(consultas_custom)) if consultas_custom else ()
    return (fuente, tiendas_tupla, consultas_tupla)


def _obtener_de_cache(clave: tuple) -> list[Deal] | None:
    if clave in _CACHE_OFERTAS:
        guardado_en, ofertas = _CACHE_OFERTAS[clave]
        if (time.time() - guardado_en) <= CACHE_TTL_SEGUNDOS:
            return list(ofertas)
        else:
            del _CACHE_OFERTAS[clave]
    return None


def _guardar_en_cache(clave: tuple, ofertas: list[Deal]) -> None:
    ahora = time.time()
    if len(_CACHE_OFERTAS) >= MAX_CACHE_ENTRIES:
        # Purgar expirados primero
        expirados = [k for k, (t, _) in _CACHE_OFERTAS.items() if (ahora - t) > CACHE_TTL_SEGUNDOS]
        for k in expirados:
            del _CACHE_OFERTAS[k]
        # Si aun supera el limite, sacar la entrada mas vieja (LRU / FIFO)
        if len(_CACHE_OFERTAS) >= MAX_CACHE_ENTRIES:
            mas_vieja = min(_CACHE_OFERTAS.keys(), key=lambda k: _CACHE_OFERTAS[k][0])
            del _CACHE_OFERTAS[mas_vieja]
    _CACHE_OFERTAS[clave] = (ahora, list(ofertas))


def _ofertas_de(watchlist: dict, fuente: str, tiendas,
                consultas_custom: list[str] | None = None) -> list[Deal]:
    """Trae las ofertas de una sola fuente, opcionalmente de tiendas concretas."""
    clave = _clave_cache(fuente, tiendas, consultas_custom)
    en_cache = _obtener_de_cache(clave)
    if en_cache is not None:
        return en_cache

    cfg = watchlist.get(fuente, {})

    # PROMOCAJITA se configura con "canales", no con "queries": no se le buscan
    # terminos sino que se lee un canal entero. Va antes del guardia de abajo,
    # que si no la descartaba sin llegar nunca a su rama.
    if fuente == "promocajita":
        if not cfg.get("canales"):
            return []
        if consultas_custom is not None:
            incluir = list(consultas_custom)
            por_canal = max(cfg.get("por_canal", 20), 80)
        else:
            incluir = cfg.get("incluir")
            por_canal = cfg.get("por_canal", 20)
        resultado = promocajita.fetch(cfg["canales"], por_canal,
                                      incluir, cfg.get("excluir"))
        _guardar_en_cache(clave, resultado)
        return resultado

    if fuente == "mercadolibre":
        consultas = consultas_custom if consultas_custom is not None else cfg.get("queries")
        crudas = mercadolibre.fetch(consultas, por_consulta=cfg.get("por_consulta", 48))
        resultado = _sin_ruido(crudas, cfg)
        _guardar_en_cache(clave, resultado)
        return resultado

    consultas = consultas_custom if consultas_custom is not None else cfg.get("queries", [])
    if not consultas:
        return []
    if fuente == "algolia_co":
        crudas = algolia_co.fetch(consultas, tiendas, cfg.get("por_consulta", 60))
    elif fuente == "vtex":
        crudas = vtex.fetch(consultas, tiendas or cfg.get("tiendas"), cfg.get("por_consulta", 24),
                            cfg.get("marcas"))
    elif fuente == "falabella":
        crudas = falabella.fetch(consultas, tiendas or cfg.get("tiendas"),
                                 cfg.get("por_consulta", 30), cfg.get("marcas"))
    elif fuente == "droguerias":
        crudas = vtex.fetch(consultas, cfg.get("tiendas"), cfg.get("por_consulta", 40))
    elif fuente == "slickdeals":
        crudas = slickdeals.fetch(consultas, cfg.get("por_consulta", 12),
                                  cfg.get("excluir"), cfg.get("incluir"))
    else:
        return []
    resultado = _sin_ruido(crudas, cfg)
    _guardar_en_cache(clave, resultado)
    return resultado


# Topes de precio especificos calibrados para que solo pasen verdaderas promociones accesibles
TOPES_CATEGORIA_COP: list[tuple[tuple[str, ...], float]] = [
    # 1. Monitores (antes de tv para evitar colisiones con 'monitor tv')
    (("monitor", "monitores", "monitor gamer"), 800000.0),

    # 2. Televisores y Smart TV (acordado $2.2M para incluir 50" y 55" en descuento)
    (("televisor", "televisores", "smart tv", "tv oled", "tv qled", "tv led", "tv 4k", "tv"), 2200000.0),

    # 3. Portátiles y computadores
    (("portatil", "portatiles", "laptop", "laptops", "computador", "computadores", "macbook", "notebook"), 2500000.0),

    # 4. Celulares
    (("celular", "celulares", "smartphone", "smartphones", "iphone", "samsung galaxy", "telefono"), 1300000.0),

    # 5. Neveras
    (("nevera", "neveras", "refrigerador", "refrigeradores", "nevecon", "nevecones", "refrigeradora", "freezer"), 2200000.0),

    # 6. Lavadoras y secadoras
    (("lavadora", "lavadoras", "secadora", "secadoras", "torre de lavado", "lavaseca"), 1800000.0),

    # 7. Estufas y hornos
    (("estufa", "estufas", "cubierta a gas", "horno", "hornos"), 900000.0),

    # 8. Aires acondicionados
    (("aire acondicionado", "aires acondicionados", "climatizador"), 1500000.0),

    # 9. Consolas y videojuegos
    (("consola", "consolas", "nintendo switch", "playstation", "xbox"), 1600000.0),

    # 10. Smartwatches
    (("smartwatch", "smartwatches", "reloj inteligente"), 350000.0),

    # 11. Audífonos y sonido
    (("audifonos", "diadema", "diademas", "parlante", "parlantes", "auriculares"), 300000.0),

    # 12. Tenis y calzado
    (("tenis", "zapatos", "zapatillas", "sneakers", "botas", "sandalias", "calzado"), 160000.0),

    # 13. Chaquetas y buzos
    (("chaqueta", "chaquetas", "buzo", "buzos", "hoodie"), 110000.0),

    # 14. Bolsos y morrales
    (("morral", "morrales", "maleta", "maletas", "billetera", "billeteras", "bolso", "bolsos", "mochila", "mochilas"), 100000.0),

    # 15. Jeans y pantalones
    (("jean", "jeans", "pantalon", "pantalones", "sudadera", "sudaderas", "bermuda", "bermudas", "pantaloneta"), 90000.0),

    # 16. Camisetas y polos
    (("camiseta", "camisetas", "polo", "polos", "camisa", "camisas", "t-shirt", "tshirt", "playera", "esqueleto"), 60000.0),

    # 17. Pequeños electrodomésticos y cocina
    (("freidora", "freidoras", "air fryer", "airfryer", "freidora de aire", "microondas",
      "licuadora", "licuadoras", "cafetera", "cafeteras", "sanduchera", "sandwichera",
      "waflera", "batidora", "procesador de alimentos", "arrocera", "arroceras",
      "olla", "ollas", "sarten", "sartenes", "bateria de cocina",
      "aspiradora", "aspiradoras", "robot aspiradora", "ventilador", "ventiladores",
      "taladro", "taladros", "herramientas", "destornillador"), 250000.0),
]


def _precio_admisible(deal: Deal, trm: float = 4000.0) -> bool:
    """Verifica que el precio este dentro del tope especifico de su categoria o tope general."""
    if deal.price is None:
        return True
    precio_cop = deal.price if deal.currency == "COP" else (deal.price * trm)

    # 1. Buscar si coincide con alguna categoria especifica
    for keywords, tope in TOPES_CATEGORIA_COP:
        if any(filtros.menciona(deal.title, kw) for kw in keywords):
            return precio_cop <= tope

    # 2. Si no coincide con ninguna categoria especifica, aplicar el tope general (si esta configurado)
    if config.MAX_PRICE_COP <= 0:
        return True
    return precio_cop <= config.MAX_PRICE_COP


DEPTO_ROPA = {
    "camiseta", "polo", "camisa", "jean", "jeans", "pantalon", "pantalones",
    "chaqueta", "chaquetas", "buzo", "buzos", "hoodie", "morral", "morrales",
    "maleta", "maletas", "billetera", "billeteras", "bolso", "bolsos", "mochila",
    "mochilas", "sudadera", "sudaderas", "ropa", "tenis", "zapatos", "zapatillas",
    "sneakers", "botas", "sandalias", "calzado", "bermuda", "bermudas",
    "t-shirt", "tshirt", "playera", "esqueleto", "deportivo", "deportiva"
}

DEPTO_LINEA_BLANCA = {
    "nevera", "neveras", "refrigerador", "refrigeradores", "nevecon", "nevecones",
    "refrigeradora", "freezer", "lavadora", "lavadoras", "secadora", "secadoras",
    "torre de lavado", "lavaseca", "estufa", "estufas", "cubierta a gas",
    "horno", "hornos", "aire acondicionado", "aires acondicionados", "climatizador"
}

DEPTO_COCINA = {
    "freidora", "freidoras", "air fryer", "airfryer", "freidora de aire",
    "sandwichera", "sandwicheras", "sanduchera", "sanducheras", "waflera", "wafleras",
    "tostadora", "tostadoras", "licuadora", "licuadoras", "procesador de alimentos",
    "batidora", "batidoras", "cafetera", "cafeteras", "microondas", "arrocera",
    "arroceras", "olla", "ollas", "sarten", "sartenes", "bateria de cocina"
}

DEPTO_TECNOLOGIA = {
    "televisor", "televisores", "smart tv", "tv", "portatil", "portatiles",
    "laptop", "laptops", "computador", "computadores", "macbook", "notebook",
    "celular", "celulares", "smartphone", "smartphones", "iphone", "galaxy",
    "telefono", "telefonos", "monitor", "monitores", "gamer", "audifonos",
    "diadema", "diademas", "parlante", "parlantes", "auriculares", "consola",
    "consolas", "playstation", "xbox", "nintendo", "smartwatch", "smartwatches",
    "reloj inteligente"
}

DEPTO_HOGAR = {
    "aspiradora", "aspiradoras", "robot aspiradora", "ventilador", "ventiladores",
    "taladro", "taladros", "herramientas", "destornillador"
}


def _tiendas_por_departamento(consultas_custom: list[str] | None) -> tuple[list[str], list[str], list[str]]:
    """Selecciona solo los almacenes relevantes según el rubro de la búsqueda para evitar peticiones inútiles."""
    algolia_todas = ["alkosto", "ktronix", "alkomprar"]
    vtex_todas = [
        "exito", "carulla", "olimpica", "jumbo", "haceb", "whirlpool", "imusa", "oster",
        "arturocalle", "totto", "studiof", "velez", "americanino"
    ]
    falabella_todas = ["falabella", "homecenter"]

    if not consultas_custom:
        return algolia_todas, vtex_todas, falabella_todas

    palabras = set()
    for c in consultas_custom:
        for p in filtros._normalizar(c).split():
            palabras.add(p)

    # 1. Ropa, Calzado y Accesorios
    if palabras & DEPTO_ROPA:
        return (
            [],  # Ninguna de Algolia vende moda
            ["exito", "jumbo", "totto", "arturocalle", "studiof", "velez", "americanino"],
            ["falabella"],
        )

    # 2. Neveras, Lavadoras y Línea Blanca
    if palabras & DEPTO_LINEA_BLANCA:
        return (
            ["alkosto", "ktronix", "alkomprar"],
            ["exito", "jumbo", "olimpica", "carulla", "haceb", "whirlpool"],
            ["falabella", "homecenter"],
        )

    # 3. Cocina y Pequeños Electrodomésticos
    if palabras & DEPTO_COCINA:
        return (
            ["alkosto", "ktronix", "alkomprar"],
            ["exito", "jumbo", "olimpica", "carulla", "imusa", "oster", "haceb"],
            ["falabella", "homecenter"],
        )

    # 4. Tecnología
    if palabras & DEPTO_TECNOLOGIA:
        return (
            ["alkosto", "ktronix", "alkomprar"],
            ["exito", "jumbo", "olimpica", "carulla"],
            ["falabella"],
        )

    # 5. Hogar y Herramientas
    if palabras & DEPTO_HOGAR:
        return (
            ["alkosto", "alkomprar"],
            ["exito", "jumbo", "carulla", "olimpica"],
            ["falabella", "homecenter"],
        )

    return algolia_todas, vtex_todas, falabella_todas


def _mejores_colombia(watchlist: dict, vistas: set, cuantas: int,
                      consultas_custom: list[str] | None = None,
                      trm: float = 4000.0,
                      max_por_tienda: int = 3) -> list:
    """Las mejores ofertas de tiendas de Todo Colombia, garantizando el Top 3 por tienda en comparaciones."""
    ofertas: list[Deal] = []

    tiendas_algolia, tiendas_vtex, tiendas_falabella = _tiendas_por_departamento(consultas_custom)

    if tiendas_algolia:
        ofertas += _ofertas_de(watchlist, "algolia_co", tiendas_algolia, consultas_custom=consultas_custom)

    if tiendas_vtex:
        ofertas += _ofertas_de(watchlist, "vtex", tiendas_vtex, consultas_custom=consultas_custom)

    if tiendas_falabella:
        ofertas += _ofertas_de(watchlist, "falabella", tiendas_falabella, consultas_custom=consultas_custom)

    ofertas = _marketplace_solo_si_mejora(_sin_repetidas(ofertas))
    disponibles = [d for d in ofertas if d.in_stock and _precio_admisible(d, trm)]
    if consultas_custom:
        disponibles = [d for d in disponibles
                       if any(filtros.menciona(d.title, c) for c in consultas_custom)]
    candidatas = [(d, Verdict(True, "pedido a mano")) for d in disponibles
                  if d.discount_verificable > 0]
    candidatas.sort(key=lambda par: -par[0].discount_verificable)
    unicas, _hermanas = _colapsar_variantes(candidatas)

    # En comparacion de categorias: garantizar el Top 3 de cada tienda
    if consultas_custom and max_por_tienda > 0:
        por_tienda_nuevas: dict[str, list] = {}
        por_tienda_repetidas: dict[str, list] = {}
        for par in unicas:
            tienda_nombre = par[0].store or par[0].source
            if par[0].key in vistas:
                por_tienda_repetidas.setdefault(tienda_nombre, []).append(par)
            else:
                por_tienda_nuevas.setdefault(tienda_nombre, []).append(par)

        todas_tiendas = list(dict.fromkeys(list(por_tienda_nuevas.keys()) + list(por_tienda_repetidas.keys())))
        balanceadas = []
        for tienda_nombre in todas_tiendas:
            nuevas_t = por_tienda_nuevas.get(tienda_nombre, [])
            repetidas_t = por_tienda_repetidas.get(tienda_nombre, [])
            balanceadas.extend((nuevas_t + repetidas_t)[:max_por_tienda])

        limite = max(cuantas, len(balanceadas))
        seleccion = balanceadas[:limite]
    else:
        nuevas = [par for par in unicas if par[0].key not in vistas]
        repetidas = [par for par in unicas if par[0].key in vistas]
        seleccion = (nuevas + repetidas)[:cuantas]

    for deal, verdict in seleccion:
        if deal.key in vistas:
            verdict.etiquetas.append("ya te la habia mostrado")
    return seleccion


def _mejores(watchlist: dict, fuentes: list[str], tiendas, vistas: set,
             cuantas: int, consultas_custom: list[str] | None = None,
             trm: float = 4000.0) -> list:
    """Las mejores ofertas de esas fuentes, priorizando las que no has visto."""
    ofertas: list[Deal] = []
    for fuente in fuentes:
        if consultas_custom is not None:
            ofertas += _ofertas_de(watchlist, fuente, tiendas, consultas_custom=consultas_custom)
        else:
            ofertas += _ofertas_de(watchlist, fuente, tiendas)
    ofertas = _marketplace_solo_si_mejora(_sin_repetidas(ofertas))

    disponibles = [d for d in ofertas if d.in_stock and _precio_admisible(d, trm)]
    if consultas_custom:
        disponibles = [d for d in disponibles
                       if any(filtros.menciona(d.title, c) for c in consultas_custom)]
    if all(f in SIN_PRECIO_DE_LISTA for f in fuentes):
        # Ni Slickdeals ni PROMOCAJITA publican precio de lista: no hay
        # porcentaje que ordenar, pero ya vienen ordenadas por la comunidad.
        candidatas = [(d, Verdict(True, "pedido a mano")) for d in disponibles]
    else:
        candidatas = [(d, Verdict(True, "pedido a mano")) for d in disponibles
                      if d.discount_verificable > 0]
        candidatas.sort(key=lambda par: -par[0].discount_verificable)

    unicas, _hermanas = _colapsar_variantes(candidatas)

    # Pedir la misma tienda dos veces seguidas debe traer cosas distintas.
    nuevas = [par for par in unicas if par[0].key not in vistas]
    repetidas = [par for par in unicas if par[0].key in vistas]
    seleccion = (nuevas + repetidas)[:cuantas]
    for deal, verdict in seleccion:
        if deal.key in vistas:
            verdict.etiquetas.append("ya te la habia mostrado")
    return seleccion


_token_busqueda_activa: int = 0


def nueva_busqueda() -> int:
    """Incrementa el token de busqueda para cancelar cualquier envio previo si el usuario pide otra cosa."""
    global _token_busqueda_activa
    _token_busqueda_activa += 1
    return _token_busqueda_activa


def atender_comandos(por_comando: int | None = None) -> dict:
    """Pregunta por comandos pendientes (getUpdates) y los responde.

    Solo funciona si NO hay webhook registrado: Telegram no deja usar los dos
    caminos a la vez. Queda para probar desde el portatil.
    """
    return atender_solicitudes(mod_comandos.pendientes(), por_comando)


def atender_solicitudes(solicitudes: list[dict],
                        por_comando: int | None = None) -> dict:
    """Responde comandos ya leidos, vengan del webhook o de getUpdates.

    No toca radar.db: una consulta a voluntad no debe alterar lo que el radar
    programado considera "ya avisado".
    """
    if not solicitudes:
        print("Sin comandos nuevos.")
        return _resultado()

    watchlist = None
    trm = None
    vistas = None
    atendidos = 0

    for solicitud in solicitudes:
        comando = solicitud["comando"]
        tipo = solicitud.get("tipo", "slash")
        chat_id = solicitud.get("chat_id")
        # El numero que pediste manda; si no pediste, el valor por defecto.
        cuantas = (solicitud.get("cantidad") or por_comando
                   or config.COMANDO_RESULTADOS)
        print(f"-> comando /{comando} (tipo={tipo}, chat={chat_id}, {cuantas} resultados)")

        user_id = solicitud.get("user_id")
        if user_id and tipo not in ("solicitud_acceso", "unirse_canal"):
            if whitelist.registrar_inicio_sesion(user_id, ventana_segundos=1800):
                nombre_u = solicitud.get("nombre") or "Usuario"
                user_u = solicitud.get("username")
                handle_u = f" (@{user_u})" if user_u else ""
                if config.TELEGRAM_ADMIN_ID:
                    telegram.send(
                        f"👤 <b>{telegram.esc(nombre_u)}</b>{telegram.esc(handle_u)} inició una sesión en PromosBot.",
                        chat_id=config.TELEGRAM_ADMIN_ID,
                    )

        if tipo == "unirse_canal":
            nombre = solicitud.get("nombre", "Usuario")
            telegram.send(
                f"👋 <b>¡Hola, {telegram.esc(nombre)}!</b>\n\n"
                f"Para poder usar <b>PromosBot</b> y consultar todas las ofertas, "
                f"primero debes estar unido a nuestro canal oficial:\n\n"
                f"📢 <b>Ofertas</b>\n\n"
                f"<i>Únete con el botón de abajo y luego presiona 'Ya me uní':</i>",
                reply_markup=telegram.teclado_unirse_canal(),
                chat_id=chat_id,
            )
            atendidos += 1
            continue

        if tipo == "solicitud_acceso":
            user_id = solicitud.get("user_id")
            nombre = solicitud.get("nombre", "Usuario")
            username = solicitud.get("username", "")
            es_nueva = solicitud.get("es_nueva", False)
            user_handle = f"@{username}" if username else "(sin username)"

            if es_nueva:
                telegram.send(
                    f"👋 <b>¡Hola, {telegram.esc(nombre)}!</b>\n\n"
                    f"✅ Confirmamos que estás unido a nuestro canal oficial.\n"
                    f"Tu solicitud de acceso a PromosBot fue enviada al administrador. "
                    f"Te notificaremos automáticamente apenas sea aprobada.",
                    chat_id=chat_id,
                )
                if config.TELEGRAM_ADMIN_ID:
                    telegram.send(
                        f"🔔 <b>Nueva solicitud de acceso a PromosBot</b>\n\n"
                        f"👤 <b>Usuario:</b> {telegram.esc(nombre)} ({telegram.esc(user_handle)})\n"
                        f"🆔 <b>ID:</b> <code>{user_id}</code>\n"
                        f"📢 <b>Canal:</b> ✅ Unido a Ofertas\n\n"
                        f"¿Deseas autorizarlo?",
                        reply_markup=telegram.teclado_aprobacion(user_id),
                        chat_id=config.TELEGRAM_ADMIN_ID,
                    )
            else:
                telegram.send(
                    "⏳ Tu solicitud aún está pendiente de aprobación por el administrador.",
                    chat_id=chat_id,
                )
            atendidos += 1
            continue

        if comando in ("ayuda", "help"):
            telegram.send(mod_comandos.AYUDA, reply_markup=telegram.teclado_tiendas(), chat_id=chat_id)
            atendidos += 1
            continue

        if comando in ("start", "menu") or tipo == "menu":
            telegram.send(
                "🤖 <b>PromosBot — Menú Principal</b>\n\n"
                "Toca una tienda o el <b>🇨🇴 Comparador</b> en los botones inferiores para explorar ofertas:",
                reply_markup=telegram.teclado_tiendas(),
                chat_id=chat_id,
            )
            atendidos += 1
            continue

        if comando == "objetivos":
            lineas = [
                "🎯 <b>Tus objetivos de precio configurados:</b>",
                "",
                "• 👕 <b>Ropa:</b> menos de $50.000",
                "• 👟 <b>Zapatos / Tenis:</b> menos de $150.000",
                "• 📺 <b>Smart TV:</b> menos de $1.500.000",
                "• 🖥️ <b>Monitores:</b> menos de $800.000",
                "• 🧺 <b>Lavadoras:</b> menos de $1.500.000",
                "• ❄️ <b>Neveras:</b> menos de $1.800.000",
                "• 🍳 <b>Electrodomésticos:</b> menos de $250.000",
                "",
                "<i>En 'Todo Colombia' se monitorean en las 6 tiendas: Éxito, Carulla, Alkosto, K-tronix, Falabella y Olímpica.</i>",
            ]
            telegram.send(telegram.NL.join(lineas), reply_markup=telegram.teclado_tiendas(), chat_id=chat_id)
            atendidos += 1
            continue

        if comando == "estado":
            with Store() as store:
                enviadas = store.enviadas_hoy()
                archivadas = store.conn.execute(
                    "SELECT COUNT(*) FROM alerts").fetchone()[0]
            telegram.send(
                f"📊 <b>Estado del radar</b>{telegram.NL}"
                f"Alertas enviadas hoy: <b>{enviadas}</b> de {config.MAX_ALERTS_PER_DAY}"
                f"{telegram.NL}Ofertas en memoria: <b>{archivadas}</b>",
                reply_markup=telegram.teclado_tiendas(),
                chat_id=chat_id,
            )
            atendidos += 1
            continue

        if tipo == "elegir_tienda":
            tienda = solicitud.get("tienda", "colombia")
            tienda_nombre = solicitud.get("tienda_nombre", tienda.capitalize())
            mod_comandos.fijar_tienda_activa(tienda, chat_id=chat_id)
            if tienda == "colombia":
                telegram.send(
                    "🇨🇴 <b>Comparador Nacional de Tiendas</b>\n\n"
                    "Elige un departamento o producto abajo para comparar y ver el <b>Top 3 de cada tienda</b> en Colombia:\n"
                    "<i>(O presiona 🌟 TODO para ver las mejores rebajas generales)</i>",
                    reply_markup=telegram.teclado_categorias(tienda_nombre),
                    chat_id=chat_id,
                )
            else:
                telegram.send(
                    f"🏬 <b>{telegram.esc(tienda_nombre)} seleccionado</b>\n\n"
                    f"Elige una opción abajo para buscar rebajas específicas o presiona <b>🌟 TODO</b> para ver las mejores ofertas generales de la tienda:",
                    reply_markup=telegram.teclado_categorias(tienda_nombre),
                    chat_id=chat_id,
                )
            atendidos += 1
            continue

        if tipo == "grupo_categoria":
            grupo = solicitud.get("grupo")
            tienda = mod_comandos.tienda_activa(chat_id=chat_id)
            fuente, _, titulo_tienda = mod_comandos.CATALOGO.get(tienda, ("co", None, "Colombia"))

            if grupo == "cocina":
                telegram.send(
                    f"🍳 <b>Cocina en {telegram.esc(titulo_tienda)}</b>\n\nElige el producto específico que buscas:",
                    reply_markup=telegram.teclado_cocina(),
                    chat_id=chat_id,
                )
            elif grupo == "tecnologia":
                telegram.send(
                    f"💻 <b>Tecnología en {telegram.esc(titulo_tienda)}</b>\n\nElige el producto específico que buscas:",
                    reply_markup=telegram.teclado_tecnologia(),
                    chat_id=chat_id,
                )
            elif grupo == "neveras":
                telegram.send(
                    f"❄️ <b>Neveras y Lavadoras en {telegram.esc(titulo_tienda)}</b>\n\nElige el producto específico que buscas:",
                    reply_markup=telegram.teclado_neveras_lavadoras(),
                    chat_id=chat_id,
                )
            elif grupo == "ropa":
                telegram.send(
                    f"👟 <b>Ropa y Calzado en {telegram.esc(titulo_tienda)}</b>\n\nElige el producto específico que buscas:",
                    reply_markup=telegram.teclado_ropa(),
                    chat_id=chat_id,
                )
            elif grupo == "hogar":
                telegram.send(
                    f"🏠 <b>Hogar y Herramientas en {telegram.esc(titulo_tienda)}</b>\n\nElige el producto específico que buscas:",
                    reply_markup=telegram.teclado_hogar(),
                    chat_id=chat_id,
                )
            elif grupo == "volver":
                telegram.send(
                    f"🏬 <b>{telegram.esc(titulo_tienda)}</b>\n\nElige un grupo o presiona <b>🌟 TODO</b>:",
                    reply_markup=telegram.teclado_categorias(titulo_tienda),
                    chat_id=chat_id,
                )
            atendidos += 1
            continue

        # Inicializacion bajo demanda solo cuando realmente se van a buscar ofertas
        if tipo == "categoria":
            token_actual = _token_busqueda_activa
            categoria_nombre = solicitud.get("categoria_nombre", "Categoría")
            tienda = mod_comandos.tienda_activa(chat_id=chat_id)
            if tienda not in mod_comandos.CATALOGO and tienda not in ("objetivos", "estado"):
                tienda = "colombia"

            fuente, tiendas, titulo_tienda = mod_comandos.CATALOGO.get(
                tienda, ("co", None, "Colombia"))

            # Notificar de inmediato al usuario que se inicio la revision si no se envio antes
            if not solicitud.get("notificado"):
                telegram.accion_escribiendo(chat_id=chat_id)
                if fuente == "co":
                    telegram.send(f"🇨🇴 <i>Comparando el <b>Top 3 de {telegram.esc(categoria_nombre)}</b> en todas las tiendas de Colombia...</i>", chat_id=chat_id)
                else:
                    telegram.send(f"🔍 <i>Revisando ofertas de <b>{telegram.esc(categoria_nombre)}</b> en <b>{telegram.esc(titulo_tienda)}</b>...</i>", chat_id=chat_id)

            if watchlist is None:
                watchlist = config.load_watchlist()
            if (tienda in ("exterior", "todo") or fuente in ("slickdeals", "*")) and trm is None:
                trm, _origen = fx.get_trm(None)
            if vistas is None:
                vistas = mod_comandos.ya_mostradas(chat_id=chat_id)
                try:
                    with Store() as store:
                        vistas.update(f["key"] for f in store.conn.execute("SELECT key FROM alerts"))
                except Exception as exc:
                    print(f"  [comandos] sin historial del radar: {exc}")

            if tienda == "exterior":
                consultas = mod_comandos.CATEGORIAS_BUSQUEDA_EN.get(
                    categoria_nombre, solicitud.get("consultas", []))
            else:
                consultas = solicitud.get("consultas", [])

            if fuente == "co":
                # Consulta las tiendas autorizadas garantizando Top 3 por tienda
                seleccion = _mejores_colombia(watchlist, vistas, cuantas, consultas_custom=consultas, trm=trm or 4000.0)
            elif fuente == "*":
                del_exterior = max(cuantas // 5, 1)
                seleccion = (_mejores(watchlist, ["algolia_co", "vtex", "falabella", "droguerias"],
                                      None, vistas, cuantas - del_exterior, consultas_custom=consultas, trm=trm or 4000.0)
                             + _mejores(watchlist, ["slickdeals"], None, vistas, del_exterior, consultas_custom=consultas, trm=trm or 4000.0))
            else:
                seleccion = _mejores(watchlist, [fuente], tiendas, vistas, cuantas,
                                     consultas_custom=consultas, trm=trm or 4000.0)

            if token_actual != _token_busqueda_activa:
                print("  [radar] categoria cancelada por nueva solicitud")
                atendidos += 1
                continue

            if not seleccion:
                telegram.send(
                    f"Ahora mismo no encontré rebajas destacadas en {telegram.esc(categoria_nombre)} para <b>{telegram.esc(titulo_tienda)}</b>.",
                    reply_markup=telegram.teclado_categorias(titulo_tienda),
                    chat_id=chat_id,
                )
                atendidos += 1
                continue

            if fuente == "co":
                encabezado = (f"🇨🇴 <b>Comparador Nacional · {telegram.esc(categoria_nombre)}</b>\n"
                              f"<i>Top 3 mejores rebajas de cada tienda en Colombia:</i>")
            else:
                encabezado = (f"🏬 <b>{telegram.esc(titulo_tienda)}</b> · {telegram.esc(categoria_nombre)}\n"
                              f"<i>Mejores rebajas encontradas ahora mismo:</i>")

            telegram.send(
                encabezado,
                reply_markup=telegram.teclado_categorias(titulo_tienda),
                chat_id=chat_id,
            )
            enviadas_ahora = []
            for deal, verdict in seleccion:
                if token_actual != _token_busqueda_activa:
                    print("  [radar] envio interrumpido por nueva solicitud")
                    break
                landed = None
                if deal.country == "US" and deal.price:
                    if trm is None:
                        trm, _origen = fx.get_trm(None)
                    landed = calcular(deal.price, trm, deal.weight_lb)
                telegram.enviar_oferta(deal, verdict, landed, chat_id=chat_id)
                enviadas_ahora.append(deal.key)
                time.sleep(1.2)
            if token_actual != _token_busqueda_activa:
                atendidos += 1
                continue
            mod_comandos.marcar_mostradas(enviadas_ahora, chat_id=chat_id)
            vistas.update(enviadas_ahora)

            if fuente == "co":
                msg_fin = (f"🏁 <b>Comparación finalizada</b> · Se enviaron las <b>{len(enviadas_ahora)}</b> mejores ofertas "
                           f"(Top 3 por tienda) de {telegram.esc(categoria_nombre)} en Colombia.\n"
                           f"<i>Puedes elegir otra opción en los botones:</i>")
            else:
                msg_fin = (f"🏁 <b>Búsqueda finalizada</b> · Se enviaron las <b>{len(enviadas_ahora)}</b> mejores ofertas "
                           f"de {telegram.esc(categoria_nombre)} en {telegram.esc(titulo_tienda)}.\n"
                           f"<i>Puedes elegir otra opción en los botones:</i>")

            telegram.send(
                msg_fin,
                reply_markup=telegram.teclado_categorias(titulo_tienda),
                chat_id=chat_id,
            )
            atendidos += 1
            continue

        if tipo == "todo_tienda":
            token_actual = _token_busqueda_activa
            tienda = mod_comandos.tienda_activa(chat_id=chat_id)
            if tienda not in mod_comandos.CATALOGO:
                tienda = "colombia"
            fuente, tiendas, titulo = mod_comandos.CATALOGO[tienda]

            # Notificar de inmediato al usuario si no se envio antes
            if not solicitud.get("notificado"):
                telegram.accion_escribiendo(chat_id=chat_id)
                telegram.send(f"🔍 <i>Revisando las mejores ofertas en <b>{telegram.esc(titulo)}</b>...</i>", chat_id=chat_id)

            if watchlist is None:
                watchlist = config.load_watchlist()
            if (tienda in ("exterior", "todo") or fuente in ("slickdeals", "*")) and trm is None:
                trm, _origen = fx.get_trm(None)
            if vistas is None:
                vistas = mod_comandos.ya_mostradas(chat_id=chat_id)
                try:
                    with Store() as store:
                        vistas.update(f["key"] for f in store.conn.execute("SELECT key FROM alerts"))
                except Exception as exc:
                    print(f"  [comandos] sin historial del radar: {exc}")

            if fuente == "co":
                # Consulta las 6 tiendas autorizadas: algolia_co, vtex, falabella
                seleccion = _mejores_colombia(watchlist, vistas, cuantas, trm=trm or 4000.0)
            elif fuente == "*":
                del_exterior = max(cuantas // 5, 1)
                seleccion = (_mejores(watchlist, ["algolia_co", "vtex", "falabella", "droguerias"],
                                      None, vistas, cuantas - del_exterior, trm=trm or 4000.0)
                             + _mejores(watchlist, ["slickdeals"], None, vistas, del_exterior, trm=trm or 4000.0))
            else:
                seleccion = _mejores(watchlist, [fuente], tiendas, vistas, cuantas, trm=trm or 4000.0)

            if token_actual != _token_busqueda_activa:
                print("  [radar] todo_tienda cancelado por nueva solicitud")
                atendidos += 1
                continue

            if not seleccion:
                telegram.send(f"Ahora mismo no encuentro rebajas en {telegram.esc(titulo)}.",
                              reply_markup=telegram.teclado_categorias(titulo),
                              chat_id=chat_id)
                atendidos += 1
                continue

            telegram.send(f"🏬 <b>{telegram.esc(titulo)}</b> — lo mejor de ahora mismo",
                          reply_markup=telegram.teclado_categorias(titulo),
                          chat_id=chat_id)
            enviadas_ahora = []
            for deal, verdict in seleccion:
                if token_actual != _token_busqueda_activa:
                    print("  [radar] envio interrumpido por nueva solicitud")
                    break
                landed = None
                if deal.country == "US" and deal.price:
                    if trm is None:
                        trm, _origen = fx.get_trm(None)
                    landed = calcular(deal.price, trm, deal.weight_lb)
                telegram.enviar_oferta(deal, verdict, landed, chat_id=chat_id)
                enviadas_ahora.append(deal.key)
                time.sleep(1.2)
            if token_actual != _token_busqueda_activa:
                atendidos += 1
                continue
            mod_comandos.marcar_mostradas(enviadas_ahora, chat_id=chat_id)
            vistas.update(enviadas_ahora)
            telegram.send(
                f"🏁 <b>Búsqueda finalizada</b> · Se enviaron las <b>{len(enviadas_ahora)}</b> mejores ofertas de {telegram.esc(titulo)}.\n"
                f"<i>Puedes seguir explorando en los botones:</i>",
                reply_markup=telegram.teclado_categorias(titulo),
                chat_id=chat_id,
            )
            atendidos += 1
            continue

        if comando not in mod_comandos.CATALOGO:
            telegram.send(f"No conozco /{telegram.esc(comando)}. Escribe /ayuda o usa el menú interactivo.",
                          reply_markup=telegram.teclado_tiendas(),
                          chat_id=chat_id)
            continue

        token_actual = _token_busqueda_activa
        mod_comandos.fijar_tienda_activa(comando, chat_id=chat_id)
        fuente, tiendas, titulo = mod_comandos.CATALOGO[comando]

        # Notificar de inmediato al usuario si no se envio antes
        if not solicitud.get("notificado"):
            telegram.accion_escribiendo(chat_id=chat_id)
            telegram.send(f"🔍 <i>Revisando ofertas en <b>{telegram.esc(titulo)}</b>...</i>", chat_id=chat_id)

        if watchlist is None:
            watchlist = config.load_watchlist()
        if (comando in ("exterior", "todo") or fuente in ("slickdeals", "*")) and trm is None:
            trm, _origen = fx.get_trm(None)
        if vistas is None:
            vistas = mod_comandos.ya_mostradas(chat_id=chat_id)
            try:
                with Store() as store:
                    vistas.update(f["key"] for f in store.conn.execute("SELECT key FROM alerts"))
            except Exception as exc:
                print(f"  [comandos] sin historial del radar: {exc}")

        if fuente == "co":
            # Consulta las 6 tiendas autorizadas: algolia_co, vtex, falabella
            seleccion = _mejores_colombia(watchlist, vistas, cuantas, trm=trm or 4000.0)
        elif fuente == "*":
            # Mezcla deliberada: las de Colombia se ordenan por descuento, pero
            # las del exterior no tienen porcentaje y nunca ganarian ese orden,
            # asi que se les reserva un cupo.
            del_exterior = max(cuantas // 5, 1)
            seleccion = (_mejores(watchlist, ["algolia_co", "vtex", "falabella", "droguerias"],
                                  None, vistas, cuantas - del_exterior, trm=trm or 4000.0)
                         + _mejores(watchlist, ["slickdeals"], None, vistas, del_exterior, trm=trm or 4000.0))
        else:
            seleccion = _mejores(watchlist, [fuente], tiendas, vistas, cuantas, trm=trm or 4000.0)

        if token_actual != _token_busqueda_activa:
            print("  [radar] comando cancelado por nueva solicitud")
            atendidos += 1
            continue

        teclado_salida = (telegram.teclado_tiendas() if comando == "cajita"
                          else telegram.teclado_categorias(titulo))
        if not seleccion:
            telegram.send(f"Ahora mismo no encuentro rebajas en {telegram.esc(titulo)}.",
                          reply_markup=teclado_salida,
                          chat_id=chat_id)
            atendidos += 1
            continue

        telegram.send(f"🏬 <b>{telegram.esc(titulo)}</b> — "
                      f"lo mejor de ahora mismo",
                      reply_markup=teclado_salida,
                      chat_id=chat_id)
        enviadas_ahora = []
        for deal, verdict in seleccion:
            if token_actual != _token_busqueda_activa:
                print("  [radar] envio interrumpido por nueva solicitud")
                break
            landed = None
            if deal.country == "US" and deal.price:
                if trm is None:
                    trm, _origen = fx.get_trm(None)
                landed = calcular(deal.price, trm, deal.weight_lb)
            telegram.enviar_oferta(deal, verdict, landed, chat_id=chat_id)
            enviadas_ahora.append(deal.key)
            time.sleep(1.2)
        if token_actual != _token_busqueda_activa:
            atendidos += 1
            continue
        mod_comandos.marcar_mostradas(enviadas_ahora, chat_id=chat_id)
        vistas.update(enviadas_ahora)
        telegram.send(
            f"🏁 <b>Búsqueda finalizada</b> · Se enviaron las <b>{len(enviadas_ahora)}</b> ofertas de {telegram.esc(titulo)}.\n"
            f"<i>Puedes seguir buscando con los botones:</i>",
            reply_markup=teclado_salida,
            chat_id=chat_id,
        )
        atendidos += 1

    print(f"Comandos atendidos: {atendidos}")
    return _resultado(enviadas=atendidos)


def _resultado(**campos) -> dict:
    base = {"ofertas": 0, "candidatas": 0, "enviadas": 0, "resumen": 0,
            "duracion": 0.0, "error": None}
    base.update(campos)
    return base


def ejecutar_ronda(fuentes=None, dry_run: bool = False, limite: int | None = None,
                   top: int | None = None) -> dict:
    """Una ronda completa. Devuelve un resumen para quien la haya invocado.

    Con `top` se pide un vistazo manual: las N mejores rebajas de ahora mismo,
    saltandose la deduplicacion y el freno diario. Sirve para revisar el
    catalogo a voluntad, no para el piloto automatico.
    """
    watchlist = config.load_watchlist()
    if not watchlist:
        print(f"No encontre {config.WATCHLIST_PATH}. Crea el archivo con tus busquedas.")
        return _resultado(error="falta watchlist.json")

    objetivos_cfg = watchlist.get("objetivos") or []
    watchlist = _con_objetivos(watchlist, objetivos_cfg)
    activas = fuentes or list(FUENTES)

    if not top and not dry_run:
        with Store() as store:
            if store.enviadas_hoy() >= config.MAX_ALERTS_PER_DAY:
                print(f"Tope diario alcanzado ({config.MAX_ALERTS_PER_DAY}); ronda de fondo omitida.")
                return _resultado(duracion=0.0)

    inicio = time.time()
    ofertas = _marketplace_solo_si_mejora(_sin_repetidas(recolectar(watchlist, activas)))
    print(f"\nRecolectadas {len(ofertas)} ofertas en {time.time() - inicio:.1f}s")

    if not ofertas:
        return _resultado(duracion=round(time.time() - inicio, 1),
                          error="ninguna fuente devolvio ofertas")

    with Store() as store:
        trm, origen_trm = fx.get_trm(store)
        print(f"TRM en uso: ${trm:,.2f} ({origen_trm})\n".replace(",", "."))

        candidatas: list[tuple[Deal, Verdict]] = []
        veracidades: dict = {}
        falsas = 0
        for deal in ofertas:
            # El historial se consulta antes de registrar el precio de hoy,
            # para que la mediana no quede contaminada por la observacion actual.
            stats = store.price_stats(deal.key)
            historial = store.historial(deal.key, config.VERACITY_WINDOW_DAYS)
            store.record(deal)

            # Contraste contra el minimo real de la ventana: es lo unico que
            # desenmascara la maniobra de subir el precio para luego "rebajarlo".
            veracidad = mod_veracidad.analizar(deal.price, historial)
            objetivo = mod_objetivos.alcanzado(deal, objetivos_cfg)
            if not objetivo and not _precio_admisible(deal, trm):
                continue
            verdict = evaluar(deal, stats, objetivo, veracidad)

            if top:
                # Vistazo manual: manda lo mas rebajado, ya se haya avisado o no.
                if deal.discount_verificable > 0 and deal.in_stock:
                    verdict.alertar = True
                    verdict.motivo = f"-{deal.discount_verificable:g}% (vistazo manual)"
                    veracidades[deal.key] = veracidad
                    candidatas.append((deal, verdict))
                continue

            if not verdict.alertar:
                if veracidad.es_falsa:
                    falsas += 1
                continue
            enviar, motivo = store.should_alert(deal)
            if not enviar:
                continue
            verdict.motivo = f"{verdict.motivo} ({motivo})"
            veracidades[deal.key] = veracidad
            candidatas.append((deal, verdict))

        if top:
            candidatas.sort(key=lambda par: -par[0].discount_verificable)
        else:
            candidatas.sort(key=_orden)
        antes = len(candidatas)
        candidatas, hermanas = _colapsar_variantes(candidatas)
        colapsadas = antes - len(candidatas)
        if config.DIGEST_ENABLED:
            inmediatas = [par for par in candidatas if par[1].inmediata]
            para_resumen = [par for par in candidatas if not par[1].inmediata]
        else:
            # Sin resumen: todo lo que vale la pena va como tarjeta propia.
            inmediatas, para_resumen = candidatas, []
        tope = top or limite or config.MAX_ALERTS_PER_RUN
        primera = (not top) and store.get_meta("ronda_inicial") is None
        if primera and config.SEED_ON_EMPTY_DB:
            tope = min(tope, config.SEED_MAX_ALERTS)
        seleccion = inmediatas[:tope]

        print(f"{len(candidatas)} candidatas ({colapsadas} variantes colapsadas): "
              f"{len(inmediatas)} inmediatas (envio {len(seleccion)}), "
              f"{len(para_resumen)} al resumen"
              + (f"; {falsas} rebajas falsas descartadas" if falsas else ""))
        print("")

        enviados = 0
        formatos: dict = {}
        cupo_diario = (top if top else
                       config.MAX_ALERTS_PER_DAY - store.enviadas_hoy())
        if cupo_diario <= 0 and not dry_run:
            print(f"Tope diario alcanzado ({config.MAX_ALERTS_PER_DAY}); "
                  "no se envia nada mas hoy.")
            seleccion = []

        for deal, verdict in seleccion:
            if not dry_run and enviados >= cupo_diario:
                print(f"Tope diario alcanzado ({config.MAX_ALERTS_PER_DAY}).")
                break
            landed = None
            if deal.country == "US" and deal.price:
                landed = calcular(deal.price, trm, deal.weight_lb)
            veracidad = veracidades.get(deal.key)

            if dry_run or not telegram.enabled():
                print(telegram.render(deal, verdict, landed, veracidad))
                if deal.image:
                    print(f"   [foto] {deal.image}")
                print("-" * 60)
                continue

            via = telegram.enviar_oferta(deal, verdict, landed, veracidad)
            if via:
                _marcar_avisada(store, deal, hermanas)
                store.sumar_enviada()
                enviados += 1
                formatos[via] = formatos.get(via, 0) + 1
                # Telegram admite ~20 mensajes por minuto en un grupo. Con 20
                # tarjetas por ronda, 3.5s de pausa deja margen de sobra.
                time.sleep(3.5)

        # El resumen recoge lo que no ameritaba interrumpir.
        en_resumen = 0
        if para_resumen and _toca_resumen(store):
            lote = para_resumen[:config.DIGEST_MAX_ITEMS]
            mensaje = telegram.render_resumen(lote)
            if dry_run or not telegram.enabled():
                print(mensaje)
                print("-" * 60)
            elif telegram.send(mensaje):
                for deal, _verdict in lote:
                    _marcar_avisada(store, deal, hermanas)
                store.set_meta("ultimo_resumen", dt.datetime.now(dt.timezone.utc).isoformat())
                en_resumen = len(lote)

        if primera and config.SEED_ON_EMPTY_DB and not dry_run:
            # Se archiva el resto del catalogo como punto de partida: a partir
            # de aqui el bot avisa de cambios, no del inventario que ya existia.
            for deal, _verdict in candidatas:
                _marcar_avisada(store, deal, hermanas)
            store.set_meta("ronda_inicial", dt.datetime.now(dt.timezone.utc).isoformat())
            print(f"Ronda inicial: {len(candidatas)} ofertas archivadas como linea base.")

        if dry_run:
            print("(dry-run: no se envio nada ni se marco como avisada)")
        elif not telegram.enabled():
            print("Sin TELEGRAM_BOT_TOKEN/CHAT_ID: se imprimio en consola.")
        else:
            detalle = ", ".join(f"{n} por {via}" for via, n in sorted(formatos.items()))
            print(f"Enviadas {enviados} alertas ({detalle or 'sin detalle'})"
                  + (f" y {en_resumen} en el resumen." if en_resumen else "."))

        borradas = store.prune()
        if borradas:
            print(f"Historial podado: {borradas} observaciones antiguas.")

    return _resultado(
        ofertas=len(ofertas),
        candidatas=len(candidatas),
        enviadas=enviados,
        resumen=en_resumen,
        duracion=round(time.time() - inicio, 1),
    )


def ejecutar(args: argparse.Namespace) -> int:
    """Envoltura para la linea de comandos."""
    resultado = ejecutar_ronda(args.source, args.dry_run, args.limit, args.top)
    return 1 if resultado["error"] == "falta watchlist.json" else 0


def main() -> int:
    # La consola de Windows usa cp1252 y no puede imprimir los emojis del mensaje.
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    parser = argparse.ArgumentParser(description="Radar de ofertas USA/Colombia")
    parser.add_argument(
        "--source", action="append", choices=FUENTES,
        help="limita la ronda a una fuente (se puede repetir)",
    )
    parser.add_argument("--dry-run", action="store_true", help="muestra en consola sin enviar")
    parser.add_argument("--limit", type=int, help="maximo de alertas de esta ronda")
    parser.add_argument("--top", type=int, metavar="N",
                        help="manda las N rebajas mas grandes de ahora mismo, "
                             "aunque ya se hayan avisado (vistazo manual)")
    parser.add_argument("--test-telegram", action="store_true", help="envia un mensaje de prueba")
    parser.add_argument("--comandos", action="store_true",
                        help="responde los comandos que hayan llegado al bot")
    parser.add_argument("--chat-id", action="store_true",
                        help="muestra los chat_id de los grupos y canales del bot")
    args = parser.parse_args()

    if args.comandos:
        mod_comandos.registrar_menu()
        atender_comandos()
        return 0

    if args.chat_id:
        try:
            bot = telegram.info_bot()
        except Exception as exc:
            print(f"No se pudo consultar Telegram: {exc}")
            return 1

        usuario = bot.get("username", "?")
        print(f"El token del .env pertenece a: @{usuario}  ({bot.get('first_name','')})")
        print("Confirma que ese sea EXACTAMENTE el bot que agregaste al grupo.")
        print("")

        try:
            chats = telegram.descubrir_chats()
        except Exception as exc:
            print(f"No se pudo consultar Telegram: {exc}")
            return 1

        if not chats:
            webhook = telegram.webhook_activo()
            if webhook:
                print(f"Hay un webhook activo ({webhook}) y eso bloquea getUpdates.")
                print("Quitalo abriendo esta URL en el navegador:")
                print(f"  https://api.telegram.org/bot<TU_TOKEN>/deleteWebhook")
                return 1
            print("Telegram no reporta ningun chat todavia.")
            print("")
            print(f"Escribe esto DENTRO del grupo:   /start@{usuario}")
            print("y vuelve a correr el comando. El orden importa: el mensaje")
            print("tiene que ser posterior a haber agregado el bot al grupo.")
            print("")
            print("Si ya lo hiciste y sigue vacio, el bot del grupo no es @" + usuario + ".")
            return 1

        print("Chats visibles para el bot:")
        print("")
        for chat in chats:
            marca = "  <-- grupo" if chat["tipo"] in ("group", "supergroup") else ""
            print(f"  {chat['id']:>16}   {chat['tipo']:<12} {chat['nombre']}{marca}")
        print("")
        print("Copia el numero (con el signo menos) en TELEGRAM_CHAT_ID dentro de .env")
        return 0

    if args.test_telegram:
        if not telegram.enabled():
            print("Faltan TELEGRAM_BOT_TOKEN y/o TELEGRAM_CHAT_ID.")
            return 1
        ok = telegram.send(
            "\u2705 <b>Radar de ofertas conectado</b>\n"
            "Prueba de formato de cupon: <code>PRUEBA20</code> <i>(tocalo para copiarlo)</i>\n\n"
            "📱 <i>El menú interactivo inferior ha sido configurado y está listo para usar.</i>",
            reply_markup=telegram.teclado_tiendas(),
        )
        print("Mensaje enviado." if ok else "Telegram rechazo el mensaje.")
        return 0 if ok else 1

    return ejecutar(args)


if __name__ == "__main__":
    sys.exit(main())
