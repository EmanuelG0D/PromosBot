"""Comandos de Telegram: pedirle al bot ofertas de una tienda a voluntad.

Los comandos llegan por webhook al servidor de Render: Telegram los entrega
apenas se escriben, sin reloj de por medio. `pendientes()` implementa el camino
viejo (preguntar con getUpdates) y sigue sirviendo para probar desde el
portatil, pero Telegram no deja usar los dos a la vez.

Telegram entrega los comandos (los que empiezan con "/") aunque el modo
privacidad este activo, asi que funcionan dentro del grupo sin configurar nada.

El identificador del ultimo mensaje procesado se guarda aparte de radar.db a
proposito: la base del radar la escribe el flujo programado, y dos procesos
escribiendo el mismo archivo terminarian pisandose.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import config
from core import http

ESTADO = Path(config.BASE_DIR / "comandos_estado.json")

# comando -> (fuente, tiendas o None, titulo para el encabezado)
CATALOGO: dict[str, tuple[str, list[str] | None, str]] = {
    "alkosto":  ("algolia_co", ["alkosto"],  "Alkosto"),
    "ktronix":  ("algolia_co", ["ktronix"],  "K-tronix"),
    "exito":    ("vtex",       ["exito"],    "Exito"),
    "carulla":  ("vtex",       ["carulla"],  "Carulla"),
    "olimpica": ("vtex",       ["olimpica"], "Olimpica"),
    "drogueria": ("droguerias", None,        "Droguerias"),
    "exterior": ("slickdeals", None,         "Ofertas del exterior"),
    "colombia": ("co",         None,         "Todo lo colombiano"),
    "todo":     ("*",          None,         "Todas las tiendas"),
}

# Cuantas claves de ofertas ya mostradas se recuerdan. Alcanza para que pedir
# la misma tienda varias veces seguidas traiga cosas distintas, sin dejar que
# el archivo de estado crezca sin control.
MEMORIA_MOSTRADAS = 400

AYUDA = (
    "\U0001F916 <b>Comandos disponibles</b>\n\n"
    "/alkosto - ofertas de Alkosto\n"
    "/ktronix - ofertas de K-tronix\n"
    "/exito - ofertas del Exito\n"
    "/carulla - ofertas de Carulla\n"
    "/olimpica - ofertas de Olimpica\n"
    "/colombia - lo mejor de las tiendas colombianas\n"
    "/drogueria - rebajas de droguerias\n"
    "/exterior - ofertas de EE. UU. (Slickdeals)\n"
    "/todo - Colombia y exterior mezclados\n"
    "Puedes pedir mas: <code>/alkosto 25</code>\n"
    "/ayuda - esta lista\n\n"
    "<i>El bot revisa solo cada 15 minutos, pero los comandos responden al "
    "instante. Una consulta larga tarda lo que tarde en preguntarle a las "
    "tiendas.</i>"
)


def _cargar() -> dict:
    try:
        return json.loads(ESTADO.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _guardar(datos: dict) -> None:
    ESTADO.write_text(json.dumps(datos), encoding="utf-8")


def _leer_estado() -> int:
    try:
        return int(_cargar().get("offset", 0))
    except (TypeError, ValueError):
        return 0


def _guardar_estado(offset: int) -> None:
    datos = _cargar()
    datos["offset"] = offset
    _guardar(datos)


def ya_mostradas() -> set:
    """Ofertas que ya se enviaron respondiendo comandos."""
    return set(_cargar().get("mostradas") or [])


def marcar_mostradas(claves) -> None:
    """Recuerda lo enviado para que la proxima vez traiga cosas distintas."""
    datos = _cargar()
    nuevas = list(claves)
    previas = [c for c in (datos.get("mostradas") or []) if c not in set(nuevas)]
    datos["mostradas"] = (previas + nuevas)[-MEMORIA_MOSTRADAS:]
    _guardar(datos)


def leer_comando(mensaje: dict) -> dict | None:
    """Un mensaje de Telegram vuelto solicitud, o None si no es para el bot.

    La usan las dos vias de entrada: el webhook (un mensaje a la vez, al
    instante) y getUpdates (un lote cada tanto). El formato del mensaje es el
    mismo, cambia solo como llega.
    """
    texto = ((mensaje or {}).get("text") or "").strip()
    chat = ((mensaje or {}).get("chat") or {}).get("id")
    if not texto.startswith("/") or chat is None:
        return None

    # Solo se obedece al chat configurado. Sin esto, cualquiera que encuentre
    # el bot podria ponerlo a trabajar para el, y las respuestas llegarian
    # igual al grupo del dueno.
    if str(chat) != str(config.TELEGRAM_CHAT_ID):
        print(f"  [comandos] ignorado: viene del chat {chat}")
        return None

    # "/alkosto@MiBot 25" -> comando "alkosto", cantidad 25
    partes = texto[1:].split()
    if not partes:
        return None                       # un "/" solo, sin comando
    crudo = re.split(r"@", partes[0], maxsplit=1)[0].lower()
    if not crudo:
        return None

    cantidad = None
    if len(partes) > 1 and partes[1].isdigit():
        cantidad = max(1, min(int(partes[1]), config.COMANDO_MAX_RESULTADOS))
    return {"comando": crudo, "chat_id": chat, "cantidad": cantidad}


def pendientes() -> list[dict]:
    """Comandos nuevos dirigidos al bot, ya confirmados ante Telegram.

    Devuelve [{"comando": "alkosto", "chat_id": ...}, ...]. Confirmar (avanzar
    el offset) es lo que evita responder dos veces lo mismo.
    """
    if not config.TELEGRAM_BOT_TOKEN:
        return []

    offset = _leer_estado()
    url = (f"https://api.telegram.org/bot{config.TELEGRAM_BOT_TOKEN}/getUpdates"
           f"?timeout=0&allowed_updates=%5B%22message%22%5D")
    if offset:
        url += f"&offset={offset}"

    try:
        datos = http.get_json(url, retries=1)
    except Exception as exc:
        print(f"  [comandos] no se pudo consultar Telegram: {exc}")
        return []
    if not datos.get("ok"):
        return []

    encontrados: list[dict] = []
    ultimo = offset
    for update in datos.get("result", []):
        ultimo = max(ultimo, int(update.get("update_id", 0)) + 1)
        solicitud = leer_comando(update.get("message") or {})
        if solicitud:
            encontrados.append(solicitud)

    if ultimo > offset:
        _guardar_estado(ultimo)
    return encontrados


MENU = [
    ("alkosto", "Ofertas de Alkosto"),
    ("ktronix", "Ofertas de K-tronix"),
    ("exito", "Ofertas del Exito"),
    ("carulla", "Ofertas de Carulla"),
    ("olimpica", "Ofertas de Olimpica"),
    ("colombia", "Lo mejor de las tiendas colombianas"),
    ("drogueria", "Rebajas de droguerias"),
    ("exterior", "Ofertas de EE. UU."),
    ("todo", "Colombia y exterior mezclados"),
    ("ayuda", "Lista de comandos"),
]


def registrar_menu() -> bool:
    """Publica los comandos para que Telegram los sugiera al escribir "/"."""
    if not config.TELEGRAM_BOT_TOKEN:
        return False
    url = f"https://api.telegram.org/bot{config.TELEGRAM_BOT_TOKEN}/setMyCommands"
    cuerpo = {"commands": [{"command": c, "description": d} for c, d in MENU]}
    try:
        return bool(http.post_json(url, cuerpo, retries=1).get("ok"))
    except Exception as exc:
        print(f"  [comandos] no se pudo registrar el menu: {exc}")
        return False
