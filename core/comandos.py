"""Comandos de Telegram: pedirle al bot ofertas de una tienda a voluntad.

El bot no vive prendido: corre por reloj en GitHub Actions. Por eso no recibe
los mensajes al instante, sino que cada tanto le pregunta a Telegram si llego
algo (getUpdates). La espera es de pocos minutos, no inmediata.

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
    "exterior": ("slickdeals", None,         "Ofertas del exterior"),
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
    "/exterior - ofertas de EE. UU. (Slickdeals)\n"
    "/todo - lo mejor de todas las tiendas\n"
    "/objetivos - tus topes de precio configurados\n"
    "/estado - cuantas alertas van hoy\n"
    "/ayuda - esta lista\n\n"
    "<i>El bot revisa solo cada 15 minutos. Los comandos tardan unos minutos "
    "en responder porque no esta encendido todo el tiempo.</i>"
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
        mensaje = update.get("message") or {}
        texto = (mensaje.get("text") or "").strip()
        chat = (mensaje.get("chat") or {}).get("id")
        if not texto.startswith("/") or chat is None:
            continue

        # "/alkosto@MiBot argumento" -> "alkosto"
        crudo = re.split(r"[\s@]", texto[1:], maxsplit=1)[0].lower()
        if crudo:
            encontrados.append({"comando": crudo, "chat_id": chat})

    if ultimo > offset:
        _guardar_estado(ultimo)
    return encontrados


MENU = [
    ("alkosto", "Ofertas de Alkosto"),
    ("ktronix", "Ofertas de K-tronix"),
    ("exito", "Ofertas del Exito"),
    ("carulla", "Ofertas de Carulla"),
    ("olimpica", "Ofertas de Olimpica"),
    ("exterior", "Ofertas de EE. UU."),
    ("todo", "Lo mejor de todas las tiendas"),
    ("objetivos", "Tus topes de precio"),
    ("estado", "Alertas enviadas hoy"),
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
