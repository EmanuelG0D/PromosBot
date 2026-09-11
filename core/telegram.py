"""Envio y formato de alertas a Telegram (HTML con cupones 'tap-to-copy')."""
from __future__ import annotations

import datetime as dt
import hashlib
import re

import time

import config
from core import http
from core.landed import Landed
from core.models import Deal
from core.scoring import Verdict
from core.veracidad import Veracidad

API = "https://api.telegram.org/bot{token}/{method}"
NL = chr(10)


def enabled() -> bool:
    return bool(config.TELEGRAM_BOT_TOKEN and config.TELEGRAM_CHAT_ID)


def esc(text: str) -> str:
    return (text or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def money(value: float | None, currency: str) -> str:
    if value is None:
        return "s/d"
    if currency == "COP":
        return "$" + f"{value:,.0f}".replace(",", ".")
    return f"US${value:,.2f}"


def _emoji(verdict: Verdict) -> str:
    if verdict.glitch:
        return "\U0001F6A8"          # sirena: posible error de precio
    if verdict.confianza == "alta":
        return "\U0001F525"          # fuego: caida confirmada con historial
    return "\U0001F3F7"              # etiqueta: oferta normal


def _lineas(deal: Deal, verdict: Verdict, landed: Landed | None = None,
            veracidad: Veracidad | None = None) -> list[str]:
    lineas: list[str] = []

    encabezado = f"{_emoji(verdict)} <b>{esc(deal.store)}</b>"
    if deal.discount_verificable:
        encabezado += f" \u00b7 <b>-{deal.discount_verificable:g}%</b>"
    lineas.append(encabezado)
    lineas.append(f"<b>{esc(deal.title[:160])}</b>")

    precio = money(deal.price, deal.currency)
    if deal.list_price_trusted and deal.list_price and deal.list_price > (deal.price or 0):
        lineas.append(f"\U0001F4B5 <s>{money(deal.list_price, deal.currency)}</s> \u2192 <b>{precio}</b>")
    else:
        lineas.append(f"\U0001F4B5 <b>{precio}</b>")

    # Precio con hora de caducidad: trasnochon u oferta relampago.
    if deal.vence_pronto:
        horas = deal.horas_restantes
        cuando = ""
        try:
            vence = dt.datetime.fromisoformat(deal.expires_at.replace("Z", "+00:00"))
            local = vence.astimezone(dt.timezone(dt.timedelta(hours=-5)))
            cuando = local.strftime(" (hasta las %H:%M del %d/%m)")
        except (AttributeError, TypeError, ValueError):
            pass
        aviso = f"menos de 1 hora" if horas < 1 else f"{horas:g} horas"
        lineas.append(f"⏳ <b>Termina en {aviso}</b><i>{cuando}</i>")

    # La prueba de honestidad: el minimo real medido por nosotros.
    if veracidad is not None and veracidad.minimo_ventana:
        referencia = money(veracidad.minimo_ventana, deal.currency)
        dias = veracidad.dias_observados
        if veracidad.es_real:
            lineas.append(f"📉 Minimo de {dias} dias: {referencia} "
                          f"<b>(hoy baja de ahi)</b>")
        else:
            lineas.append(f"⚠ Minimo de {dias} dias: {referencia} "
                          f"<i>(la rebaja no lo mejora)</i>")
        if veracidad.alza_previa:
            lineas.append(f"   <i>Le subieron {veracidad.alza_previa:g}% "
                          "antes de anunciar la rebaja.</i>")

    for nota in deal.notes[:3]:
        # Tarjeta de credito para promos bancarias, vineta para el resto.
        icono = "\U0001F4B3" if "arjeta" in nota else "\u2022"
        lineas.append(f"{icono} {esc(nota)}")

    if deal.coupons:
        codigos = "  ".join(f"<code>{esc(c)}</code>" for c in deal.coupons[:3])
        lineas.append(f"\U0001F39F Cupon: {codigos}  <i>(tocalo para copiarlo)</i>")

    if landed is not None:
        detalle = (
            f"FOB {money(landed.fob_usd, 'USD')} + flete {money(landed.flete_usd, 'USD')}"
        )
        if landed.exento:
            detalle += " \u00b7 exento de IVA"
        else:
            detalle += f" + impuestos {money(landed.iva_usd + landed.arancel_usd, 'USD')}"
        lineas.append(
            f"\U0001F1E8\U0001F1F4 Puesto en Colombia \u2248 <b>{money(landed.total_cop, 'COP')}</b>"
        )
        lineas.append(f"   <i>{esc(detalle)} \u00b7 TRM {money(landed.trm, 'COP')}</i>")

    if verdict.etiquetas:
        lineas.append(f"\U0001F4CC <i>{esc(' \u00b7 '.join(verdict.etiquetas[:4]))}</i>")

    if verdict.glitch:
        lineas.append("<i>Verifica antes de pagar: los errores de precio suelen cancelarse.</i>")

    lineas.append(f'\U0001F517 <a href="{esc(deal.url)}">Abrir oferta</a>')
    return lineas


def render(deal: Deal, verdict: Verdict, landed: Landed | None = None,
           veracidad: Veracidad | None = None) -> str:
    return NL.join(_lineas(deal, verdict, landed, veracidad))


def _pie_de_foto(deal: Deal, verdict: Verdict, landed: Landed | None = None,
                 veracidad: Veracidad | None = None, limite: int = 1024) -> str:
    """Telegram corta los pies de foto en 1024 caracteres.

    Si no cabe, se van sacrificando lineas de detalle desde el final, pero
    nunca la ultima: el enlace a la oferta es lo que no se puede perder.
    """
    lineas = _lineas(deal, verdict, landed, veracidad)
    while len(NL.join(lineas)) > limite and len(lineas) > 3:
        del lineas[-2]
    return NL.join(lineas)[:limite]


def _dominio(url: str) -> str:
    return url.split("/")[2] if url.count("/") > 2 else url[:40]


def _foto_por_url(foto: str, pie: str, chat_id: int | str | None = None) -> bool:
    """Le pasa la URL a Telegram para que la baje el. Es lo barato."""
    url = API.format(token=config.TELEGRAM_BOT_TOKEN, method="sendPhoto")
    destino = chat_id if chat_id is not None else config.TELEGRAM_CHAT_ID
    payload = {
        "chat_id": destino,
        "photo": foto,
        "caption": pie,
        "parse_mode": "HTML",
    }
    respuesta = http.post_json(url, payload, retries=1)
    return bool(respuesta.get("ok"))


def _foto_subida(foto: str, pie: str, chat_id: int | str | None = None) -> bool:
    """Baja la imagen y la sube como archivo.

    Hay CDN de tienda que no responden a los servidores de Telegram aunque
    desde aqui carguen perfecto: media.falabella.com.co devuelve un JPEG de
    768x768 sin problema, y Telegram contesta "failed to get HTTP URL
    content". Subiendo los bytes, Telegram no tiene que alcanzar a nadie.

    Cuesta una descarga de unos 80 KB, menos de un segundo, y se pierde dentro
    de la pausa de 3.5s que igual hay entre mensajes.
    """
    url = API.format(token=config.TELEGRAM_BOT_TOKEN, method="sendPhoto")
    imagen = http.descargar(foto, retries=1)
    destino = chat_id if chat_id is not None else config.TELEGRAM_CHAT_ID

    # Si la imagen no es un formato estandar directo (ej. AVIF de K-tronix),
    # convertirla a JPEG en memoria para que Telegram no la rechace con IMAGE_PROCESS_FAILED.
    if imagen and not (imagen.startswith(b"\xff\xd8\xff") or imagen.startswith(b"\x89PNG") or imagen.startswith(b"GIF8")):
        try:
            import io
            from PIL import Image
            img = Image.open(io.BytesIO(imagen))
            buf = io.BytesIO()
            img.convert("RGB").save(buf, format="JPEG", quality=85)
            imagen = buf.getvalue()
        except Exception:
            pass

    campos = {"chat_id": destino, "caption": pie, "parse_mode": "HTML"}

    respuesta = http.post_multipart(
        url,
        campos,
        ("photo", "oferta.jpg", imagen),
        retries=1,
    )
    if not respuesta.get("ok"):
        print(f"  [telegram] la subida tambien fallo: {respuesta.get('description')}")
    return bool(respuesta.get("ok"))


_CDNS_SUBIDA_DIRECTA = (
    "cdn.dam.ktronix.com",
    "cdn.dam.alkosto.com",
    "media.falabella.com.co",
)


def _enviar_foto(foto: str, pie: str, chat_id: int | str | None = None) -> bool:
    """Primero por URL; si Telegram no puede bajarla, se le suben los bytes."""
    # Las CDN que sabemos que bloquean peticiones directas de Telegram van directo a subida
    # para no perder varios segundos de espera por producto.
    if not any(cdn in foto for cdn in _CDNS_SUBIDA_DIRECTA):
        try:
            if _foto_por_url(foto, pie, chat_id=chat_id):
                return True
            print(f"  [telegram] {_dominio(foto)} no le sirve por URL; se sube")
        except Exception as exc:
            print(f"  [telegram] {_dominio(foto)} rechazada por URL ({exc}); se sube")

    try:
        return _foto_subida(foto, pie, chat_id=chat_id)
    except Exception as exc:
        # Imagen caida de verdad. Se avisa con el dominio para poder ubicar
        # que tienda publica imagenes problematicas.
        print(f"  [telegram] no se pudo subir la foto de {_dominio(foto)}: {exc}")
        return False


def enviar_oferta(deal: Deal, verdict: Verdict, landed: Landed | None = None,
                  veracidad: Veracidad | None = None,
                  chat_id: int | str | None = None) -> str:
    """Manda la oferta como tarjeta con foto; si la foto falla, como texto.

    Devuelve "foto", "texto" o "" si no se pudo enviar. Saber por cual de los
    dos caminos salio es la unica forma de detectar que una tienda publica
    imagenes que Telegram rechaza.
    """
    if not enabled():
        return ""
    if deal.image and _enviar_foto(deal.image, _pie_de_foto(deal, verdict, landed, veracidad), chat_id=chat_id):
        return "foto"
    # Sin foto, o si Telegram la rechazo, se manda como texto dejando que
    # Telegram arme su propia vista previa del enlace.
    return "texto" if send(render(deal, verdict, landed, veracidad), preview=True, chat_id=chat_id) else ""


def _avisar_migracion(error: Exception) -> None:
    """Un grupo que pasa a supergrupo cambia de id y deja al bot mudo.

    Telegram lo dice en el cuerpo del error, pero como un 400 cualquiera. Vale
    la pena gritarlo, porque la solucion es cambiar una variable y no hay forma
    de adivinarlo desde afuera.
    """
    texto = str(error)
    if "migrate_to_chat_id" not in texto:
        return
    nuevo = re.search(r"migrate_to_chat_id[^-\d]*(-?\d+)", texto)
    print("  [telegram] EL GRUPO PASO A SUPERGRUPO Y CAMBIO DE ID.")
    if nuevo:
        print(f"  [telegram] actualiza TELEGRAM_CHAT_ID a: {nuevo.group(1)}")


def teclado_tiendas() -> dict:
    """Menu principal de tiendas para ReplyKeyboardMarkup."""
    return {
        "keyboard": [
            [{"text": "🟡 Éxito"}, {"text": "🔴 Alkosto"}],
            [{"text": "💛 Mercado Libre"}, {"text": "🟢 Falabella"}],
            [{"text": "⚪ K-tronix"}, {"text": "🔵 Olímpica"}],
            [{"text": "🟢 Carulla"}, {"text": "🟠 Homecenter"}],
            [{"text": "📦 Promocajita"}, {"text": "🇨🇴 Todo Colombia"}],
            [{"text": "🇺🇸 Exterior (EE. UU.)"}, {"text": "🎯 Mis Objetivos"}],
        ],
        "resize_keyboard": True,
        "is_persistent": False,
    }


def teclado_categorias(tienda_nombre: str = "") -> dict:
    """Submenu de categorias para la tienda seleccionada."""
    return {
        "keyboard": [
            [{"text": "🌟 TODO"}, {"text": "📺 Smart TV"}],
            [{"text": "💻 Portátiles"}, {"text": "🖥️ Monitores"}],
            [{"text": "📱 Celulares"}, {"text": "👟 Zapatos y Tenis"}],
            [{"text": "🎧 Audio y Diademas"}, {"text": "❄️ Electrodomésticos"}],
            [{"text": "👕 Ropa y Moda"}, {"text": "⬅️ Volver a Tiendas"}],
        ],
        "resize_keyboard": True,
        "is_persistent": False,
    }


def teclado_aprobacion(user_id: int | str) -> dict:
    """Botones inline para que el admin apruebe o rechace a un usuario."""
    uid = str(user_id).strip()
    return {
        "inline_keyboard": [
            [
                {"text": "✅ Aprobar", "callback_data": f"aprobar:{uid}"},
                {"text": "❌ Rechazar", "callback_data": f"rechazar:{uid}"},
            ]
        ]
    }


def teclado_unirse_canal(url_canal: str | None = None) -> dict:
    """Botones inline para invitar al usuario al canal y verificar su membresia."""
    enlace = (url_canal or getattr(config, "TELEGRAM_CHANNEL_URL", "")
              or "https://t.me/+GE1nQO-f0HYwNGQx").strip()
    return {
        "inline_keyboard": [
            [
                {"text": "📢 Unirme a Ofertas", "url": enlace},
            ],
            [
                {"text": "🔄 Ya me uní", "callback_data": "verificar_canal"},
            ],
        ]
    }


def es_miembro_del_canal(user_id: int | str,
                         channel_id: int | str | None = None) -> bool:
    """Verifica si el usuario es miembro activo del canal/grupo configurado."""
    if not enabled() or not user_id:
        return False
    destino = channel_id if channel_id is not None else (
        getattr(config, "TELEGRAM_CHANNEL_ID", None) or config.TELEGRAM_CHAT_ID
    )
    if not destino:
        return True
    url = API.format(token=config.TELEGRAM_BOT_TOKEN, method="getChatMember")
    try:
        resp = http.post_json(url, {"chat_id": destino, "user_id": int(user_id)}, retries=1)
        if not resp.get("ok"):
            return False
        estado = (resp.get("result") or {}).get("status", "")
        return estado in ("creator", "administrator", "member", "restricted")
    except Exception as exc:
        print(f"  [telegram] fallo verificando membresia de {user_id}: {exc}")
        return False


def send(html: str, preview: bool = False, reply_markup: dict | None = None,
         chat_id: int | str | None = None) -> bool:
    if not enabled():
        return False
    url = API.format(token=config.TELEGRAM_BOT_TOKEN, method="sendMessage")
    destino = chat_id if chat_id is not None else config.TELEGRAM_CHAT_ID
    payload = {
        "chat_id": destino,
        "text": html,
        "parse_mode": "HTML",
        "disable_web_page_preview": not preview,
    }
    if reply_markup is not None:
        payload["reply_markup"] = reply_markup
    try:
        respuesta = http.post_json(url, payload, retries=1)
        return bool(respuesta.get("ok"))
    except Exception as exc:
        _avisar_migracion(exc)
        print(f"  [telegram] fallo el envio a {destino}: {exc}")
        return False


def editar_mensaje(chat_id: int | str, message_id: int, texto: str,
                   reply_markup: dict | None = None) -> bool:
    """Edita el texto y los botones de un mensaje ya enviado."""
    if not enabled():
        return False
    url = API.format(token=config.TELEGRAM_BOT_TOKEN, method="editMessageText")
    payload = {
        "chat_id": chat_id,
        "message_id": message_id,
        "text": texto,
        "parse_mode": "HTML",
    }
    if reply_markup is not None:
        payload["reply_markup"] = reply_markup
    try:
        respuesta = http.post_json(url, payload, retries=1)
        return bool(respuesta.get("ok"))
    except Exception as exc:
        print(f"  [telegram] fallo editando mensaje: {exc}")
        return False


def responder_callback(callback_query_id: str, texto: str = "",
                       alerta: bool = False) -> bool:
    """Confirma la recepcion de un click en un boton inline."""
    if not enabled():
        return False
    url = API.format(token=config.TELEGRAM_BOT_TOKEN, method="answerCallbackQuery")
    payload = {
        "callback_query_id": callback_query_id,
        "text": texto,
        "show_alert": alerta,
    }
    try:
        respuesta = http.post_json(url, payload, retries=1)
        return bool(respuesta.get("ok"))
    except Exception as exc:
        print(f"  [telegram] fallo respondiendo callback: {exc}")
        return False


def accion_escribiendo(chat_id: int | str | None = None) -> bool:
    """Muestra 'escribiendo...' en la cabecera de Telegram mientras procesa una busqueda."""
    if not enabled():
        return False
    url = API.format(token=config.TELEGRAM_BOT_TOKEN, method="sendChatAction")
    destino = chat_id if chat_id is not None else config.TELEGRAM_CHAT_ID
    try:
        respuesta = http.post_json(url, {"chat_id": destino, "action": "typing"}, retries=1)
        return bool(respuesta.get("ok"))
    except Exception:
        return False


def render_resumen(pares) -> str:
    """Un unico mensaje con las ofertas que no ameritan interrumpir.

    Telegram corta los mensajes en 4096 caracteres, asi que se agregan lineas
    mientras quepan y el resto se anuncia como pendiente.
    """
    cabecera = f"\U0001F4CB <b>Resumen de ofertas</b> ({len(pares)} nuevas)"
    lineas = [cabecera, ""]
    largo = len(cabecera) + 2
    incluidas = 0

    for deal, _verdict in pares:
        pct = f"-{deal.discount_verificable:g}% " if deal.discount_verificable else ""
        cupon = ""
        if deal.coupons:
            cupon = " \U0001F39F <code>" + esc(deal.coupons[0]) + "</code>"
        linea = (f"\u2022 <b>{esc(deal.store)}</b> {pct}"
                 f'<a href="{esc(deal.url)}">{esc(deal.title[:70])}</a> '
                 f"\u2014 <b>{money(deal.price, deal.currency)}</b>{cupon}")
        if largo + len(linea) > 3900:
            lineas.append(f"<i>...y {len(pares) - incluidas} mas que no cupieron.</i>")
            break
        lineas.append(linea)
        largo += len(linea) + 1
        incluidas += 1

    return "\n".join(lineas)


def descubrir_chats() -> list[dict]:
    """Chats donde el bot ha visto actividad reciente (para hallar el chat_id).

    Sirve tanto para grupos como para canales. Basta con agregar el bot al
    grupo: eso genera un update de tipo my_chat_member, aunque nadie escriba.
    Telegram solo guarda los updates de las ultimas 24 horas.
    """
    if not config.TELEGRAM_BOT_TOKEN:
        raise RuntimeError("falta TELEGRAM_BOT_TOKEN en el archivo .env")

    url = API.format(token=config.TELEGRAM_BOT_TOKEN, method="getUpdates")
    datos = http.get_json(url, retries=1)
    if not datos.get("ok"):
        raise RuntimeError(f"Telegram respondio: {datos}")

    campos = ("message", "edited_message", "channel_post", "edited_channel_post",
              "my_chat_member", "chat_member")
    encontrados: dict = {}
    for update in datos.get("result", []):
        for campo in campos:
            objeto = update.get(campo)
            if not isinstance(objeto, dict):
                continue
            chat = objeto.get("chat") or {}
            if chat.get("id") is None:
                continue
            encontrados[chat["id"]] = {
                "id": chat["id"],
                "tipo": chat.get("type", "?"),
                "nombre": (chat.get("title") or chat.get("username")
                           or chat.get("first_name") or "(sin nombre)"),
            }
    return list(encontrados.values())


def info_bot() -> dict:
    """Identidad del bot dueno del token, para confirmar que es el correcto."""
    if not config.TELEGRAM_BOT_TOKEN:
        raise RuntimeError("falta TELEGRAM_BOT_TOKEN en el archivo .env")
    datos = http.get_json(API.format(token=config.TELEGRAM_BOT_TOKEN, method="getMe"), retries=1)
    if not datos.get("ok"):
        raise RuntimeError(f"Telegram respondio: {datos}")
    return datos.get("result") or {}


def secreto_webhook() -> str:
    """Clave con la que Telegram firma cada entrega del webhook.

    Si no se configura una, se deriva del token: queda estable entre reinicios
    (Telegram la guarda al registrar el webhook) y no revela nada, porque un
    hash no se puede devolver al token.
    """
    if config.TELEGRAM_WEBHOOK_SECRET:
        return config.TELEGRAM_WEBHOOK_SECRET
    return hashlib.sha256(config.TELEGRAM_BOT_TOKEN.encode()).hexdigest()[:48]


def registrar_webhook(url: str) -> bool:
    """Le dice a Telegram que entregue los mensajes en esa URL.

    Con webhook los comandos llegan al instante y getUpdates queda mudo: son
    excluyentes, y es a proposito. Se piden solo los mensajes porque el bot no
    tiene nada que hacer con los demas tipos de evento.
    """
    if not config.TELEGRAM_BOT_TOKEN:
        return False
    cuerpo = {
        "url": url,
        "secret_token": secreto_webhook(),
        "allowed_updates": ["message", "callback_query"],
        # Un despliegue no debe arrastrar comandos de hace horas: para cuando
        # se respondieran, quien los pidio ya ni se acuerda.
        "drop_pending_updates": True,
    }
    url_api = API.format(token=config.TELEGRAM_BOT_TOKEN, method="setWebhook")
    try:
        respuesta = http.post_json(url_api, cuerpo, retries=1)
        if not respuesta.get("ok"):
            print(f"  [telegram] webhook rechazado: {respuesta.get('description')}")
        return bool(respuesta.get("ok"))
    except Exception as exc:
        print(f"  [telegram] no se pudo registrar el webhook: {exc}")
        return False


def quitar_webhook() -> bool:
    """Devuelve el bot a getUpdates (util para depurar desde el portatil)."""
    if not config.TELEGRAM_BOT_TOKEN:
        return False
    url_api = API.format(token=config.TELEGRAM_BOT_TOKEN, method="deleteWebhook")
    try:
        return bool(http.post_json(url_api, {}, retries=1).get("ok"))
    except Exception as exc:
        print(f"  [telegram] no se pudo quitar el webhook: {exc}")
        return False


def webhook_activo() -> str:
    """URL del webhook configurado, si lo hay. Un webhook deja mudo a getUpdates."""
    try:
        datos = http.get_json(
            API.format(token=config.TELEGRAM_BOT_TOKEN, method="getWebhookInfo"), retries=1)
        return (datos.get("result") or {}).get("url") or ""
    except Exception:
        return ""
