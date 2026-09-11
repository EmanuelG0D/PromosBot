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
from core import http, telegram, whitelist

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


def tienda_activa(chat_id: int | str = "") -> str:
    """Tienda seleccionada actualmente en el menu de navegacion."""
    datos = _cargar()
    if chat_id:
        tiendas = datos.get("tiendas_activas", {})
        if str(chat_id) in tiendas:
            return str(tiendas[str(chat_id)])
    return str(datos.get("tienda_activa") or "colombia")


def fijar_tienda_activa(tienda: str, chat_id: int | str = "") -> None:
    """Guarda la tienda activa para que los botones de categoria sepan a quien consultar."""
    datos = _cargar()
    datos["tienda_activa"] = tienda
    if chat_id:
        tiendas = datos.setdefault("tiendas_activas", {})
        tiendas[str(chat_id)] = tienda
    _guardar(datos)


def ya_mostradas(chat_id: int | str = "") -> set:
    """Ofertas que ya se enviaron respondiendo comandos."""
    datos = _cargar()
    if chat_id:
        mostradas_chat = datos.get("mostradas_por_chat", {})
        if str(chat_id) in mostradas_chat:
            return set(mostradas_chat[str(chat_id)])
    return set(datos.get("mostradas") or [])


def marcar_mostradas(claves, chat_id: int | str = "") -> None:
    """Recuerda lo enviado para que la proxima vez traiga cosas distintas."""
    datos = _cargar()
    nuevas = list(claves)
    previas = [c for c in (datos.get("mostradas") or []) if c not in set(nuevas)]
    datos["mostradas"] = (previas + nuevas)[-MEMORIA_MOSTRADAS:]
    if chat_id:
        mostradas_chat = datos.setdefault("mostradas_por_chat", {})
        previas_chat = [c for c in (mostradas_chat.get(str(chat_id)) or []) if c not in set(nuevas)]
        mostradas_chat[str(chat_id)] = (previas_chat + nuevas)[-MEMORIA_MOSTRADAS:]
    _guardar(datos)


def leer_comando(mensaje: dict) -> dict | None:
    """Un mensaje de Telegram vuelto solicitud, o None si no es para el bot.

    Soporta comandos tradicionales con '/' y toques en los botones del menu interactivo.
    """
    texto = ((mensaje or {}).get("text") or "").strip()
    chat_obj = (mensaje or {}).get("chat") or {}
    chat = chat_obj.get("id")
    chat_type = chat_obj.get("type", "")
    if not texto or chat is None:
        return None

    user = (mensaje or {}).get("from") or {}
    user_id = user.get("id")
    nombre = user.get("first_name") or "Usuario"
    username = user.get("username")

    # En grupos y canales no se atiende ningun comando ni menu:
    # el bot solo difunde ofertas automaticas y toda interaccion es por privado.
    if chat_type in ("group", "supergroup", "channel"):
        return None

    es_grupo_configurado = str(chat) == str(config.TELEGRAM_CHAT_ID)
    es_privado = chat_type == "private"

    # Si no es el grupo configurado ni un chat privado, se ignora
    if not (es_grupo_configurado or es_privado):
        print(f"  [comandos] ignorado: viene del chat {chat}")
        return None

    # En chat privado: verificar suscripcion al canal y lista blanca
    if es_privado:
        # El Administrador siempre tiene acceso libre
        if not whitelist.es_admin(user_id):
            # 1. Filtro obligatorio: debe estar en el canal oficial
            if not telegram.es_miembro_del_canal(user_id):
                return {
                    "comando": "unirse_canal",
                    "chat_id": chat,
                    "user_id": user_id,
                    "nombre": nombre,
                    "username": username,
                    "tipo": "unirse_canal",
                }

            # 2. Filtro de aprobacion: debe estar en la whitelist
            if not whitelist.es_permitido(user_id):
                es_nueva = whitelist.registrar_solicitud(user_id, nombre=nombre, username=username)
                return {
                    "comando": "solicitud_acceso",
                    "chat_id": chat,
                    "user_id": user_id,
                    "nombre": nombre,
                    "username": username,
                    "tipo": "solicitud_acceso",
                    "es_nueva": es_nueva,
                }

    res = None
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
        res = {"comando": crudo, "chat_id": chat, "cantidad": cantidad}

    # 2. Botones del teclado interactivo (sin "/")
    else:
        texto_norm = texto.lower()

        # Volver a la lista de tiendas
        if "volver a tiendas" in texto_norm or "volver" in texto_norm:
            res = {"comando": "menu", "chat_id": chat, "cantidad": None, "tipo": "menu"}

        # Boton de tienda
        if not res:
            for btn_key, (tienda_key, tienda_nombre) in BOTONES_TIENDA.items():
                if texto_norm == btn_key:
                    if tienda_key == "objetivos":
                        res = {"comando": "objetivos", "chat_id": chat, "cantidad": None}
                    else:
                        res = {
                            "comando": "elegir_tienda",
                            "tienda": tienda_key,
                            "tienda_nombre": tienda_nombre,
                            "chat_id": chat,
                            "cantidad": None,
                            "tipo": "elegir_tienda",
                        }
                    break

        # Boton '🌟 TODO'
        if not res and "todo" in texto_norm:
            res = {
                "comando": "todo_tienda",
                "chat_id": chat,
                "cantidad": None,
                "tipo": "todo_tienda",
            }

        # Boton de categoria
        if not res:
            for cat_label, queries in CATEGORIAS_BUSQUEDA.items():
                sin_emoji = cat_label.split(" ", 1)[-1].lower()
                if texto_norm == cat_label.lower() or texto_norm == sin_emoji:
                    res = {
                        "comando": "categoria",
                        "categoria_nombre": cat_label,
                        "consultas": queries,
                        "chat_id": chat,
                        "cantidad": None,
                        "tipo": "categoria",
                    }
                    break

    if res:
        if user_id is not None:
            res["user_id"] = user_id
            res["nombre"] = nombre
            res["username"] = username
        return res

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
           f"?timeout={timeout}&allowed_updates=%5B%22message%22%2C%22callback_query%22%5D")
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
        if "callback_query" in update:
            encontrados.append({
                "tipo": "callback_query",
                "callback_query": update["callback_query"],
                "update_id": update.get("update_id"),
            })
            continue
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
    """Publica los comandos exclusivamente en chats privados, nunca en grupos."""
    if not config.TELEGRAM_BOT_TOKEN:
        return False
    try:
        url_set = f"https://api.telegram.org/bot{config.TELEGRAM_BOT_TOKEN}/setMyCommands"
        url_del = f"https://api.telegram.org/bot{config.TELEGRAM_BOT_TOKEN}/deleteMyCommands"
        cmds = [{"command": c, "description": d} for c, d in MENU]

        # 1. Limpiar comandos de grupos y globales (para que NUNCA aparezca el boton [/] en grupos)
        http.post_json(url_del, {"scope": {"type": "default"}}, retries=1)
        http.post_json(url_del, {"scope": {"type": "all_group_chats"}}, retries=1)
        http.post_json(url_del, {"scope": {"type": "all_chat_administrators"}}, retries=1)
        if config.TELEGRAM_CHAT_ID:
            http.post_json(url_del, {"scope": {"type": "chat", "chat_id": config.TELEGRAM_CHAT_ID}}, retries=1)
            http.post_json(url_del, {"scope": {"type": "chat_administrators", "chat_id": config.TELEGRAM_CHAT_ID}}, retries=1)
            if config.TELEGRAM_ADMIN_ID:
                try:
                    http.post_json(url_del, {"scope": {"type": "chat_member", "chat_id": config.TELEGRAM_CHAT_ID, "user_id": int(config.TELEGRAM_ADMIN_ID)}}, retries=1)
                except (ValueError, TypeError):
                    pass

        # 2. Publicar comandos UNICAMENTE en chats privados
        http.post_json(url_set, {"commands": cmds, "scope": {"type": "all_private_chats"}}, retries=1)
        return True
    except Exception as exc:
        print(f"  [comandos] no se pudo registrar el menu: {exc}")
        return False
