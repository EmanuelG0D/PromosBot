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
from core import facebook, filtros, fx, respaldo, telegram, whitelist
from core import comandos as mod_comandos
from core import objetivos as mod_objetivos
from core import veracidad as mod_veracidad
from core.landed import calcular
from core.models import Deal
from core.scoring import SIN_PRECIO_DE_LISTA, Verdict, evaluar
from core.store import Store
from sources import algolia_co, falabella, promocajita, mercadolibre, slickdeals, vtex, koaj, promohunter, miloderrocha, republica, descuentostech

FUENTES = ("slickdeals", "promocajita", "vtex", "algolia_co", "falabella",
           "mercadolibre", "koaj", "promohunter", "miloderrocha", "republica", "descuentostech")

# Los colores no distinguen productos: solo variantes del mismo modelo.
COLORES = {
    "negro", "negra", "blanco", "blanca", "azul", "rojo", "roja", "gris",
    "verde", "rosado", "rosa", "plateado", "plata", "dorado", "oro", "morado",
    "amarillo", "beige", "cafe", "marron", "naranja", "violeta", "turquesa",
    "black", "white", "blue", "red", "gray", "grey", "green", "pink",
    "silver", "gold", "purple", "yellow", "orange", "brown",
}

_SALUD_FUENTES: dict[str, dict] = {}
_TELEMETRIA_LIGAS: dict[str, Any] = {
    "glitches_hoy": 0,
    "ultimo_top3_avg_score": 0.0,
    "menciones_hoy": 0,
    "fecha_hoy": "",
}


def _actualizar_telemetria_dia() -> None:
    hoy = dt.date.today().isoformat()
    if _TELEMETRIA_LIGAS["fecha_hoy"] != hoy:
        _TELEMETRIA_LIGAS["glitches_hoy"] = 0
        _TELEMETRIA_LIGAS["menciones_hoy"] = 0
        _TELEMETRIA_LIGAS["fecha_hoy"] = hoy


def _recolectar_fuente(clave: str, fetcher, *args, **kwargs) -> list[Deal]:
    """Ejecuta la recoleccion de una fuente aislando excepciones y midiendo duracion y cantidad."""
    t0 = time.time()
    try:
        resultado = fetcher(*args, **kwargs) or []
        dur = round(time.time() - t0, 1)
        _SALUD_FUENTES[clave] = {
            "ts": time.time(),
            "ok": True,
            "ofertas": len(resultado),
            "duracion": dur,
            "error": None,
        }
        return resultado
    except Exception as exc:
        dur = round(time.time() - t0, 1)
        _SALUD_FUENTES[clave] = {
            "ts": time.time(),
            "ok": False,
            "ofertas": 0,
            "duracion": dur,
            "error": str(exc),
        }
        print(f"  [recolectar] error en fuente '{clave}': {exc}")
        return []


def recolectar(watchlist: dict, activas: list[str]) -> list[Deal]:
    ofertas: list[Deal] = []

    if "slickdeals" in activas:
        cfg = watchlist.get("slickdeals", {})
        consultas = cfg.get("queries", [])
        if consultas:
            print(f"-> Slickdeals: {len(consultas)} busquedas")
            ofertas += _sin_ruido(
                _recolectar_fuente(
                    "slickdeals",
                    slickdeals.fetch,
                    consultas,
                    cfg.get("por_consulta", 12),
                    cfg.get("excluir"),
                    cfg.get("incluir"),
                ),
                cfg,
            )

    if "vtex" in activas:
        cfg = watchlist.get("vtex", {})
        consultas = cfg.get("queries", [])
        if consultas:
            print(f"-> VTEX ({', '.join(cfg.get('tiendas', vtex.TIENDAS))}): {len(consultas)} busquedas")
            ofertas += _sin_ruido(
                _recolectar_fuente(
                    "vtex",
                    vtex.fetch,
                    consultas,
                    cfg.get("tiendas"),
                    cfg.get("por_consulta", 24),
                    cfg.get("marcas"),
                ),
                cfg,
            )

    if "algolia_co" in activas:
        cfg = watchlist.get("algolia_co", {})
        consultas = cfg.get("queries", [])
        if consultas:
            print(f"-> Alkosto/K-tronix: {len(consultas)} busquedas")
            ofertas += _sin_ruido(
                _recolectar_fuente(
                    "algolia_co",
                    algolia_co.fetch,
                    consultas,
                    cfg.get("tiendas"),
                    cfg.get("por_consulta", 60),
                ),
                cfg,
            )

    if "promocajita" in activas:
        cfg = watchlist.get("promocajita", {})
        if cfg.get("canales"):
            print("-> PROMOCAJITA: canal de Telegram")
            ofertas += _recolectar_fuente(
                "promocajita",
                promocajita.fetch,
                cfg.get("canales"),
                cfg.get("por_canal", 20),
                cfg.get("incluir"),
                cfg.get("excluir"),
            )

    if "falabella" in activas:
        cfg = watchlist.get("falabella", {})
        consultas = cfg.get("queries", [])
        if consultas:
            print(f"-> Falabella/Homecenter: {len(consultas)} busquedas")
            ofertas += _sin_ruido(
                _recolectar_fuente(
                    "falabella",
                    falabella.fetch,
                    consultas,
                    cfg.get("tiendas"),
                    cfg.get("por_consulta", 30),
                    cfg.get("marcas"),
                ),
                cfg,
            )


    if "mercadolibre" in activas:
        cfg = watchlist.get("mercadolibre", {})
        consultas = cfg.get("queries", [])
        if consultas:
            print(f"-> Mercado Libre: {len(consultas)} busquedas")
            ofertas += _sin_ruido(
                _recolectar_fuente(
                    "mercadolibre",
                    mercadolibre.fetch,
                    consultas,
                    por_consulta=cfg.get("por_consulta", 48),
                ),
                cfg,
            )
        else:
            print("-> Mercado Libre: ofertas destacadas")
            ofertas += _sin_ruido(
                _recolectar_fuente(
                    "mercadolibre",
                    mercadolibre.fetch,
                    por_consulta=cfg.get("por_consulta", 48),
                ),
                cfg,
            )


    if "koaj" in activas:
        cfg = watchlist.get("koaj", {})
        consultas = cfg.get("queries", [])
        print(f"-> Koaj Colombia: {len(consultas)} busquedas + outlets")
        ofertas += _sin_ruido(
            _recolectar_fuente(
                "koaj",
                koaj.fetch,
                consultas,
                incluir_outlet=cfg.get("incluir_outlet", True),
                por_consulta=cfg.get("por_consulta", 24),
            ),
            cfg,
        )

    if "promohunter" in activas:
        cfg = watchlist.get("promohunter", {})
        paginas = cfg.get("paginas", 2)
        print(f"-> El Promo Hunter: {paginas} páginas web")
        ofertas += _sin_ruido(
            _recolectar_fuente(
                "promohunter",
                promohunter.fetch,
                paginas=paginas,
                incluir=cfg.get("incluir"),
                excluir=cfg.get("excluir"),
            ),
            cfg,
        )

    if "miloderrocha" in activas:
        cfg = watchlist.get("miloderrocha", {})
        por_canal = cfg.get("por_canal", 20)
        print(f"-> Milo Derrocha: {por_canal} ofertas de Telegram")
        ofertas += _sin_ruido(
            _recolectar_fuente(
                "miloderrocha",
                miloderrocha.fetch,
                por_canal=por_canal,
                incluir=cfg.get("incluir"),
                excluir=cfg.get("excluir"),
            ),
            cfg,
        )

    if "republica" in activas:
        cfg = watchlist.get("republica", {})
        por_canal = cfg.get("por_canal", 20)
        print(f"-> República de Descuentos: {por_canal} ofertas de Telegram")
        ofertas += _sin_ruido(
            _recolectar_fuente(
                "republica",
                republica.fetch,
                por_canal=por_canal,
                incluir=cfg.get("incluir"),
                excluir=cfg.get("excluir"),
            ),
            cfg,
        )

    if "descuentostech" in activas:
        cfg = watchlist.get("descuentostech", {})
        canales = cfg.get("canales", ["DescuentosTech"])
        por_canal = cfg.get("por_canal", 20)
        print(f"-> Descuentos Tech ({', '.join(canales)}): {por_canal} ofertas de Telegram")
        ofertas += _sin_ruido(
            _recolectar_fuente(
                "descuentostech",
                descuentostech.fetch,
                canales=canales,
                por_canal=por_canal,
                incluir=cfg.get("incluir"),
                excluir=cfg.get("excluir"),
            ),
            cfg,
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
                          ("slickdeals", en_dolares)):
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

    if fuente == "promohunter":
        resultado = promohunter.fetch(paginas=cfg.get("paginas", 2), incluir=cfg.get("incluir"), excluir=cfg.get("excluir"))
        _guardar_en_cache(clave, resultado)
        return resultado

    if fuente == "miloderrocha":
        resultado = miloderrocha.fetch(por_canal=cfg.get("por_canal", 20), incluir=cfg.get("incluir"), excluir=cfg.get("excluir"))
        _guardar_en_cache(clave, resultado)
        return resultado

    if fuente == "republica":
        resultado = republica.fetch(por_canal=cfg.get("por_canal", 20), incluir=cfg.get("incluir"), excluir=cfg.get("excluir"))
        _guardar_en_cache(clave, resultado)
        return resultado

    if fuente == "descuentostech":
        resultado = descuentostech.fetch(canales=cfg.get("canales", ["DescuentosTech"]),
                                         por_canal=cfg.get("por_canal", 20),
                                         incluir=cfg.get("incluir"),
                                         excluir=cfg.get("excluir"))
        _guardar_en_cache(clave, resultado)
        return resultado

    if fuente in ("amazon", "amazon_gangas"):
        cfg_ph = watchlist.get("promohunter", {})
        cfg_milo = watchlist.get("miloderrocha", {})
        deals_ph = promohunter.fetch(paginas=cfg_ph.get("paginas", 2), incluir=cfg_ph.get("incluir"), excluir=cfg_ph.get("excluir"))
        deals_milo = miloderrocha.fetch(por_canal=cfg_milo.get("por_canal", 20), incluir=cfg_milo.get("incluir"), excluir=cfg_milo.get("excluir"))
        amazon_deals = [
            d for d in (deals_ph + deals_milo)
            if (d.store and "amazon" in d.store.lower()) or ("amazon" in d.url.lower())
        ]
        if consultas_custom:
            palabras = [c.strip().lower() for c in consultas_custom if len(c.strip()) >= 2]
            amazon_deals = [
                d for d in amazon_deals
                if any(p in d.title.lower() for p in palabras)
            ]
        resultado = _sin_ruido(amazon_deals, cfg)
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

    elif fuente == "slickdeals":
        crudas = slickdeals.fetch(consultas, cfg.get("por_consulta", 12),
                                  cfg.get("excluir"), cfg.get("incluir"))
    elif fuente == "koaj":
        crudas = koaj.fetch(consultas, incluir_outlet=cfg.get("incluir_outlet", True),
                            por_consulta=cfg.get("por_consulta", 24))
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

    # 17. Freidoras de aire y microondas (calibrado para incluir freidoras de 3L a 5L en descuento)
    (("freidora", "freidoras", "air fryer", "airfryer", "freidora de aire", "microondas"), 320000.0),

    # 18. Pequeños electrodomésticos y cocina
    (("licuadora", "licuadoras", "cafetera", "cafeteras", "sanduchera", "sandwichera",
      "waflera", "batidora", "procesador de alimentos", "arrocera", "arroceras",
      "olla", "ollas", "sarten", "sartenes", "bateria de cocina",
      "aspiradora", "aspiradoras", "robot aspiradora", "ventilador", "ventiladores",
      "taladro", "taladros", "herramientas", "destornillador"), 250000.0),
]


def _precio_admisible(deal: Deal, trm: float = 4000.0) -> bool:
    """Verifica que el precio este dentro del tope especifico de su categoria o tope general."""
    if deal.price is None:
        return True

    # Cazadores de comunidad (PromoHunter, Milo Derrocha, Promocajita, República, DescuentosTech) ya vienen curados
    # y quedan exentos de topes para no bloquear gangas reales de alto valor.
    if deal.source in {"promohunter", "miloderrocha", "promocajita", "republica", "descuentostech"}:
        return True

    precio_cop = deal.price if deal.currency == "COP" else (deal.price * trm)

    # Regla específica para Nike Colombia: solo calzado y hasta $220.000 COP
    if deal.store == "Nike":
        kw_calzado = ("tenis", "zapatos", "zapatillas", "sneakers", "botas", "sandalias", "calzado", "guayos")
        es_calzado = any(filtros.menciona(deal.title, kw) for kw in kw_calzado)
        return es_calzado and precio_cop <= 220000.0

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
    "taladro", "taladros", "herramientas", "destornillador",
    "colchon", "colchones", "cama", "camas", "almohada", "almohadas",
    "sabana", "sabanas", "edredon", "mueble", "muebles", "sofa", "sofas",
    "silla", "sillas", "mesa", "mesas", "comedor"
}


def detectar_errores_precio(oferta: Deal) -> bool:
    """Detecta si una oferta es un 'Glitch' o 'Error de Precio' matemático (Fase 0).

    Condiciones:
    - Glitch Tecnológico/Pesado: (descuento_porcentaje >= 65) AND ((precio_original - precio_final) >= 300000)
    - Glitch General: (descuento_porcentaje >= 85) AND (precio_final >= 20000)
    """
    desc = getattr(oferta, "discount_verificable", 0.0) or 0.0
    p_final = getattr(oferta, "price", 0.0) or 0.0
    p_orig = getattr(oferta, "list_price", 0.0) or p_final

    if getattr(oferta, "currency", "COP") == "USD":
        ahorro_usd = max(0.0, p_orig - p_final)
        glitch_pesado = (desc >= 65.0) and (ahorro_usd >= 75.0)
        glitch_general = (desc >= 85.0) and (p_final >= 5.0)
        return bool(glitch_pesado or glitch_general)

    ahorro = max(0.0, p_orig - p_final)
    glitch_tecnologico_pesado = (desc >= 65.0) and (ahorro >= 300000.0)
    glitch_general = (desc >= 85.0) and (p_final >= 20000.0)
    return bool(glitch_tecnologico_pesado or glitch_general)


PESOS_FAMILIA: dict[str, float] = {
    "computadores_y_hardware": 1.5,
    "tv_y_monitores": 1.5,
    "smartphones": 1.5,
    "perifericos": 1.2,
    "refrigeracion": 1.4,
    "pequenos_electro": 1.1,
    "ropa_basica": 0.8,
    "otros": 1.0,
}


def calcular_score_deal(deal: Deal) -> float:
    """Calcula el score de relevancia ponderado por categoría de un deal."""
    desc = getattr(deal, "discount_verificable", 0.0) or 0.0
    if deal.source in {"republica", "promocajita", "slickdeals", "miloderrocha", "descuentostech"}:
        piso = 55.0 if deal.coupons else 45.0
        desc = max(desc, piso)
    fam = filtros.asignar_familia(deal.title)
    peso = PESOS_FAMILIA.get(fam, 1.0)
    return round(desc * peso, 2)


def torneo_familias(candidatas: list[tuple[Deal, Verdict]]) -> list[tuple[Deal, Verdict, float]]:
    """Ejecuta el Sistema de Torneo y Score Ponderado por Familias (Fase 2).

    1. Toma las ofertas válidas y las agrupa por 'familia'.
    2. De cada familia conserva ÚNICAMENTE la oferta con mayor descuento_porcentaje (El Campeón).
    3. Calcula score_relevancia = descuento_porcentaje * PESOS_FAMILIA.get(familia, 1.0).
    4. Ordena la lista final usando ORDER BY score_relevancia DESC.
    """
    if not candidatas:
        return []

    por_fam: dict[str, list[tuple[Deal, Verdict]]] = {}
    for par in candidatas:
        fam = filtros.asignar_familia(par[0].title)
        por_fam.setdefault(fam, []).append(par)

    campeones: list[tuple[Deal, Verdict, float]] = []
    for fam, grupo in por_fam.items():
        if not grupo:
            continue
        campeon_par = max(
            grupo,
            key=lambda p: getattr(p[0], "discount_verificable", 0.0) or 0.0
        )
        deal, verdict = campeon_par
        score = calcular_score_deal(deal)
        campeones.append((deal, verdict, score))

    campeones.sort(key=lambda c: c[2], reverse=True)
    return campeones


def seleccionar_por_tiendas(candidatas: list[tuple[Deal, Verdict]],
                            max_por_tienda: int = 1,
                            tope_ronda: int | None = None) -> list[tuple[Deal, Verdict]]:
    """Selecciona ofertas garantizando que la competencia sea interna por tienda.

    1. Agrupa las candidatas por tienda (o fuente si no tiene tienda asignada).
    2. En cada tienda, ordena sus ofertas por score y toma hasta `max_por_tienda` (la campeona de esa tienda).
    3. Si una tienda no tiene ofertas válidas que superen el filtro, aporta 0 ofertas.
    4. Reúne las campeonas de cada tienda y las ordena por score global descendente.
    5. Aplica `tope_ronda` si se especificó, garantizando que nunca se sature el canal.
    """
    if not candidatas:
        return []

    por_tienda: dict[str, list[tuple[Deal, Verdict, float]]] = {}
    for par in candidatas:
        deal, verdict = par
        t_nombre = (deal.store or deal.source or "tienda").strip().lower()
        score = calcular_score_deal(deal)
        por_tienda.setdefault(t_nombre, []).append((deal, verdict, score))

    campeones_tiendas: list[tuple[Deal, Verdict, float]] = []
    cupo_tienda = max(1, max_por_tienda)

    for t_nombre, grupo in por_tienda.items():
        # Ordenar dentro de la tienda por score ponderado descendente
        grupo_ordenado = sorted(grupo, key=lambda x: x[2], reverse=True)
        campeones_tiendas.extend(grupo_ordenado[:cupo_tienda])

    # Ordenar los campeones de cada tienda por score global para priorizar lo mejor del país
    campeones_tiendas.sort(key=lambda x: x[2], reverse=True)

    if tope_ronda is not None and tope_ronda > 0:
        campeones_tiendas = campeones_tiendas[:tope_ronda]

    return [(item[0], item[1]) for item in campeones_tiendas]


def _clasificar_departamento(title: str) -> str:
    """Clasifica un producto en un rubro principal para equilibrar la variedad de ofertas."""
    fam = filtros.asignar_familia(title)
    if fam in ("tv_y_monitores", "smartphones", "perifericos", "computadores_y_hardware"):
        return "tecnologia"
    if fam in ("refrigeracion", "pequenos_electro"):
        return "cocina_electro"
    if fam == "ropa_basica":
        return "moda_calzado"

    t_norm = filtros._normalizar(title)
    if any(filtros.menciona(t_norm, kw) for kw in DEPTO_COCINA | DEPTO_LINEA_BLANCA):
        return "cocina_electro"
    if any(filtros.menciona(t_norm, kw) for kw in DEPTO_ROPA):
        return "moda_calzado"
    if any(filtros.menciona(t_norm, kw) for kw in DEPTO_TECNOLOGIA):
        return "tecnologia"
    if any(filtros.menciona(t_norm, kw) for kw in DEPTO_HOGAR):
        return "hogar"
    return "otros"



def _seleccionar_diversificadas(candidatas: list[tuple[Deal, Verdict]],
                               tope: int,
                               max_por_tienda: int = 1) -> list[tuple[Deal, Verdict]]:
    """Selecciona ofertas garantizando variedad de departamento y de tienda.

    Evita que un solo departamento (ej. audífonos o accesorios) o una sola tienda
    acaparen todos los cupos de la ronda nacional.
    """
    if not candidatas or tope <= 0:
        return []

    deptos_orden = ["cocina_electro", "tecnologia", "moda_calzado", "hogar", "otros"]
    por_depto: dict[str, list[tuple[Deal, Verdict]]] = {d: [] for d in deptos_orden}

    for par in candidatas:
        d = _clasificar_departamento(par[0].title)
        por_depto.setdefault(d, []).append(par)

    seleccionadas: list[tuple[Deal, Verdict]] = []
    conteo_tiendas: dict[str, int] = {}
    vistos_keys: set[str] = set()

    def _puede_agregar(deal: Deal, estricto_tienda: bool = True) -> bool:
        if deal.key in vistos_keys:
            return False
        t_nombre = (deal.store or deal.source).lower().strip()
        if estricto_tienda and max_por_tienda > 0:
            if conteo_tiendas.get(t_nombre, 0) >= max_por_tienda:
                return False
        return True

    def _agregar(par: tuple[Deal, Verdict]) -> None:
        deal = par[0]
        t_nombre = (deal.store or deal.source).lower().strip()
        seleccionadas.append(par)
        vistos_keys.add(deal.key)
        conteo_tiendas[t_nombre] = conteo_tiendas.get(t_nombre, 0) + 1

    # Pase 1: 1 de cada departamento principal respetando max_por_tienda
    for depto in deptos_orden:
        if len(seleccionadas) >= tope:
            break
        for par in por_depto[depto]:
            if _puede_agregar(par[0], estricto_tienda=True):
                _agregar(par)
                break

    # Pase 2: si faltan cupos, tomar las siguientes mejores respetando max_por_tienda
    if len(seleccionadas) < tope:
        for par in candidatas:
            if len(seleccionadas) >= tope:
                break
            if _puede_agregar(par[0], estricto_tienda=True):
                _agregar(par)

    # Pase 3: si aún faltan cupos (ej. solo 1 tienda tenía ofertas), flexibilizar tienda
    if len(seleccionadas) < tope:
        for par in candidatas:
            if len(seleccionadas) >= tope:
                break
            if _puede_agregar(par[0], estricto_tienda=False):
                _agregar(par)

    return seleccionadas


def _tiendas_por_departamento(consultas_custom: list[str] | None) -> tuple[list[str], list[str], list[str]]:
    """Selecciona solo los almacenes relevantes según el rubro de la búsqueda para evitar peticiones inútiles."""
    algolia_todas = ["alkosto", "ktronix", "alkomprar"]
    vtex_todas = [
        "exito", "carulla", "olimpica", "jumbo", "haceb", "whirlpool", "imusa", "oster",
        "arturocalle", "totto", "studiof", "velez", "americanino", "nike"
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
            ["exito", "jumbo", "totto", "arturocalle", "studiof", "velez", "americanino", "nike"],
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
                      max_por_tienda: int = 3,
                      solo_nuevas: bool = False) -> list:
    """Las mejores ofertas de tiendas de Todo Colombia, garantizando el Top 3 por tienda en comparaciones."""
    ofertas: list[Deal] = []

    tiendas_algolia, tiendas_vtex, tiendas_falabella = _tiendas_por_departamento(consultas_custom)

    if tiendas_algolia:
        ofertas += _ofertas_de(watchlist, "algolia_co", tiendas_algolia, consultas_custom=consultas_custom)

    if tiendas_vtex:
        ofertas += _ofertas_de(watchlist, "vtex", tiendas_vtex, consultas_custom=consultas_custom)

    if tiendas_falabella:
        ofertas += _ofertas_de(watchlist, "falabella", tiendas_falabella, consultas_custom=consultas_custom)

    if not consultas_custom or any(any(p in DEPTO_ROPA for p in filtros._normalizar(c).split()) for c in (consultas_custom or [])):
        ofertas += _ofertas_de(watchlist, "koaj", None, consultas_custom=consultas_custom)

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
            if solo_nuevas:
                balanceadas.extend(nuevas_t[:max_por_tienda])
            else:
                balanceadas.extend((nuevas_t + repetidas_t)[:max_por_tienda])

        limite = max(cuantas, len(balanceadas))
        seleccion = balanceadas[:limite]
    else:
        nuevas = [par for par in unicas if par[0].key not in vistas]
        repetidas = [par for par in unicas if par[0].key in vistas]
        if solo_nuevas:
            seleccion = nuevas[:cuantas]
        else:
            seleccion = (nuevas + repetidas)[:cuantas]

    for deal, verdict in seleccion:
        if deal.key in vistas:
            verdict.etiquetas.append("ya te la habia mostrado")
    return seleccion


def _mejores(watchlist: dict, fuentes: list[str], tiendas, vistas: set,
             cuantas: int, consultas_custom: list[str] | None = None,
             trm: float = 4000.0,
             solo_nuevas: bool = False) -> list:
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
        # Slickdeals ya filtra en origen (es_calzado_o_ropa_de_marca): aplicar el filtro
        # local encima descartaria ofertas validas cuyo titulo no repite el termino exacto
        # (ej: "Nike Air Force 1" al buscar "shoes"). Para el resto de fuentes si aplica.
        if not all(f == "slickdeals" for f in fuentes):
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
    if solo_nuevas:
        seleccion = nuevas[:cuantas]
    else:
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


def generar_panel_salud() -> str:
    """Construye un informe visual detallado del estado del radar exclusivo para el administrador."""
    zona_co = dt.timezone(dt.timedelta(hours=-5))
    ahora = dt.datetime.now(zona_co)
    ahora_str = ahora.strftime("%I:%M %p").lstrip("0")

    with Store() as store:
        enviadas_hoy = store.enviadas_hoy()
        try:
            archivadas = store.conn.execute("SELECT COUNT(*) FROM alerts").fetchone()[0]
        except Exception:
            archivadas = 0

    lineas = [
        "🩺 <b>Panel de Diagnóstico y Salud — PromosBot</b>",
        "",
        f"🕒 <b>Hora actual:</b> {ahora_str} (Colombia)",
        f"🚨 <b>Alertas enviadas hoy:</b> <b>{enviadas_hoy}</b> de {config.MAX_ALERTS_PER_DAY}",
        f"⚡ <b>Errores de Precio (Glitches) hoy:</b> <b>{_TELEMETRIA_LIGAS.get('glitches_hoy', 0)}</b>",
        f"🏆 <b>Promedio Score Top 3:</b> <b>{_TELEMETRIA_LIGAS.get('ultimo_top3_avg_score', 0.0)} pts</b>",
        f"📜 <b>Menciones honoríficas enviadas:</b> <b>{_TELEMETRIA_LIGAS.get('menciones_hoy', 0)}</b>",
        f"💾 <b>Gangas registradas en BD:</b> <b>{archivadas}</b>",
        "",
        "🏬 <b>Estado de Scrapers y Tiendas:</b>",
    ]

    nombres_fuentes = [
        ("algolia_co", "Alkosto / K-tronix"),
        ("vtex", "Éxito / Carulla / Jumbo"),
        ("falabella", "Falabella / Homecenter"),
        ("mercadolibre", "Mercado Libre"),
        ("koaj", "Koaj Colombia"),
        ("promohunter", "El Promo Hunter"),
        ("miloderrocha", "Milo Derrocha"),
        ("republica", "República de Descuentos"),
        ("promocajita", "PromoCajita"),
        ("slickdeals", "Slickdeals (Moda/Calzado)"),
        ("descuentostech", "Descuentos Tech"),
    ]

    ahora_ts = time.time()
    for clave, etiqueta in nombres_fuentes:
        info = _SALUD_FUENTES.get(clave)
        if not info:
            lineas.append(f"• ⚪ <b>{etiqueta}:</b> Esperando próxima ronda")
            continue

        minutos_atras = int((ahora_ts - info["ts"]) / 60)
        tiempo_str = f"hace {minutos_atras}m" if minutos_atras > 0 else "hace un momento"

        if info["ok"]:
            conteo = info["ofertas"]
            dur = info["duracion"]
            lineas.append(f"• 🟢 <b>{etiqueta}:</b> OK ({tiempo_str}) — {conteo} items ({dur}s)")
        else:
            err = info.get("error") or "error desconocido"
            err_corto = telegram.esc(str(err)[:45])
            lineas.append(f"• 🔴 <b>{etiqueta}:</b> ⚠️ FALLÓ ({tiempo_str}) — <i>{err_corto}</i>")

    lineas.append("")
    lineas.append("⚙️ <b>Ritmos Automáticos:</b>")
    lineas.append("• ⚡ Comunidad: cada 15 min (máx 2 alertas)")
    lineas.append("• 🏬 Catálogos: cada 60 min (máx 3 alertas diversificadas)")
    lineas.append("• 🛡️ Anti-Monopolio: máx 1 alerta por tienda en cada ronda")

    return "\n".join(lineas)


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
        comando = solicitud.get("comando", "")
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

        if tipo == "siguientes":
            ultima = mod_comandos.obtener_ultima_busqueda(chat_id)
            if not ultima:
                telegram.send(
                    "No tienes una búsqueda previa para continuar. Selecciona una opción en el menú inferior:",
                    reply_markup=telegram.teclado_tiendas(),
                    chat_id=chat_id,
                )
                atendidos += 1
                continue
            solicitud = dict(ultima)
            solicitud["chat_id"] = chat_id
            solicitud["es_siguiente"] = True
            comando = solicitud.get("comando")
            tipo = solicitud.get("tipo")

        if tipo == "unirse_canal":
            nombre = solicitud.get("nombre", "Usuario")
            enlace = (getattr(config, "TELEGRAM_CHANNEL_URL", "") or "https://t.me/RadarPromoCol").strip()
            if not enlace or "+GE1nQO-f0HYwNGQx" in enlace or "joinchat" in enlace or "/+" in enlace:
                enlace = "https://t.me/RadarPromoCol"

            # Ocultar cualquier teclado de tiendas previo que pudiera persistir en la pantalla del usuario
            telegram.send(
                "🔒 <b>Acceso exclusivo para miembros</b>\n"
                "<i>El menú de tiendas y la consulta de ofertas están reservados para miembros del canal oficial.</i>",
                reply_markup={"remove_keyboard": True},
                chat_id=chat_id,
            )

            telegram.send(
                f"👋 <b>¡Hola, {telegram.esc(nombre)}!</b>\n\n"
                f"Para poder usar <b>PromosBot</b> y consultar todas las ofertas, "
                f"primero debes estar unido a nuestro canal oficial:\n\n"
                f"📢 <b>Promociones Colombia🇨🇴</b>\n"
                f"👉 <a href=\"{enlace}\">{enlace}</a>\n\n"
                f"<i>Únete con el enlace o el botón de abajo y luego presiona 'Ya me uní':</i>",
                reply_markup=telegram.teclado_unirse_canal(enlace),
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

        if tipo == "start_deal":
            deal_hash = solicitud.get("deal_hash", "")
            telegram.accion_escribiendo(chat_id=chat_id)
            with Store() as store:
                deal_guardado = store.obtener_deal_reciente(deal_hash)

            if deal_guardado:
                landed = None
                if deal_guardado.country == "US" and deal_guardado.price:
                    trm_val, _ = fx.get_trm(None)
                    landed = calcular(deal_guardado.price, trm_val, deal_guardado.weight_lb)
                verdict = Verdict(True, "guardada desde el canal", inmediata=True)
                telegram.enviar_oferta(deal_guardado, verdict, landed, chat_id=chat_id,
                                       reply_markup={"remove_keyboard": True})

                # Si el usuario llegó desde Facebook u otro enlace externo y aún no está en el canal,
                # le enviamos la invitación para que se suscriba al canal de ofertas
                user_id = solicitud.get("user_id")
                if user_id and not telegram.es_miembro_del_canal(user_id):
                    enlace = (getattr(config, "TELEGRAM_CHANNEL_URL", "") or "https://t.me/RadarPromoCol").strip()
                    if not enlace or "+GE1nQO-f0HYwNGQx" in enlace or "joinchat" in enlace or "/+" in enlace:
                        enlace = "https://t.me/RadarPromoCol"
                    telegram.send(
                        "📢 <b>¿Te gustó esta oferta?</b>\n\n"
                        "Únete a nuestro canal oficial para no perderte las mejores gangas y errores de precio en Colombia antes de que se agoten:\n"
                        f"👉 <a href=\"{enlace}\">{enlace}</a>",
                        reply_markup=telegram.teclado_unirse_canal(enlace),
                        chat_id=chat_id,
                    )
            else:
                telegram.send(
                    "⚠️ <i>Esta oferta ya expiró o no está disponible.</i>",
                    reply_markup={"remove_keyboard": True},
                    chat_id=chat_id,
                )
            atendidos += 1
            continue

        if tipo == "iniciar_reporte":
            deal_hash = solicitud.get("deal_hash")
            deal_info = None
            if deal_hash:
                with Store() as store:
                    deal_rec = store.obtener_deal_reciente(deal_hash)
                    if deal_rec:
                        deal_info = {
                            "hash": deal_hash,
                            "title": deal_rec.title,
                            "store": deal_rec.store,
                            "url": deal_rec.url,
                            "price": deal_rec.price,
                            "currency": deal_rec.currency,
                        }

            mod_comandos.fijar_estado_feedback(chat_id, {
                "ts": time.time(),
                "deal": deal_info,
            })

            if deal_info:
                texto_prompt = (
                    f"✍️ <b>Reportar problema con una oferta</b>\n\n"
                    f"📦 <b>Producto:</b> {telegram.esc(deal_info['title'])}\n"
                    f"🏪 <b>Tienda:</b> {telegram.esc(deal_info['store'])}\n\n"
                    f"A continuación escribe el problema que encontraste "
                    f"(ej: <i>precio incorrecto, enlace roto, producto agotado</i>, etc.):"
                )
            else:
                texto_prompt = (
                    f"✍️ <b>Buzón de Sugerencias y Reportes</b>\n\n"
                    f"Nos ayuda mucho saber cómo mejorar PromosBot durante esta fase de pruebas.\n\n"
                    f"A continuación escribe tu mensaje, sugerencia o problema que hayas encontrado:"
                )

            telegram.send(
                texto_prompt,
                reply_markup=telegram.boton_cancelar_feedback(),
                chat_id=chat_id,
            )
            atendidos += 1
            continue

        if tipo == "cancelar_feedback":
            mod_comandos.limpiar_estado_feedback(chat_id)
            telegram.send(
                "❌ <i>Operación cancelada. Puedes seguir explorando ofertas en el menú:</i>",
                reply_markup=telegram.teclado_tiendas(),
                chat_id=chat_id,
            )
            atendidos += 1
            continue

        if tipo == "enviar_feedback":
            texto_fb = solicitud.get("texto_feedback", "").strip()
            estado_fb = solicitud.get("estado_feedback") or {}
            deal_info = estado_fb.get("deal")

            mod_comandos.limpiar_estado_feedback(chat_id)

            nombre_u = solicitud.get("nombre") or "Usuario"
            user_u = solicitud.get("username")
            handle_u = f" (@{user_u})" if user_u else ""
            uid_u = solicitud.get("user_id") or chat_id

            telegram.send(
                "✅ <b>¡Muchas gracias! Tu reporte ha sido enviado.</b>\n\n"
                "El equipo lo revisará pronto para seguir mejorando el bot. "
                "Puedes continuar explorando ofertas:",
                reply_markup=telegram.teclado_tiendas(),
                chat_id=chat_id,
            )

            admin_id = getattr(config, "TELEGRAM_ADMIN_ID", None) or getattr(whitelist, "ADMIN_ID_POR_DEFECTO", 5583002220)
            if admin_id:
                if deal_info:
                    lineas_admin = [
                        "🚨 <b>Nuevo Reporte de Oferta</b>",
                        "",
                        f"👤 <b>Usuario:</b> {telegram.esc(nombre_u)}{telegram.esc(handle_u)} (<code>{uid_u}</code>)",
                        f"🏪 <b>Tienda:</b> {telegram.esc(deal_info.get('store', 'N/A'))}",
                        f"📦 <b>Producto:</b> {telegram.esc(deal_info.get('title', 'N/A'))}",
                        f"🔗 <b>Enlace:</b> <a href=\"{telegram.esc(deal_info.get('url', ''))}\">Ver oferta</a>",
                        "",
                        f"📝 <b>Mensaje del usuario:</b>",
                        f"<i>{telegram.esc(texto_fb)}</i>",
                    ]
                else:
                    lineas_admin = [
                        "📬 <b>Nueva Sugerencia / Reporte General</b>",
                        "",
                        f"👤 <b>Usuario:</b> {telegram.esc(nombre_u)}{telegram.esc(handle_u)} (<code>{uid_u}</code>)",
                        "",
                        f"📝 <b>Mensaje:</b>",
                        f"<i>{telegram.esc(texto_fb)}</i>",
                    ]
                telegram.send(
                    telegram.NL.join(lineas_admin),
                    chat_id=admin_id,
                )

            atendidos += 1
            continue

        if comando in ("ayuda", "help"):
            mod_comandos.limpiar_estado_feedback(chat_id)
            telegram.send(mod_comandos.AYUDA, reply_markup=telegram.teclado_tiendas(), chat_id=chat_id)
            atendidos += 1
            continue

        if comando in ("start", "menu") or tipo == "menu":
            mod_comandos.limpiar_estado_feedback(chat_id)
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

        if comando in ("salud", "diagnostico", "status_admin") or tipo == "salud":
            user_id = solicitud.get("user_id")
            if not whitelist.es_admin(user_id):
                telegram.send("⛔ Este comando es exclusivo para el administrador de PromosBot.", chat_id=chat_id)
                atendidos += 1
                continue
            texto_salud = generar_panel_salud()
            telegram.send(texto_salud, reply_markup=telegram.teclado_tiendas(), chat_id=chat_id)
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
            mod_comandos.limpiar_estado_feedback(chat_id)
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
            if not solicitud.get("es_siguiente"):
                mod_comandos.guardar_ultima_busqueda(chat_id, solicitud)
            categoria_nombre = solicitud.get("categoria_nombre", "Categoría")
            tienda = mod_comandos.tienda_activa(chat_id=chat_id)
            if tienda not in mod_comandos.CATALOGO and tienda not in ("objetivos", "estado"):
                tienda = "colombia"

            fuente, tiendas, titulo_tienda = mod_comandos.CATALOGO.get(
                tienda, ("co", None, "Colombia"))

            # Notificar de inmediato al usuario que se inicio la revision si no se envio antes
            if not solicitud.get("notificado"):
                telegram.accion_escribiendo(chat_id=chat_id)
                if solicitud.get("es_siguiente"):
                    telegram.send(f"🔄 <i>Buscando siguientes ofertas de <b>{telegram.esc(categoria_nombre)}</b>...</i>", chat_id=chat_id)
                elif fuente == "co":
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

            solo_nuevas = bool(solicitud.get("es_siguiente"))
            if fuente == "co":
                # Consulta las tiendas autorizadas garantizando Top 3 por tienda
                seleccion = _mejores_colombia(watchlist, vistas, cuantas, consultas_custom=consultas, trm=trm or 4000.0, solo_nuevas=solo_nuevas)
            elif fuente == "*":
                del_exterior = max(cuantas // 5, 1)
                seleccion = (_mejores(watchlist, ["algolia_co", "vtex", "falabella"],
                                      None, vistas, cuantas - del_exterior, consultas_custom=consultas, trm=trm or 4000.0, solo_nuevas=solo_nuevas)
                             + _mejores(watchlist, ["slickdeals"], None, vistas, del_exterior, consultas_custom=consultas, trm=trm or 4000.0, solo_nuevas=solo_nuevas))
            else:
                seleccion = _mejores(watchlist, [fuente], tiendas, vistas, cuantas,
                                     consultas_custom=consultas, trm=trm or 4000.0, solo_nuevas=solo_nuevas)

            if token_actual != _token_busqueda_activa:
                print("  [radar] categoria cancelada por nueva solicitud")
                atendidos += 1
                continue

            if not seleccion:
                if solicitud.get("es_siguiente"):
                    telegram.send(
                        f"🏁 <b>¡Ya te mostré todas las ofertas destacadas</b> de {telegram.esc(categoria_nombre)} en este momento!\n\n"
                        f"Puedes explorar otra categoría o tienda en los botones abajo.",
                        reply_markup=telegram.teclado_categorias(titulo_tienda),
                        chat_id=chat_id,
                    )
                else:
                    telegram.send(
                        f"Ahora mismo no encontré rebajas destacadas en {telegram.esc(categoria_nombre)} para <b>{telegram.esc(titulo_tienda)}</b>.",
                        reply_markup=telegram.teclado_categorias(titulo_tienda),
                        chat_id=chat_id,
                    )
                atendidos += 1
                continue

            if fuente == "co":
                encabezado = (f"🇨🇴 <b>Comparador Nacional · {telegram.esc(categoria_nombre)}</b>\n"
                              f"<i>{'Siguientes' if solo_nuevas else 'Top 3'} mejores rebajas de cada tienda en Colombia:</i>")
            else:
                encabezado = (f"🏬 <b>{telegram.esc(titulo_tienda)}</b> · {telegram.esc(categoria_nombre)}\n"
                              f"<i>{'Siguientes' if solo_nuevas else 'Mejores'} rebajas encontradas ahora mismo:</i>")

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
                           f"de {telegram.esc(categoria_nombre)} en Colombia.\n"
                           f"<i>Presiona abajo para ver siguientes ofertas:</i>")
            else:
                msg_fin = (f"🏁 <b>Búsqueda finalizada</b> · Se enviaron las <b>{len(enviadas_ahora)}</b> mejores ofertas "
                           f"de {telegram.esc(categoria_nombre)} en {telegram.esc(titulo_tienda)}.\n"
                           f"<i>Presiona abajo para ver siguientes ofertas:</i>")

            telegram.send(
                msg_fin,
                reply_markup=telegram.boton_siguientes_ofertas(),
                chat_id=chat_id,
            )
            atendidos += 1
            continue

        if tipo == "todo_tienda":
            token_actual = _token_busqueda_activa
            if not solicitud.get("es_siguiente"):
                mod_comandos.guardar_ultima_busqueda(chat_id, solicitud)
            tienda = mod_comandos.tienda_activa(chat_id=chat_id)
            if tienda not in mod_comandos.CATALOGO:
                tienda = "colombia"
            fuente, tiendas, titulo = mod_comandos.CATALOGO[tienda]

            # Notificar de inmediato al usuario si no se envio antes
            if not solicitud.get("notificado"):
                telegram.accion_escribiendo(chat_id=chat_id)
                if solicitud.get("es_siguiente"):
                    telegram.send(f"🔄 <i>Buscando siguientes ofertas en <b>{telegram.esc(titulo)}</b>...</i>", chat_id=chat_id)
                else:
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

            solo_nuevas = bool(solicitud.get("es_siguiente"))
            if fuente == "co":
                # Consulta las 6 tiendas autorizadas: algolia_co, vtex, falabella
                seleccion = _mejores_colombia(watchlist, vistas, cuantas, trm=trm or 4000.0, solo_nuevas=solo_nuevas)
            elif fuente == "*":
                del_exterior = max(cuantas // 5, 1)
                seleccion = (_mejores(watchlist, ["algolia_co", "vtex", "falabella"],
                                      None, vistas, cuantas - del_exterior, trm=trm or 4000.0, solo_nuevas=solo_nuevas)
                             + _mejores(watchlist, ["slickdeals"], None, vistas, del_exterior, trm=trm or 4000.0, solo_nuevas=solo_nuevas))
            else:
                seleccion = _mejores(watchlist, [fuente], tiendas, vistas, cuantas, trm=trm or 4000.0, solo_nuevas=solo_nuevas)

            if token_actual != _token_busqueda_activa:
                print("  [radar] todo_tienda cancelado por nueva solicitud")
                atendidos += 1
                continue

            teclado_salida = (telegram.teclado_tiendas() if tienda in ("cajita", "amazon")
                              else telegram.teclado_categorias(titulo))
            if not seleccion:
                if solicitud.get("es_siguiente"):
                    telegram.send(
                        f"🏁 <b>¡Ya te mostré todas las ofertas disponibles</b> en {telegram.esc(titulo)} en este momento!\n\n"
                        f"Puedes explorar otra tienda en el menú inferior.",
                        reply_markup=teclado_salida,
                        chat_id=chat_id,
                    )
                else:
                    telegram.send(f"Ahora mismo no encuentro rebajas en {telegram.esc(titulo)}.",
                                  reply_markup=teclado_salida,
                                  chat_id=chat_id)
                atendidos += 1
                continue

            telegram.send(f"🏬 <b>{telegram.esc(titulo)}</b> — {'siguientes ofertas' if solo_nuevas else 'lo mejor de ahora mismo'}",
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
                f"🏁 <b>Búsqueda finalizada</b> · Se enviaron las <b>{len(enviadas_ahora)}</b> mejores ofertas de {telegram.esc(titulo)}.\n"
                f"<i>Presiona abajo para ver siguientes ofertas:</i>",
                reply_markup=telegram.boton_siguientes_ofertas(),
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
        if not solicitud.get("es_siguiente"):
            mod_comandos.guardar_ultima_busqueda(chat_id, solicitud)
        mod_comandos.fijar_tienda_activa(comando, chat_id=chat_id)
        fuente, tiendas, titulo = mod_comandos.CATALOGO[comando]

        # Notificar de inmediato al usuario si no se envio antes
        if not solicitud.get("notificado"):
            telegram.accion_escribiendo(chat_id=chat_id)
            if solicitud.get("es_siguiente"):
                telegram.send(f"🔄 <i>Buscando siguientes ofertas en <b>{telegram.esc(titulo)}</b>...</i>", chat_id=chat_id)
            else:
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

        solo_nuevas = bool(solicitud.get("es_siguiente"))
        if fuente == "co":
            # Consulta las 6 tiendas autorizadas: algolia_co, vtex, falabella
            seleccion = _mejores_colombia(watchlist, vistas, cuantas, trm=trm or 4000.0, solo_nuevas=solo_nuevas)
        elif fuente == "amazon_gangas":
            seleccion = _mejores(watchlist, ["promohunter", "miloderrocha"], None, vistas, cuantas, trm=trm or 4000.0, solo_nuevas=solo_nuevas)
        elif fuente == "*":
            # Mezcla deliberada: las de Colombia se ordenan por descuento, pero
            # las del exterior no tienen porcentaje y nunca ganarian ese orden,
            # asi que se les reserva un cupo.
            del_exterior = max(cuantas // 5, 1)
            seleccion = (_mejores(watchlist, ["algolia_co", "vtex", "falabella"],
                                  None, vistas, cuantas - del_exterior, trm=trm or 4000.0, solo_nuevas=solo_nuevas)
                          + _mejores(watchlist, ["slickdeals"], None, vistas, del_exterior, trm=trm or 4000.0, solo_nuevas=solo_nuevas))
        else:
            seleccion = _mejores(watchlist, [fuente], tiendas, vistas, cuantas, trm=trm or 4000.0, solo_nuevas=solo_nuevas)

        if token_actual != _token_busqueda_activa:
            print("  [radar] comando cancelado por nueva solicitud")
            atendidos += 1
            continue

        teclado_salida = (telegram.teclado_tiendas() if comando in ("cajita", "amazon")
                          else telegram.teclado_categorias(titulo))
        if not seleccion:
            if solicitud.get("es_siguiente"):
                telegram.send(f"🏁 <b>¡Ya te mostré todas las ofertas disponibles</b> en {telegram.esc(titulo)} en este momento!",
                              reply_markup=teclado_salida,
                              chat_id=chat_id)
            else:
                telegram.send(f"Ahora mismo no encuentro rebajas en {telegram.esc(titulo)}.",
                              reply_markup=teclado_salida,
                              chat_id=chat_id)
            atendidos += 1
            continue

        telegram.send(f"🏬 <b>{telegram.esc(titulo)}</b> — "
                      f"{'siguientes ofertas' if solo_nuevas else 'lo mejor de ahora mismo'}",
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
            f"<i>Presiona abajo para ver siguientes ofertas:</i>",
            reply_markup=telegram.boton_siguientes_ofertas(),
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
            if store.enviadas_hoy() >= int(config.MAX_ALERTS_PER_DAY * 1.5) and not objetivos_cfg:
                print(f"Tope diario alcanzado con amplio margen ({store.enviadas_hoy()}/{config.MAX_ALERTS_PER_DAY}); ronda omitida.")
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
            es_cazador = deal.source in {"promohunter", "miloderrocha", "promocajita", "republica", "descuentostech"}
            objetivo = None if es_cazador else mod_objetivos.alcanzado(deal, objetivos_cfg)
            if not es_cazador and not objetivo and not _precio_admisible(deal, trm):
                continue
            if deal.country == "US" and not getattr(deal, "free_shipping_co", False):
                if filtros.es_pesado_para_casillero(deal.title):
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

        # FASE 0: Válvula de Escape / Fast-Track para Errores de Precio
        fasttrack_glitches: list[tuple[Deal, Verdict]] = []
        candidatas_ordinarias: list[tuple[Deal, Verdict]] = []

        for par in candidatas:
            deal, verdict = par
            if detectar_errores_precio(deal):
                deal.es_glitch = True
                verdict.glitch = True
                fasttrack_glitches.append(par)
            else:
                candidatas_ordinarias.append(par)

        # FASE 2: Selección por Tiendas (Competencia interna, cupos por tienda)
        fuentes_comunidad = {"slickdeals", "promocajita", "promohunter", "miloderrocha", "republica", "descuentostech"}
        es_ronda_comunidad = set(activas).issubset(fuentes_comunidad)

        if limite is not None:
            tope_efectivo = limite
        elif es_ronda_comunidad:
            tope_efectivo = getattr(config, "MAX_ALERTS_COMUNIDAD", 3)
        else:
            tope_efectivo = getattr(config, "MAX_ALERTS_CATALOGOS", 6)

        max_tienda = getattr(config, "MAX_ALERTS_PER_STORE_RUN", 1)

        if top:
            campeones_ordenados = torneo_familias(candidatas_ordinarias)
            seleccion = [(c[0], c[1]) for c in campeones_ordenados[:top]]
        else:
            seleccion = seleccionar_por_tiendas(
                candidatas_ordinarias,
                max_por_tienda=max_tienda,
                tope_ronda=tope_efectivo,
            )

        _actualizar_telemetria_dia()
        if seleccion:
            scores_sel = [calcular_score_deal(par[0]) for par in seleccion[:3]]
            _TELEMETRIA_LIGAS["ultimo_top3_avg_score"] = round(sum(scores_sel) / len(scores_sel), 1)

        menciones = []

        # Las de Fast-Track se anteponen para enviarse sin topes ni límites
        seleccion = fasttrack_glitches + seleccion

        print(f"{len(candidatas)} candidatas ({colapsadas} variantes colapsadas): "
              f"{len(inmediatas)} inmediatas (envio {len(seleccion)}), "
              f"{len(para_resumen)} al resumen"
              + (f"; {falsas} rebajas falsas descartadas" if falsas else ""))
        print("")

        enviados = 0
        formatos: dict = {}

        for deal, verdict in seleccion:
            veracidad = veracidades.get(deal.key)

            # Oferta 'intocable' (error de precio / super ganga / objetivo de precio cumplido):
            # Se salta los limites ordinarios para nunca perder un oferton real
            es_objetivo = bool(any("objetivo" in str(et).lower() for et in verdict.etiquetas))
            es_glitch = bool(verdict.glitch)
            es_super_ganga = (
                (deal.discount_verificable >= getattr(config, "SUPER_DEAL_DISCOUNT_PCT", 60.0) and verdict.confianza != "baja")
                or (veracidad is not None and getattr(veracidad, "es_real", False) and getattr(veracidad, "descuento_real", 0.0) >= 25.0)
                or (deal.vence_pronto and deal.discount_verificable >= 50.0)
                or (bool(deal.coupons) and deal.source in {"promohunter", "miloderrocha", "promocajita", "slickdeals", "republica", "descuentostech"})
                or (deal.discount_verificable >= 50.0 and deal.source in {"promohunter", "miloderrocha", "promocajita", "slickdeals", "republica", "descuentostech"})
            )
            es_intocable = es_glitch or es_objetivo or es_super_ganga

            if not dry_run and not top and not es_intocable:
                # 1. Tope diario global
                if store.enviadas_hoy() >= config.MAX_ALERTS_PER_DAY:
                    print(f"  [radar] '{deal.title[:35]}...' omitida: tope diario global alcanzado ({config.MAX_ALERTS_PER_DAY}).")
                    continue

                # 2. Tope diario por fuente
                lim_fuente = getattr(config, "MAX_ALERTS_PER_SOURCE_DAY", 15)
                if store.enviadas_hoy_fuente(deal.source) >= lim_fuente:
                    print(f"  [radar] '{deal.title[:35]}...' omitida: tope diario de fuente {deal.source} alcanzado ({lim_fuente}).")
                    continue

                # 3. Tope diario por tienda de destino
                lim_tienda = getattr(config, "MAX_ALERTS_PER_STORE_DAY", 15)
                if store.enviadas_hoy_tienda(deal.store) >= lim_tienda:
                    print(f"  [radar] '{deal.title[:35]}...' omitida: tope diario de tienda {deal.store} alcanzado ({lim_tienda}).")
                    continue

            landed = None
            if deal.country == "US" and deal.price:
                landed = calcular(deal.price, trm, deal.weight_lb)

            if dry_run or not telegram.enabled():
                try:
                    print(telegram.render(deal, verdict, landed, veracidad))
                except UnicodeEncodeError:
                    print(telegram.render(deal, verdict, landed, veracidad).encode("ascii", "replace").decode("ascii"))
                if deal.image:
                    print(f"   [foto] {deal.image}")
                print("-" * 60)
                continue

            via = telegram.enviar_oferta(deal, verdict, landed, veracidad)
            if via:
                _marcar_avisada(store, deal, hermanas)
                store.guardar_deal_reciente(deal)
                store.sumar_enviada(deal)
                enviados += 1
                if getattr(deal, "es_glitch", False) or verdict.glitch:
                    _TELEMETRIA_LIGAS["glitches_hoy"] += 1
                formatos[via] = formatos.get(via, 0) + 1

                # Difusión en Página de Facebook: se encolan para publicación dosificada (lotes cada 45 min)
                if facebook.configurado():
                    facebook.encolar_oferta(deal, verdict, landed, veracidad)
                    # Historias de Facebook: si es ganga de alto impacto y hay cupo diario (máx 2 al día)
                    if facebook.es_ganga_para_historia(deal, verdict) and store.facebook_puede_publicar_historia():
                        try:
                            facebook.publicar_historia(deal, verdict, store=store)
                        except Exception as exc_hist:
                            print(f"  [facebook] aviso: error subiendo historia ({exc_hist})")

                # Telegram admite ~20 mensajes por minuto en un grupo. Con 20
                # tarjetas por ronda, 3.5s de pausa deja margen de sobra.
                time.sleep(3.5)

        # FASE 3: Menciones honoríficas agrupadas
        en_menciones = 0
        if menciones:
            if dry_run or not telegram.enabled():
                print(telegram.render_menciones_honorificas(menciones))
                print("-" * 60)
                en_menciones = len(menciones)
            else:
                if telegram.enviar_menciones_honorificas(menciones):
                    _TELEMETRIA_LIGAS["menciones_hoy"] += len(menciones)
                    en_menciones = len(menciones)
                    for m_deal, _ in menciones:
                        _marcar_avisada(store, m_deal, hermanas)
                        store.guardar_deal_reciente(m_deal)
                        store.sumar_enviada(m_deal)


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

        if dry_run:
            print("(dry-run: no se envio nada ni se marco como avisada)")
        elif not telegram.enabled():
            print("Sin TELEGRAM_BOT_TOKEN/CHAT_ID: se imprimio en consola.")
        else:
            detalle = ", ".join(f"{n} por {via}" for via, n in sorted(formatos.items()))
            print(f"Enviadas {enviados} alertas ({detalle or 'sin detalle'})"
                  + (f", {en_menciones} menciones honoríficas" if en_menciones else "")
                  + (f" y {en_resumen} en el resumen." if en_resumen else "."))

        borradas = store.prune()
        if borradas:
            print(f"Historial podado: {borradas} observaciones antiguas.")

        if (enviados > 0 or en_resumen > 0 or en_menciones > 0) and not dry_run:
            if respaldo.guardar():
                print("  [radar] historial respaldado inmediatamente en GitHub")

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
