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
from core import fx, telegram
from core import comandos as mod_comandos
from core import objetivos as mod_objetivos
from core import veracidad as mod_veracidad
from core.landed import calcular
from core.models import Deal
from core.scoring import Verdict, evaluar
from core.store import Store
from sources import algolia_co, ebay, mercadolibre, slickdeals, vtex

FUENTES = ("slickdeals", "vtex", "algolia_co", "mercadolibre", "ebay")

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
            ofertas += slickdeals.fetch(consultas, cfg.get("por_consulta", 12),
                                        cfg.get("excluir"), cfg.get("incluir"))

    if "vtex" in activas:
        cfg = watchlist.get("vtex", {})
        consultas = cfg.get("queries", [])
        if consultas:
            print(f"-> VTEX ({', '.join(cfg.get('tiendas', vtex.TIENDAS))}): {len(consultas)} busquedas")
            ofertas += vtex.fetch(consultas, cfg.get("tiendas"), cfg.get("por_consulta", 24))

    if "algolia_co" in activas:
        cfg = watchlist.get("algolia_co", {})
        consultas = cfg.get("queries", [])
        if consultas:
            print(f"-> Alkosto/K-tronix: {len(consultas)} busquedas")
            ofertas += algolia_co.fetch(consultas, cfg.get("tiendas"), cfg.get("por_consulta", 60))

    if "mercadolibre" in activas:
        cfg = watchlist.get("mercadolibre", {})
        consultas = cfg.get("queries", [])
        if consultas:
            print(f"-> Mercado Libre: {len(consultas)} busquedas")
            ofertas += mercadolibre.fetch(consultas, cfg.get("por_consulta", 50),
                                          cfg.get("solo_tienda_oficial", False))

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


def _clave_variante(deal: Deal) -> tuple:
    """Agrupa variantes del mismo producto (mismo modelo en otro color)."""
    limpio = unicodedata.normalize("NFKD", deal.title.lower())
    limpio = "".join(c for c in limpio if not unicodedata.combining(c))
    palabras = [p for p in re.findall(r"[a-z0-9]+", limpio) if p not in COLORES][:8]
    # Sin la tienda en la clave: Alkosto/K-tronix y Exito/Carulla son hermanas
    # y publican el mismo producto al mismo precio.
    return (deal.price, " ".join(palabras))


def _colapsar_variantes(candidatas):
    """Deja una sola alerta por producto para no quemar el cupo en colores.

    Devuelve (unicas, hermanas). Las hermanas hay que marcarlas como avisadas
    junto con su representante: si no, el mismo producto en otro color vuelve a
    aparecer como novedad en la ronda siguiente.
    """
    vistas: dict = {}
    hermanas: dict = {}
    unicas = []
    for par in candidatas:
        clave = _clave_variante(par[0])
        previo = vistas.get(clave)
        if previo is not None:
            # Misma oferta en la tienda hermana: se anota en vez de repetirse.
            otra = f"Tambien en {par[0].store}"
            if par[0].store != previo.store and otra not in previo.notes:
                previo.notes.append(otra)
            hermanas[previo.key].append(par[0])
            continue
        vistas[clave] = par[0]
        hermanas[par[0].key] = []
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


def _ofertas_de(watchlist: dict, fuente: str, tiendas) -> list[Deal]:
    """Trae las ofertas de una sola fuente, opcionalmente de tiendas concretas."""
    cfg = watchlist.get(fuente, {})
    consultas = cfg.get("queries", [])
    if not consultas:
        return []
    if fuente == "algolia_co":
        return algolia_co.fetch(consultas, tiendas, cfg.get("por_consulta", 60))
    if fuente == "vtex":
        return vtex.fetch(consultas, tiendas, cfg.get("por_consulta", 24))
    if fuente == "slickdeals":
        return slickdeals.fetch(consultas, cfg.get("por_consulta", 12),
                                cfg.get("excluir"), cfg.get("incluir"))
    return []


def _mejores(watchlist: dict, fuentes: list[str], tiendas, vistas: set,
             cuantas: int) -> list:
    """Las mejores ofertas de esas fuentes, priorizando las que no has visto."""
    ofertas: list[Deal] = []
    for fuente in fuentes:
        ofertas += _ofertas_de(watchlist, fuente, tiendas)
    ofertas = _sin_repetidas(ofertas)

    disponibles = [d for d in ofertas if d.in_stock]
    if fuentes == ["slickdeals"]:
        # Slickdeals no publica precio de lista: no hay porcentaje que ordenar,
        # pero el feed ya viene ordenado por los votos de la comunidad.
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


def atender_comandos(por_comando: int = 5) -> dict:
    """Responde los comandos que hayan llegado al bot.

    No toca radar.db: una consulta a voluntad no debe alterar lo que el flujo
    programado considera "ya avisado".
    """
    solicitudes = mod_comandos.pendientes()
    if not solicitudes:
        print("Sin comandos nuevos.")
        return _resultado()

    watchlist = config.load_watchlist()
    trm, _origen = fx.get_trm(None)
    atendidos = 0

    # Todo lo que ya viste: lo que alerto el radar programado (se lee sin
    # escribir) y lo que ya se envio respondiendo comandos.
    vistas = mod_comandos.ya_mostradas()
    try:
        with Store() as store:
            vistas.update(f["key"] for f in store.conn.execute("SELECT key FROM alerts"))
    except Exception as exc:
        print(f"  [comandos] sin historial del radar: {exc}")

    for solicitud in solicitudes:
        comando = solicitud["comando"]
        print(f"-> comando /{comando}")

        if comando in ("ayuda", "help", "start"):
            telegram.send(mod_comandos.AYUDA)
            atendidos += 1
            continue

        if comando == "objetivos":
            lineas = ["🎯 <b>Tus objetivos de precio</b>", ""]
            for objetivo in watchlist.get("objetivos", []):
                tope = objetivo.get("max_cop")
                valor = (telegram.money(tope, "COP") if tope
                         else telegram.money(objetivo.get("max_usd"), "USD"))
                lineas.append(f"• {telegram.esc(objetivo['termino'])} "
                              f"por debajo de <b>{valor}</b>")
            telegram.send(telegram.NL.join(lineas))
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
                f"{telegram.NL}Ofertas en memoria: <b>{archivadas}</b>")
            atendidos += 1
            continue

        if comando not in mod_comandos.CATALOGO:
            telegram.send(f"No conozco /{telegram.esc(comando)}. Escribe /ayuda.")
            continue

        fuente, tiendas, titulo = mod_comandos.CATALOGO[comando]
        if fuente == "co":
            seleccion = _mejores(watchlist, ["algolia_co", "vtex"], None,
                                 vistas, por_comando)
        elif fuente == "*":
            # Mezcla deliberada: las de Colombia se ordenan por descuento, pero
            # las del exterior no tienen porcentaje y nunca ganarian ese orden,
            # asi que se les reserva un cupo.
            seleccion = (_mejores(watchlist, ["algolia_co", "vtex"], None, vistas,
                                  max(por_comando - 1, 1))
                         + _mejores(watchlist, ["slickdeals"], None, vistas, 1))
        else:
            seleccion = _mejores(watchlist, [fuente], tiendas, vistas, por_comando)

        if not seleccion:
            telegram.send(f"Ahora mismo no encuentro rebajas en {telegram.esc(titulo)}.")
            continue

        telegram.send(f"🏬 <b>{telegram.esc(titulo)}</b> — "
                      f"lo mejor de ahora mismo")
        enviadas_ahora = []
        for deal, verdict in seleccion:
            landed = calcular(deal.price, trm, deal.weight_lb) if deal.country == "US" else None
            telegram.enviar_oferta(deal, verdict, landed)
            enviadas_ahora.append(deal.key)
            time.sleep(3.5)
        mod_comandos.marcar_mostradas(enviadas_ahora)
        vistas.update(enviadas_ahora)
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
    inicio = time.time()
    ofertas = _sin_repetidas(recolectar(watchlist, activas))
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
            "Prueba de formato de cupon: <code>PRUEBA20</code> <i>(tocalo para copiarlo)</i>"
        )
        print("Mensaje enviado." if ok else "Telegram rechazo el mensaje.")
        return 0 if ok else 1

    return ejecutar(args)


if __name__ == "__main__":
    sys.exit(main())
