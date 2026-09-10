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
    "falabella":  ("falabella", ["falabella"],  "Falabella"),
    "homecenter": ("falabella", ["homecenter"], "Homecenter"),
    "drogueria": ("droguerias", None,        "Droguerias"),
    "cajita":   ("promocajita", None,        "PROMOCAJITA"),
    "mercadolibre": ("mercadolibre", None,   "Mercado Libre"),
    "meli":     ("mercadolibre", None,       "Mercado Libre"),
    "exterior": ("slickdeals", None,         "Ofertas del exterior"),
    "colombia": ("co",         None,         "Todo lo colombiano"),
    "todo":     ("*",          None,         "Todas las tiendas"),
}

# Mapeo de botones del menu principal a claves de tienda
BOTONES_TIENDA: dict[str, tuple[str, str]] = {
    "🟡 éxito": ("exito", "Éxito"),
    "🟡 exito": ("exito", "Éxito"),
    "🔴 alkosto": ("alkosto", "Alkosto"),
    "💛 mercado libre": ("mercadolibre", "Mercado Libre"),
    "💛 mercadolibre": ("mercadolibre", "Mercado Libre"),
    "mercado libre": ("mercadolibre", "Mercado Libre"),
    "mercadolibre": ("mercadolibre", "Mercado Libre"),
    "meli": ("mercadolibre", "Mercado Libre"),
    "🟢 falabella": ("falabella", "Falabella"),
    "⚪ k-tronix": ("ktronix", "K-tronix"),
    "⚪ ktronix": ("ktronix", "K-tronix"),
    "🔵 olímpica": ("olimpica", "Olímpica"),
    "🔵 olimpica": ("olimpica", "Olímpica"),
    "🟢 carulla": ("carulla", "Carulla"),
    "carulla": ("carulla", "Carulla"),
    "caruya": ("carulla", "Carulla"),
    "🟠 homecenter": ("homecenter", "Homecenter"),
    "homecenter": ("homecenter", "Homecenter"),
    "📦 promocajita": ("cajita", "PROMOCAJITA"),
    "promocajita": ("cajita", "PROMOCAJITA"),
    "📦 cajita": ("cajita", "PROMOCAJITA"),
    "cajita": ("cajita", "PROMOCAJITA"),
    "🇨🇴 todo colombia": ("colombia", "Todo Colombia"),
    "🇺🇸 exterior (ee. uu.)": ("exterior", "Exterior (EE. UU.)"),
    "🇺🇸 exterior": ("exterior", "Exterior (EE. UU.)"),
    "🎯 mis objetivos": ("objetivos", "Mis Objetivos"),
}

# Consultas especificas al tocar cada categoria en tiendas de Colombia
CATEGORIAS_BUSQUEDA: dict[str, list[str]] = {
    "📺 Smart TV": ["televisor", "smart tv"],
    "💻 Portátiles": ["portatil", "laptop", "computador"],
    "🖥️ Monitores": ["monitor"],
    "📱 Celulares": ["celular", "smartphone", "iphone"],
    "👟 Zapatos y Tenis": ["tenis", "zapatillas", "sneakers", "botas", "zapatos"],
    "🎧 Audio y Diademas": ["audifonos", "diadema", "parlante", "audio"],
    "❄️ Electrodomésticos": [
        "nevera",
        "lavadora",
        "electrodomesticos",
        "freidora de aire",
        "microondas",
        "cafetera",
        "aspiradora",
        "licuadora",
    ],
    "👕 Ropa y Moda": [
        "camiseta",
        "pantalon",
        "chaqueta",
        "billetera",
        "bolso",
        "sudadera",
        "jean",
    ],
}

# Consultas especificas al tocar cada categoria en tiendas de EE. UU. (Slickdeals)
CATEGORIAS_BUSQUEDA_EN: dict[str, list[str]] = {
    "📺 Smart TV": ["tv", "oled tv", "smart tv"],
    "💻 Portátiles": ["laptop", "macbook", "notebook"],
    "🖥️ Monitores": ["monitor", "gaming monitor"],
    "📱 Celulares": ["phone", "smartphone", "iphone"],
    "👟 Zapatos y Tenis": ["sneakers", "shoes", "running shoes"],
    "🎧 Audio y Diademas": ["headphones", "earbuds", "speaker"],
    "❄️ Electrodomésticos": ["air fryer", "vacuum", "coffee maker", "appliance"],
    "👕 Ropa y Moda": ["clothing", "hoodie", "jacket", "jeans"],
}

# Cuantas claves de ofertas ya mostradas se recuerdan. Alcanza para que pedir
# la misma tienda varias veces seguidas traiga cosas distintas, sin dejar que
# el archivo de estado crezca sin control.
MEMORIA_MOSTRADAS = 400

AYUDA = (
    "\U0001F916 <b>Comandos y Menú interactivo</b>\n\n"
    "/menu - Abre el menú de botones interactivo\n"
    "/alkosto - ofertas de Alkosto\n"
    "/ktronix - ofertas de K-tronix\n"
    "/exito - ofertas del Exito\n"
    "/carulla - ofertas de Carulla\n"
    "/olimpica - ofertas de Olimpica\n"
    "/falabella - ofertas de Falabella\n"
    "/homecenter - ofertas de Homecenter\n"
    "/mercadolibre - ofertas de Mercado Libre\n"
    "/colombia - lo mejor de las tiendas colombianas\n"
    "/drogueria - rebajas de droguerias\n"
    "/cajita - lo que publica PROMOCAJITA\n"
    "/exterior - ofertas de EE. UU. (Slickdeals)\n"
    "/todo - Colombia y exterior mezclados\n"
    "Puedes pedir mas: <code>/alkosto 25</code>\n"
    "/ayuda - esta lista\n\n"
    "<i>También puedes navegar tocando los botones del menú inferior.</i>"
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


def tienda_activa() -> str:
    """Tienda seleccionada actualmente en el menu de navegacion."""
    return str(_cargar().get("tienda_activa") or "colombia")


def fijar_tienda_activa(tienda: str) -> None:
    """Guarda la tienda activa para que los botones de categoria sepan a quien consultar."""
    datos = _cargar()
    datos["tienda_activa"] = tienda
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

    Soporta comandos tradicionales con '/' y toques en los botones del menu interactivo.
    """
    texto = ((mensaje or {}).get("text") or "").strip()
    chat = ((mensaje or {}).get("chat") or {}).get("id")
    if not texto or chat is None:
        return None

    # Solo se obedece al chat configurado. Sin esto, cualquiera que encuentre
    # el bot podria ponerlo a trabajar para el, y las respuestas llegarian
    # igual al grupo del dueno.
    if str(chat) != str(config.TELEGRAM_CHAT_ID):
        print(f"  [comandos] ignorado: viene del chat {chat}")
        return None

    # 1. Comandos tradicionales con "/"
    if texto.startswith("/"):
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

    # 2. Botones del teclado interactivo (sin "/")
    texto_norm = texto.lower()

    # Volver a la lista de tiendas
    if "volver a tiendas" in texto_norm or "volver" in texto_norm:
        return {"comando": "menu", "chat_id": chat, "cantidad": None, "tipo": "menu"}

    # Boton de tienda
    for btn_key, (tienda_key, tienda_nombre) in BOTONES_TIENDA.items():
        if texto_norm == btn_key:
            if tienda_key == "objetivos":
                return {"comando": "objetivos", "chat_id": chat, "cantidad": None}
            return {
                "comando": "elegir_tienda",
                "tienda": tienda_key,
                "tienda_nombre": tienda_nombre,
                "chat_id": chat,
                "cantidad": None,
                "tipo": "elegir_tienda",
            }

    # Boton '🌟 TODO'
    if "todo" in texto_norm:
        return {
            "comando": "todo_tienda",
            "chat_id": chat,
            "cantidad": None,
            "tipo": "todo_tienda",
        }

    # Boton de categoria
    for cat_label, queries in CATEGORIAS_BUSQUEDA.items():
        if texto_norm == cat_label.lower():
            return {
                "comando": "categoria",
                "categoria_nombre": cat_label,
                "consultas": queries,
                "chat_id": chat,
                "cantidad": None,
                "tipo": "categoria",
            }
        sin_emoji = cat_label.split(" ", 1)[-1].lower()
        if texto_norm == sin_emoji:
            return {
                "comando": "categoria",
                "categoria_nombre": cat_label,
                "consultas": queries,
                "chat_id": chat,
                "cantidad": None,
                "tipo": "categoria",
            }

    return None


def pendientes(timeout: int = 0) -> list[dict]:
    """Comandos nuevos dirigidos al bot, ya confirmados ante Telegram.

    Devuelve [{"comando": "alkosto", "chat_id": ...}, ...]. Confirmar (avanzar
    el offset) es lo que evita responder dos veces lo mismo.
    """
    if not config.TELEGRAM_BOT_TOKEN:
        return []

    offset = _leer_estado()
    url = (f"https://api.telegram.org/bot{config.TELEGRAM_BOT_TOKEN}/getUpdates"
           f"?timeout={timeout}&allowed_updates=%5B%22message%22%5D")
    if offset:
        url += f"&offset={offset}"

    try:
        timeout_red = (timeout + 5) if timeout > 0 else 10
        datos = http.get_json(url, timeout=timeout_red, retries=1)
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
    ("falabella", "Ofertas de Falabella"),
    ("homecenter", "Ofertas de Homecenter"),
    ("mercadolibre", "Ofertas de Mercado Libre"),
    ("colombia", "Lo mejor de las tiendas colombianas"),
    ("drogueria", "Rebajas de droguerias"),
    ("cajita", "Ofertas de PROMOCAJITA"),
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
