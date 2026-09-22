"""Publicación automática de ofertas en Página de Facebook vía Graph API.

Implementa los 5 mecanismos de blindaje anti-spam exigidos por Meta:
1. Link Cloaking con endpoint en Render (/ir/{id_oferta}).
2. Cola de publicación y agrupación carrusel (Rate limit de 45 min y attached_media).
3. Formateo Spintax y truncamiento a 60 caracteres.
4. Protocolo del Primer Comentario: cero URLs en el caption del post.
5. Regla del Camuflaje (5 a 1): post orgánico limpio cada 5 publicaciones promocionales.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import io
import json
import os
import random
import urllib.error
import urllib.parse
import urllib.request

from PIL import Image, ImageDraw, ImageFont

import config
from core import branding
from core.landed import Landed
from core.models import Deal
from core.scoring import Verdict
from core.store import Store
from core.veracidad import Veracidad

DOMINIO_DEFAULT = os.environ.get("RENDER_EXTERNAL_URL", "https://promosbot.onrender.com").rstrip("/")

# --- BANCOS SPINTAX (Variación natural sin IA) -----------------------------
SPINTAX_ENCABEZADOS = [
    "🔥 ¡TOP OFERTAS RECIÉN DETECTADAS! 🔥",
    "🚨 ¡BAJARON DE PRECIO EN COLOMBIA! 🚨",
    "⚡ ¡OFERTAS DESTACADAS DEL RADAR! ⚡",
    "🎯 ¡SELECCIÓN DE GANGAS VERIFICADAS! 🎯",
    "💥 ¡REBAJAS IMPERDIBLES DE HOY! 💥",
    "🛍️ ¡NUEVAS PROMOCIONES ENCONTRADAS! 🛍️",
    "✨ ¡DESCUENTOS QUE VALEN LA PENA! ✨",
]

SPINTAX_AVISOS_COMENTARIO = [
    "👇 Los enlaces oficiales de compra y cupones están en el primer comentario.",
    "📌 Mira los links verificados a las tiendas en el primer comentario fijado 👇",
    "👉 Accede a cada producto desde el primer comentario de esta publicación.",
    "💬 Enlaces directos a las tiendas oficiales en el primer comentario 👇",
    "🔎 Encuentra los links y cupones abajo en el primer comentario.",
]

# --- BANCOS SPINTAX DE CARRUSEL (Estructura de 3 bloques anti-ban) ----------
SPINTAX_CARRUSEL_ENCABEZADOS = [
    "💥 ¡OFERTAS ESPECTACULARES DEL DÍA! 💥",
    "🔥 ¡REBAJAS EXCLUSIVAS DEL RADAR! 🔥",
    "⚡ ¡SELECCIÓN DE GANGAS VERIFICADAS! ⚡",
    "🚨 ¡TOP PROMOCIONES ENCONTRADAS HOY! 🚨",
    "🎯 ¡PRECIOS IMPERDIBLES EN TIENDAS OFICIALES! 🎯",
    "🛍️ ¡DESCUENTOS DESTACADOS DE HOY! 🛍️",
    "✨ ¡OPORTUNIDADES DE AHORRO DEL DÍA! ✨",
    "🏷️ ¡OFERTAS IMPERDIBLES DETECTADAS! 🏷️",
]

SPINTAX_CARRUSEL_BAJADAS = [
    "Seleccionamos las mejores rebajas y gangas verificadas en tiendas oficiales.",
    "Encontramos descuentos reales y precios bajos en tus tiendas favoritas.",
    "Detectamos promociones destacadas y errores de precio que valen la pena aprovechar.",
    "Monitoreamos el mercado y reunimos estas oportunidades con descuentos increíbles.",
    "Reunimos los mejores descuentos activos en este momento para que compres informado.",
    "Filtramos las mejores ofertas de hoy directamente de comercios autorizados.",
    "Aquí tienes nuestra selección de artículos con grandes bajas de precio garantizadas.",
    "Rastreamos las mejores rebajas del momento para ahorrar en tus compras online.",
]

SPINTAX_CARRUSEL_CTAS = [
    "👇 Toca cada foto para ver el precio especial, cupones y enlace de compra directa:",
    "👇 Haz clic en cada foto para ver su cupón, precio final y link directo:",
    "👉 Toca o desliza cada imagen para consultar los cupones y el enlace oficial 👇",
    "🔍 Abre la foto del producto que te interese para ver cupón y comprar directo 👇",
    "👇 Desliza las fotos y toca la que te guste para acceder a la tienda oficial:",
    "👉 Toca cualquier foto para ver el descuento, cupón activo y enlace oficial 👇",
    "👇 Haz clic sobre cada producto en la galería para ver toda la info y comprar:",
    "✨ Toca cada imagen para ver cupón exclusivo y comprar en la tienda oficial 👇",
]

SPINTAX_CTA_FOTO = [
    "👉 Comprar directo en tienda oficial:",
    "👉 Ver oferta y comprar aquí:",
    "👉 Enlace oficial de compra:",
    "👉 Toca para comprar directo:",
    "👉 Accede a la tienda oficial aquí:",
    "👉 Link verificado de compra:",
]

SPINTAX_CAMUFLAJE = [
    "☕ Buenos días cazadores de ofertas. ¿Qué producto están esperando que baje de precio esta semana? Cuéntennos en los comentarios 👇",
    "💡 Tip de Ahorro: Antes de comprar en línea, revisa siempre si la tienda tiene cupón de primera compra o descuento adicional por pagar con tarjeta débito o crédito específica.",
    "🛒 Pregunta para la comunidad: Si tuvieras que elegir una sola tienda para comprar tecnología con descuento en Colombia, ¿cuál elegirías?",
    "Verdad del comprador online: Llenar el carrito de compras a medianoche solo para ver cuánto sería el total y luego no comprar nada 😂 ¿A quién más le pasa?",
    "🎯 ¿Cuál ha sido la mejor ganga o error de precio que has logrado comprar en internet? ¡Déjanos tu historia en los comentarios!",
    "📱 ¿Prefieres comprar desde la aplicación móvil de las tiendas o directamente desde la página web en el computador? Déjanos tu opinión 👇",
    "💳 Regla de oro financiera: Si vas a pagar con tarjeta de crédito para aprovechar una oferta, págalo a 1 sola cuota. Así ganas puntos o cashback sin pagar un solo peso de interés.",
    "🎮 Encuesta gamer: ¿PlayStation 5, Xbox Series X o PC Gamer? ¿Cuál consideran que ofrece mejor relación costo-beneficio hoy en día?",
    "📦 ¿Cuánto es lo máximo que han esperado por un paquete de compras por internet? ¿Son de los que miran el rastreo del envío cada 10 minutos?",
    "💡 Consejo para comprar tecnología: No siempre el modelo del año actual vale la pena. Muchas veces el modelo del año anterior baja 40% de precio y rinde casi igual.",
    "🛍️ Debate de tiendas en Colombia: ¿Qué tienda consideran que tiene los mejores tiempos de entrega y garantía: Alkosto, Falabella o Éxito?",
    "👟 Amantes de los tenis y ropa deportiva: ¿Prefieren comprar en outlets físicos o cazar rebajas por internet? Los leemos en comentarios 👇",
    "🚨 Situación clásica: Compras algo con descuento y al día siguiente la tienda le baja todavía más el precio 🤦‍♂️ ¿Les ha pasado?",
    "📺 Para el hogar: ¿Smart TV de 55 pulgadas o proyector portátil? ¿Qué recomiendan para una sala de entretenimiento en casa?",
    "💡 Tip para comprar en Amazon desde Colombia: Recuerda que compras de productos enviados por Amazon superiores a 35 dólares tienen envío GRATIS directo a Colombia.",
    "🎧 En audífonos inalámbricos: ¿Priorizan la cancelación activa de ruido, la duración de la batería o la calidad del micrófono para llamadas?",
    "🛒 Pregunta sincera: ¿Compran por necesidad real o la emoción de ver un 60% de descuento es irresistible? 😂 ¡Confiesen en comentarios!",
    "💻 Para trabajar o estudiar: ¿Son del equipo portátil ligero (tipo MacBook/Zenbook) o prefieren armar un computador de escritorio potente?",
    "💡 Tip de compras: Antes de pagar, abre una pestaña en modo incógnito. Algunas tiendas suben los precios si detectan que visitaste el mismo producto varias veces.",
    "⌚ Relojes inteligentes: ¿Realmente los usan para deporte y salud o solo para ver las notificaciones de WhatsApp sin sacar el celular?",
    "🇨🇴 Compras nacionales vs importadas: ¿Prefieren pagar un poco más por tener garantía local en Colombia o pedir directo de USA/China por mejor precio?",
    "🍳 Para la cocina: ¿La freidora de aire realmente les cambió la vida o terminó arrumada en una esquina de la cocina? ¡Queremos opiniones reales!",
    "💡 Tip de seguridad: Nunca compres en páginas que te pidan pagar únicamente por transferencia directa a cuentas personales de Nequi o Daviplata sin pasarela de pagos segura.",
    "☕ Debate mañanero: ¿Cuál es el electrodoméstico que más les ha ahorrado tiempo en la casa este año?",
    "📦 Sensación insuperable: Cuando el domiciliario te llama diciendo 'tengo una entrega para usted' y ni te acordabas qué habías pedido 🎁",
    "📱 ¿Cada cuántos años cambian de celular en promedio? ¿Esperan a que muera por completo o cambian cada 2 años?",
    "💡 Tip para temporadas de rebajas (CyberLunes/Black Friday): Anota los precios desde dos semanas antes; así detectas de inmediato si el descuento es real o si inflaron el precio.",
    "🏠 Domótica y hogar inteligente: ¿Qué dispositivo inteligente recomiendan para empezar? ¿Bombillos WiFi, enchufes inteligentes o asistentes de voz?",
    "🎒 ¿Qué es lo primero que empacan en su morral cuando van a viajar: cargador portátil, audífonos o cámara?",
    "🛒 Pregunta de fin de semana: ¿Cuál es esa compra que hicieron por internet y que superó todas sus expectativas por lo barata que fue?",
    "💡 Tip de garantía: Guarda siempre la factura digital en una carpeta de Google Drive o correo. La mayoría de tiendas en Colombia exigen la factura para cualquier trámite.",
    "🔊 Parlantes Bluetooth para reuniones: ¿Equipo JBL, Sony o alternativas económicas de buena calidad? ¿Qué marca prefieren?",
    "🚗 ¿Qué accesorio para el carro o la moto consideran 100% indispensable que hayan comprado en oferta?",
    "💤 Domingo de descanso: ¿Qué serie o película se van a ver hoy? Dejen sus mejores recomendaciones en comentarios 👇",
    "🎯 Meta de ahorro: ¿Qué compra grande tienen planeada para este año: computador, moto, celular nuevo o remodelación del hogar?",
    "💡 Tip de compras online: Siempre revisa la sección de opiniones y fotos reales de compradores antes de decidirte por un producto poco conocido.",
]


_PAGE_TOKEN_CACHE: str | None = None


def _obtener_page_token() -> str:
    """Devuelve el token de página listo. Si el token configurado es de usuario, obtiene el Page Token de /me/accounts."""
    global _PAGE_TOKEN_CACHE
    if _PAGE_TOKEN_CACHE:
        return _PAGE_TOKEN_CACHE
    token = config.FB_PAGE_ACCESS_TOKEN
    page_id = config.FB_PAGE_ID
    if not token or not page_id:
        return token
    try:
        url = f"https://graph.facebook.com/v20.0/me/accounts?access_token={token}"
        req = urllib.request.Request(url)
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            for item in data.get("data", []):
                if str(item.get("id")) == str(page_id):
                    _PAGE_TOKEN_CACHE = item.get("access_token")
                    return _PAGE_TOKEN_CACHE
    except Exception:
        pass
    _PAGE_TOKEN_CACHE = token
    return _PAGE_TOKEN_CACHE


def configurado() -> bool:
    """Indica si las credenciales de Facebook están presentes y habilitadas."""
    return bool(config.FB_PAGE_ID and config.FB_PAGE_ACCESS_TOKEN and config.FB_ENABLED)


def money(amount: float | int | None, currency: str = "COP") -> str:
    if amount is None:
        return ""
    cur = (currency or "COP").upper()
    if cur == "USD":
        return f"US${amount:,.2f}"
    return f"${round(amount):,}".replace(",", ".")


def truncar(texto: str, max_chars: int = 60) -> str:
    """Trunca el título o descripción a un máximo de caracteres agregando '...' si excede."""
    t = (texto or "").strip()
    return t if len(t) <= max_chars else t[:max_chars].rstrip() + "..."


def spintax_encabezado() -> str:
    return random.choice(SPINTAX_ENCABEZADOS)


def spintax_aviso_comentario() -> str:
    return random.choice(SPINTAX_AVISOS_COMENTARIO)


def spintax_camuflaje() -> str:
    """Devuelve un post de camuflaje sin repetición garantizada mediante rotación cíclica."""
    try:
        with Store() as store:
            row = store.conn.execute("SELECT v FROM meta WHERE k = 'fb_camuflaje_usados'").fetchone()
            usados = json.loads(row["v"]) if row and row["v"] else []
            disponibles = [i for i in range(len(SPINTAX_CAMUFLAJE)) if i not in usados]
            if not disponibles:
                # Si ya rotaron todas las frases del banco, reinicia el ciclo
                disponibles = list(range(len(SPINTAX_CAMUFLAJE)))
                usados = []
            elegido_idx = random.choice(disponibles)
            usados.append(elegido_idx)
            store.conn.execute(
                "INSERT INTO meta(k, v) VALUES ('fb_camuflaje_usados', ?) "
                "ON CONFLICT(k) DO UPDATE SET v = excluded.v",
                (json.dumps(usados),)
            )
            store.conn.commit()
            return SPINTAX_CAMUFLAJE[elegido_idx]
    except Exception:
        return random.choice(SPINTAX_CAMUFLAJE)


# --- RENDERIZADORES DE TEXTO (REGLA: CERO URLS EN EL POST PRINCIPAL) -------

def render_individual(deal: Deal, verdict: Verdict | None = None,
                      landed: Landed | None = None,
                      veracidad: Veracidad | None = None,
                      hash_id: str | None = None,
                      base_url: str | None = None) -> str:
    """Genera el caption para una oferta individual con link cloaked seguro si se pasa hash_id."""
    lineas = []
    pct = round(deal.discount_pct or 0)
    descuento_str = f" (-{pct}%)" if pct > 0 else ""
    tienda = deal.store or "Tienda"

    if verdict and (verdict.glitch or getattr(deal, "es_glitch", False)):
        lineas.append(f"🚨 ¡ERROR DE PRECIO / SÚPER GANGA!{descuento_str} 🚨")
    elif pct >= 50:
        lineas.append(f"🔥 ¡OFERTÓN! {tienda}{descuento_str} 🔥")
    else:
        lineas.append(f"🏷️ {tienda}{descuento_str}")

    lineas.append("")
    lineas.append(deal.title.strip())
    lineas.append("")

    if deal.list_price and deal.price and deal.list_price > deal.price:
        lineas.append(f"💵 Antes: {money(deal.list_price, deal.currency)} ➡️ Ahora: {money(deal.price, deal.currency)}")
    elif deal.price:
        lineas.append(f"💵 Precio: {money(deal.price, deal.currency)}")

    if deal.coupons:
        cupones = ", ".join(deal.coupons[:2])
        lineas.append(f"🎟️ Cupón de descuento: {cupones}")

    if landed is not None:
        es_directo = getattr(deal, "free_shipping_co", False) or (
            deal.country == "US" and "amazon" in (deal.store or "").lower() and (deal.price or 0) >= 35
        )
        if es_directo:
            total_cop = round((deal.price or 0) * landed.trm)
            lineas.append(f"✈️🇨🇴 Envío GRATIS directo a Colombia (Total: {money(total_cop, 'COP')})")
        else:
            lineas.append(f"📦 Puesto en Colombia: ≈ {money(landed.total_cop, 'COP')} (TRM {money(landed.trm, 'COP')})")

    for nota in (deal.notes or [])[:2]:
        if "prime" in nota.lower() and "amazon" not in (deal.store or "").lower():
            continue
        lineas.append(f"• {nota}")

    lineas.append("")
    if hash_id:
        url_dest = deal.url or f"{(base_url or DOMINIO_DEFAULT).rstrip('/')}/ir/{hash_id}"
        lineas.append(f"👉 Ver oferta y comprar: {url_dest}")
    else:
        lineas.append(spintax_aviso_comentario())
    return "\n".join(lineas)


def spintax_post_carrusel() -> str:
    """Genera el texto editorial de 3 bloques para el post principal en modo carrusel."""
    enc = random.choice(SPINTAX_CARRUSEL_ENCABEZADOS)
    baj = random.choice(SPINTAX_CARRUSEL_BAJADAS)
    cta = random.choice(SPINTAX_CARRUSEL_CTAS)
    return f"{enc}\n\n{baj}\n\n{cta}"


def render_caption_foto(deal: Deal, hash_id: str | None = None, base_url: str | None = None) -> str:
    """Construye la ficha descriptiva para la foto individual en Facebook (visible en el visor de fotos)."""
    lineas = []
    pct = round(deal.discount_pct or 0)
    descuento_str = f" (-{pct}%)" if pct > 0 else ""
    tienda = deal.store or deal.source or "Tienda oficial"

    lineas.append(f"📦 {deal.title.strip()}")
    lineas.append("")
    lineas.append(f"🏷️ Tienda: {tienda}{descuento_str}")

    if deal.list_price and deal.price and deal.list_price > deal.price:
        lineas.append(f"💵 Antes: {money(deal.list_price, deal.currency)} ➡️ Ahora: {money(deal.price, deal.currency)}")
    elif deal.price:
        lineas.append(f"💵 Precio: {money(deal.price, deal.currency)}")

    if deal.coupons:
        cupones = ", ".join(deal.coupons[:2])
        lineas.append(f"🎟️ Cupón de descuento: {cupones}")

    es_directo = getattr(deal, "free_shipping_co", False) or (
        deal.country == "US" and "amazon" in (deal.store or "").lower() and (deal.price or 0) >= 35
    )
    if es_directo:
        lineas.append("✈️🇨🇴 Envío GRATIS directo a Colombia")

    for nota in (deal.notes or [])[:2]:
        if "prime" in nota.lower() and "amazon" not in (deal.store or "").lower():
            continue
        lineas.append(f"• {nota}")

    if deal.url:
        cta = random.choice(SPINTAX_CTA_FOTO)
        lineas.append("")
        lineas.append(f"{cta} {deal.url}")

    return "\n".join(lineas)


def render_agrupado(elementos: list[Deal] | list[tuple[str, Deal]], base_url: str | None = None) -> str:
    """Genera el caption para un lote de ofertas en modo carrusel.
    
    Implementa la estructura de 3 bloques (Titular + Bajada + CTA) con rotación
    Spintax para máxima protección anti-ban y anti-spam en Facebook.
    """
    return spintax_post_carrusel()


def render_comentario_links(items: list[tuple[str, Deal]], base_url: str | None = None) -> str:
    """Construye el primer comentario con los enlaces oficiales directos a las tiendas."""
    emojis_num = ["1️⃣", "2️⃣", "3️⃣", "4️⃣"]
    lineas = ["🛍️ ENLACES DIRECTOS A LAS TIENDAS OFICIALES:", ""]

    for i, (hash_id, d) in enumerate(items[:4]):
        num = emojis_num[i] if i < len(emojis_num) else f"{i+1}."
        pct = round(d.discount_pct or 0)
        dcto = f" (-{pct}%)" if pct > 0 else ""
        tienda = d.store or d.source or "Tienda"
        url_tienda = d.url or f"{(base_url or DOMINIO_DEFAULT).rstrip('/')}/ir/{hash_id}"
        lineas.append(f"{num} {tienda}{dcto}:")
        lineas.append(f"👉 {url_tienda}")
        if d.coupons:
            lineas.append(f"🎟️ Cupón: {d.coupons[0]}")
        lineas.append("")

    canal_url = (getattr(config, "TELEGRAM_CHANNEL_URL", "") or "https://t.me/RadarPromoCol").strip().rstrip("/")
    canal_nombre = canal_url.split("/")[-1].lstrip("@")
    canal_handle = f"@{canal_nombre}" if canal_nombre else "@RadarPromoCol"
    lineas.append(f"⚡ ¿Quieres alertas de ofertas en vivo? Canal en Telegram: {canal_handle}")
    return "\n".join(lineas)


# Wrapper de compatibilidad hacia atrás
def render(deal: Deal, verdict: Verdict, landed: Landed | None = None,
           veracidad: Veracidad | None = None) -> str:
    """Función de render tradicional para compatibilidad."""
    return render_individual(deal, verdict, landed, veracidad)


# --- TELEMETRÍA Y CONTROL DE ESTADO ----------------------------------------
_TELEMETRIA: dict = {
    "publicados": 0,
    "fallidos": 0,
    "camuflajes": 0,
    "lotes": 0,
    "historias_publicadas": 0,
    "ultima_historia": None,
    "ultimo_exito": None,
    "ultimo_error": None,
    "ultima_oferta": None,
}


def telemetria() -> dict:
    """Devuelve las métricas de publicaciones e historias en Facebook."""
    datos = dict(_TELEMETRIA)
    try:
        with Store() as store:
            cola = store.obtener_cola_facebook(limite=100)
            datos["cola_pendientes"] = len(cola)
            datos["promos_desde_camuflaje"] = store.facebook_contador_promos()
            datos["historias"] = store.facebook_estado_historias()
    except Exception:
        datos["cola_pendientes"] = 0
        datos["promos_desde_camuflaje"] = 0
        datos["historias"] = {}
    return datos


def verificar_conexion() -> dict:
    """Valida en tiempo real que el token y la página tengan conexión activa con Graph API."""
    if not configurado():
        return {"ok": False, "error": "credenciales no configuradas"}
    page_id = config.FB_PAGE_ID
    token = _obtener_page_token()
    tok_info = {
        "longitud": len(token),
        "prefijo": token[:6] if len(token) >= 6 else "",
        "sufijo": token[-6:] if len(token) >= 6 else "",
    }
    url = f"https://graph.facebook.com/v20.0/{page_id}?fields=id,name&access_token={token}"
    req = urllib.request.Request(url)
    try:
        with urllib.request.urlopen(req, timeout=8) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            return {"ok": True, "nombre": data.get("name"), "id": data.get("id"), "token_info": tok_info}
    except urllib.error.HTTPError as exc:
        cuerpo = ""
        try:
            cuerpo = exc.read().decode("utf-8", errors="replace")
        except Exception:
            pass
        return {"ok": False, "codigo": exc.code, "error": cuerpo or exc.reason, "token_info": tok_info}
    except Exception as exc:
        return {"ok": False, "error": str(exc), "token_info": tok_info}


# --- SUBIDA DE MEDIOS Y PUBLICACIÓN GRAPH API ------------------------------

def subir_foto_oculta(url_imagen: str, deal: Deal | None = None, caption: str | None = None) -> str | None:
    """Sube una foto a Meta como 'published=false' para obtener media_fbid y adjuntarla en carrusel."""
    if not configurado():
        return None

    # Intentar subir la versión con branding si la oferta califica (modo solo texto para blindar Facebook)
    if deal is not None and branding.debe_aplicar_branding(deal):
        try:
            foto_bytes = branding.generar_tarjeta_branding_bytes(deal, solo_texto_tienda=True)
            if foto_bytes:
                photo_id = subir_foto_historia_binario(foto_bytes, caption=caption)
                if photo_id:
                    return photo_id
        except Exception as exc:
            print(f"  [facebook] aviso: error generando foto brandeada ({exc}), usando original")

    if not url_imagen or not url_imagen.startswith("http"):
        return None
    page_id = config.FB_PAGE_ID
    token = _obtener_page_token()
    url = f"https://graph.facebook.com/v20.0/{page_id}/photos"
    params = {
        "url": url_imagen,
        "published": "false",
        "temporary": "true",
        "access_token": token,
    }
    if caption:
        params["caption"] = caption
    data = urllib.parse.urlencode(params).encode("utf-8")
    req = urllib.request.Request(url, data=data)
    try:
        with urllib.request.urlopen(req, timeout=12) as resp:
            res = json.loads(resp.read().decode("utf-8"))
            return res.get("id")
    except Exception as exc:
        print(f"  [facebook] aviso: foto rechazada ({exc})")
        return None


def publicar_post_con_medios(texto: str, media_ids: list[str] | None = None) -> str | None:
    """Publica en el feed con 0 o más fotos adjuntas (attached_media) y devuelve post_id."""
    if not configurado():
        return None
    page_id = config.FB_PAGE_ID
    token = _obtener_page_token()
    url = f"https://graph.facebook.com/v20.0/{page_id}/feed"
    payload: dict = {
        "message": texto,
        "access_token": token,
    }
    if media_ids:
        payload["attached_media"] = json.dumps([{"media_fbid": m_id} for m_id in media_ids])

    data = urllib.parse.urlencode(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data)
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            res = json.loads(resp.read().decode("utf-8"))
            return res.get("id")
    except urllib.error.HTTPError as exc:
        cuerpo = ""
        try:
            cuerpo = exc.read().decode("utf-8", errors="replace")
        except Exception:
            pass
        mensaje_err = f"HTTP {exc.code}: {cuerpo or exc.reason}"
        print(f"  [facebook] error publicando post: {mensaje_err}")
        _TELEMETRIA["ultimo_error"] = mensaje_err
        return None
    except Exception as exc:
        mensaje_err = f"{type(exc).__name__}: {exc}"
        print(f"  [facebook] error publicando post: {mensaje_err}")
        _TELEMETRIA["ultimo_error"] = mensaje_err
        return None


def publicar_comentario(post_id: str, texto: str) -> str | None:
    """Publica el primer comentario con los enlaces cloaked bajo el post principal."""
    if not configurado() or not post_id or not texto:
        return None
    token = _obtener_page_token()
    url = f"https://graph.facebook.com/v20.0/{post_id}/comments"
    data = urllib.parse.urlencode({
        "message": texto,
        "access_token": token,
    }).encode("utf-8")
    req = urllib.request.Request(url, data=data)
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            res = json.loads(resp.read().decode("utf-8"))
            return res.get("id")
    except Exception as exc:
        print(f"  [facebook] error inyectando primer comentario: {exc}")
        return None


def publicar_camuflaje() -> bool:
    """Publica un post orgánico de interacción/comunidad 100% limpio (sin enlaces ni comentarios)."""
    if not configurado():
        return False
    texto = spintax_camuflaje()
    post_id = publicar_post_con_medios(texto)
    if post_id:
        print(f"  [facebook] post de camuflaje publicado ({post_id})")
        with Store() as store:
            store.facebook_resetear_camuflaje()
        _TELEMETRIA["camuflajes"] = _TELEMETRIA.get("camuflajes", 0) + 1
        _TELEMETRIA["ultimo_exito"] = dt.datetime.now(dt.timezone.utc).isoformat()
        return True
    return False


# --- COLA Y PROCESAMIENTO EN LOTES (CADA 45 MIN) ---------------------------

def encolar_oferta(deal: Deal, verdict: Verdict | None = None,
                   landed: Landed | None = None,
                   veracidad: Veracidad | None = None) -> str | None:
    """Encola una oferta en la base de datos para ser publicada en el siguiente lote de 45 min."""
    if not configurado():
        return None
    with Store() as store:
        h = store.encolar_facebook(deal)
        if h:
            print(f"  [facebook] oferta '{deal.title[:30]}...' encolada (hash={h})")
        else:
            print(f"  [facebook] cola llena (máx 4 pendientes), omitiendo para Facebook '{deal.title[:30]}...'")
        return h


def procesar_cola(limite: int = 4, base_url: str | None = None) -> dict:
    """Procesa la cola de Facebook cumpliendo la regla de camuflaje 5:1, agrupación y primer comentario."""
    if not configurado():
        return {"ok": False, "motivo": "facebook no configurado"}

    with Store() as store:
        contador_promos = store.facebook_contador_promos()

        # 1. Regla del Camuflaje (5 a 1):
        if contador_promos >= 5:
            ok_cam = publicar_camuflaje()
            return {
                "ok": ok_cam,
                "tipo": "camuflaje",
                "contador_previo": contador_promos,
            }

        # 2. Tomar hasta `limite` ofertas pendientes
        items = store.obtener_cola_facebook(limite=limite)
        if not items:
            return {"ok": True, "tipo": "cola_vacia", "items": 0}

        hash_ids = [it[0] for it in items]
        deals = [it[1] for it in items]

        # 3. Subir fotos a Meta con published=false y su caption descriptivo individual
        media_ids = []
        for hash_id, d in items:
            if d.image:
                cap = render_caption_foto(d, hash_id=hash_id, base_url=base_url)
                m_id = subir_foto_oculta(d.image, deal=d, caption=cap)
                if m_id:
                    media_ids.append(m_id)

        # 4. Construir caption principal con links cloaked protegidos de Render (/ir/{id})
        if len(deals) == 1:
            texto_post = render_individual(deals[0], hash_id=hash_ids[0], base_url=base_url)
        else:
            texto_post = render_agrupado(items, base_url=base_url)

        # 5. Publicar en feed con fotos adjuntas
        post_id = publicar_post_con_medios(texto_post, media_ids=media_ids)
        if not post_id:
            _TELEMETRIA["fallidos"] += 1
            return {
                "ok": False,
                "tipo": "error_publicacion",
                "error": _TELEMETRIA.get("ultimo_error"),
            }

        # 6. Inyectar inmediatamente el primer comentario con los links cloaked
        comentario_texto = render_comentario_links(items, base_url=base_url)
        com_id = publicar_comentario(post_id, comentario_texto)

        # 7. Actualizar la cola y el contador de camuflaje
        store.remover_de_cola_facebook(hash_ids)
        nuevo_contador = store.facebook_incrementar_promos()

        _TELEMETRIA["publicados"] += len(deals)
        _TELEMETRIA["lotes"] = _TELEMETRIA.get("lotes", 0) + 1
        _TELEMETRIA["ultimo_exito"] = dt.datetime.now(dt.timezone.utc).isoformat()
        _TELEMETRIA["ultima_oferta"] = deals[0].title[:40]

        print(f"  [facebook] lote de {len(deals)} ofertas publicado con éxito ({post_id}), comentario ({com_id})")
        return {
            "ok": True,
            "tipo": "lote" if len(deals) > 1 else "individual",
            "post_id": post_id,
            "comentario_id": com_id,
            "deals_count": len(deals),
            "promos_desde_camuflaje": nuevo_contador,
        }


def publicar_oferta(deal: Deal, verdict: Verdict, landed: Landed | None = None,
                     veracidad: Veracidad | None = None) -> bool:
    """Publica de forma directa con el protocolo de cero URLs en caption y primer comentario."""
    if not configurado():
        return False
    with Store() as store:
        h = store.encolar_facebook(deal)
    res = procesar_cola(limite=1)
    return bool(res.get("ok"))


# --- GENERACIÓN Y PUBLICACIÓN DE HISTORIAS (PAGE STORIES 9:16) ------------

def _obtener_fuente_historia(tamano: int) -> ImageFont.ImageFont | ImageFont.FreeTypeFont:
    """Busca fuentes compatibles en Linux (Render) y Windows con fallback seguro."""
    rutas = [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
        "/usr/share/fonts/truetype/freefont/FreeSansBold.ttf",
        "C:/Windows/Fonts/arialbd.ttf",
        "C:/Windows/Fonts/arial.ttf",
        "C:/Windows/Fonts/segoeuib.ttf",
        "arial.ttf",
    ]
    for r in rutas:
        try:
            return ImageFont.truetype(r, tamano)
        except Exception:
            continue
    try:
        return ImageFont.load_default(size=tamano)
    except Exception:
        return ImageFont.load_default()


def descargar_foto_producto(url_foto: str) -> Image.Image | None:
    """Descarga la imagen del producto a memoria RAM para procesarla."""
    if not url_foto or not url_foto.startswith("http"):
        return None
    try:
        req = urllib.request.Request(
            url_foto,
            headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
        )
        with urllib.request.urlopen(req, timeout=8) as resp:
            data = resp.read()
            return Image.open(io.BytesIO(data)).convert("RGBA")
    except Exception as exc:
        print(f"  [facebook] aviso: error descargando foto para historia ({exc})")
        return None


def es_ganga_para_historia(deal: Deal, verdict: Verdict | None = None) -> bool:
    """Determina si una oferta reúne los criterios para publicarse como Historia destacada."""
    # 1. Filtro local: Cero Slickdeals (tiendas gringas sin soporte local)
    fuente = (deal.source or "").lower().strip()
    if fuente == "slickdeals":
        return False

    # 2. Requiere imagen válida
    if not deal.image or not deal.image.startswith("http"):
        return False

    # 3. Descuento masivo (>= 50%) o error de precio (glitch)
    pct = round(deal.discount_pct or 0)
    es_glitch = bool(verdict and verdict.glitch) or getattr(deal, "es_glitch", False)
    if not es_glitch and pct < 50:
        return False

    # 4. Filtro de precio mínimo (evitar baratijas de poco impacto visual)
    if deal.currency == "COP" and deal.price and deal.price < 25000:
        return False

    return True


def generar_canvas_historia(deal: Deal, verdict: Verdict | None = None) -> bytes:
    """Genera en memoria un canvas vertical 9:16 (1080x1920) optimizado para Historias."""
    ancho, alto = 1080, 1920
    img = Image.new("RGB", (ancho, alto), (12, 18, 28))
    draw = ImageDraw.Draw(img)

    # 1. Fondo degradado vertical (de azul noche profundo a carbón oscuro)
    c_top = (15, 23, 42)
    c_bot = (7, 10, 19)
    for y in range(alto):
        r = int(c_top[0] + (c_bot[0] - c_top[0]) * (y / alto))
        g = int(c_top[1] + (c_bot[1] - c_top[1]) * (y / alto))
        b = int(c_top[2] + (c_bot[2] - c_top[2]) * (y / alto))
        draw.line([(0, y), (ancho, y)], fill=(r, g, b))

    # 2. Tipografías
    font_badge = _obtener_fuente_historia(42)
    font_tienda = _obtener_fuente_historia(34)
    font_titulo = _obtener_fuente_historia(40)
    font_antes = _obtener_fuente_historia(36)
    font_precio = _obtener_fuente_historia(64)
    font_cta = _obtener_fuente_historia(30)
    font_sub_cta = _obtener_fuente_historia(26)

    # 3. Cinta de alerta superior
    es_glitch = bool(verdict and verdict.glitch) or getattr(deal, "es_glitch", False)
    texto_alerta = "¡ERROR DE PRECIO DETECTADO!" if es_glitch else "¡SÚPER GANGA DEL DÍA!"
    color_alerta = (225, 29, 72) if es_glitch else (234, 88, 12)  # Rojo o Naranja intenso
    draw.rounded_rectangle([(90, 100), (990, 190)], radius=20, fill=color_alerta)
    draw.text((540, 145), texto_alerta, font=font_badge, fill=(255, 255, 255), anchor="mm")

    # 4. Tienda oficial
    tienda_nombre = (deal.store or deal.source or "Tienda").upper()
    draw.text((540, 240), f"TIENDA OFICIAL: {tienda_nombre}", font=font_tienda, fill=(251, 191, 36), anchor="mm")

    # 5. Tarjeta de producto (Blanca limpia)
    card_x1, card_y1, card_x2, card_y2 = 90, 310, 990, 1210
    draw.rounded_rectangle([(card_x1, card_y1), (card_x2, card_y2)], radius=30, fill=(255, 255, 255))

    # Descargar y montar la foto
    foto = descargar_foto_producto(deal.image)
    if foto:
        max_dim = 820
        ratio = min(max_dim / foto.width, max_dim / foto.height)
        new_w = max(1, int(foto.width * ratio))
        new_h = max(1, int(foto.height * ratio))
        foto_resized = foto.resize((new_w, new_h), Image.Resampling.LANCZOS)
        pos_x = card_x1 + (card_x2 - card_x1 - new_w) // 2
        pos_y = card_y1 + (card_y2 - card_y1 - new_h) // 2
        if foto_resized.mode == "RGBA":
            img.paste(foto_resized, (pos_x, pos_y), foto_resized)
        else:
            img.paste(foto_resized, (pos_x, pos_y))
    else:
        draw.text((540, 760), "[ FOTO OFERTA ]", font=font_badge, fill=(100, 116, 139), anchor="mm")

    # 6. Badge de descuento flotante superpuesto en la tarjeta
    pct = round(deal.discount_pct or 0)
    badge_texto = f"-{pct}% OFF" if pct > 0 else "GANGA"
    draw.rounded_rectangle([(700, 330), (970, 430)], radius=25, fill=(234, 179, 8))
    draw.text((835, 380), badge_texto, font=font_badge, fill=(0, 0, 0), anchor="mm")

    # 7. Título del producto truncado
    t_trunc = truncar(deal.title, 45)
    draw.text((540, 1270), t_trunc, font=font_titulo, fill=(255, 255, 255), anchor="mm")

    # 8. Precios
    if deal.list_price and deal.price and deal.list_price > deal.price:
        antes_txt = f"Antes: {money(deal.list_price, deal.currency)}"
        draw.text((540, 1360), antes_txt, font=font_antes, fill=(148, 163, 184), anchor="mm")
        draw.line([(320, 1360), (760, 1360)], fill=(239, 68, 68), width=4)

    ahora_txt = f"AHORA: {money(deal.price, deal.currency)}" if deal.price else "¡PRECIO ESPECIAL!"
    draw.text((540, 1460), ahora_txt, font=font_precio, fill=(34, 197, 94), anchor="mm")

    # 9. Línea divisoria decorativa
    draw.line([(140, 1600), (940, 1600)], fill=(51, 65, 85), width=2)

    # 10. Pie de llamado a la acción (CTA)
    draw.rounded_rectangle([(90, 1650), (990, 1790)], radius=25, fill=(30, 41, 59))
    draw.text((540, 1700), "LINK DE COMPRA EN NUESTRO ÚLTIMO POST", font=font_cta, fill=(255, 255, 255), anchor="mm")
    draw.text((540, 1745), "O toca nuestra foto de perfil para ver la ganga", font=font_sub_cta, fill=(203, 213, 225), anchor="mm")

    out = io.BytesIO()
    img.save(out, format="JPEG", quality=90)
    return out.getvalue()


def subir_foto_historia_binario(imagen_bytes: bytes, caption: str | None = None) -> str | None:
    """Sube el binario JPEG a /{page_id}/photos como published=false para obtener el photo_id."""
    if not configurado() or not imagen_bytes:
        return None
    page_id = config.FB_PAGE_ID
    token = _obtener_page_token()

    boundary = f"----WebKitFormBoundary{hashlib.md5(os.urandom(16)).hexdigest()}"
    cuerpo = bytearray()
    
    # Campo access_token
    cuerpo.extend(f"--{boundary}\r\n".encode("utf-8"))
    cuerpo.extend(b'Content-Disposition: form-data; name="access_token"\r\n\r\n')
    cuerpo.extend(token.encode("utf-8") + b"\r\n")
    
    # Campo published=false
    cuerpo.extend(f"--{boundary}\r\n".encode("utf-8"))
    cuerpo.extend(b'Content-Disposition: form-data; name="published"\r\n\r\n')
    cuerpo.extend(b"false\r\n")
    
    # Campo temporary=true
    cuerpo.extend(f"--{boundary}\r\n".encode("utf-8"))
    cuerpo.extend(b'Content-Disposition: form-data; name="temporary"\r\n\r\n')
    cuerpo.extend(b"true\r\n")

    # Campo caption opcional para descripciones individuales en carrusel
    if caption:
        cuerpo.extend(f"--{boundary}\r\n".encode("utf-8"))
        cuerpo.extend(b'Content-Disposition: form-data; name="caption"\r\n\r\n')
        cuerpo.extend(caption.encode("utf-8") + b"\r\n")
    
    # Campo source (binario de la foto)
    cuerpo.extend(f"--{boundary}\r\n".encode("utf-8"))
    cuerpo.extend(b'Content-Disposition: form-data; name="source"; filename="foto.jpg"\r\n')
    cuerpo.extend(b"Content-Type: image/jpeg\r\n\r\n")
    cuerpo.extend(imagen_bytes)
    cuerpo.extend(b"\r\n")
    cuerpo.extend(f"--{boundary}--\r\n".encode("utf-8"))

    headers = {
        "Content-Type": f"multipart/form-data; boundary={boundary}",
        "Content-Length": str(len(cuerpo)),
    }
    url = f"https://graph.facebook.com/v20.0/{page_id}/photos"
    req = urllib.request.Request(url, data=bytes(cuerpo), headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            res = json.loads(resp.read().decode("utf-8"))
            return res.get("id")
    except Exception as exc:
        print(f"  [facebook] error subiendo binario para historia: {exc}")
        return None


def publicar_historia_meta(photo_id: str) -> str | None:
    """Publica en las historias de la página de Facebook a partir del photo_id."""
    if not configurado() or not photo_id:
        return None
    page_id = config.FB_PAGE_ID
    token = _obtener_page_token()
    url = f"https://graph.facebook.com/v20.0/{page_id}/photo_stories"
    data = urllib.parse.urlencode({
        "photo_id": photo_id,
        "access_token": token,
    }).encode("utf-8")
    req = urllib.request.Request(url, data=data)
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            res = json.loads(resp.read().decode("utf-8"))
            return res.get("id") or res.get("post_id")
    except Exception as exc:
        print(f"  [facebook] error publicando historia en Meta: {exc}")
        return None


def publicar_historia(deal: Deal, verdict: Verdict | None = None,
                      store: Store | None = None,
                      forzar: bool = False) -> dict:
    """Publica una Historia de Facebook con plantilla 9:16 verificando cupo y criterios."""
    if not configurado():
        return {"ok": False, "motivo": "facebook no configurado"}

    # 1. Filtro de ganga
    if not forzar and not es_ganga_para_historia(deal, verdict):
        return {"ok": False, "motivo": "no_califica_ganga"}

    # 2. Control de cupo diario (máximo 2 por día, mínimo 4h de espaciado)
    propio_store = False
    if store is None:
        store = Store()
        propio_store = True

    try:
        if not forzar and not store.facebook_puede_publicar_historia():
            return {"ok": False, "motivo": "cupo_diario_o_espaciado"}

        # 3. Generar canvas
        img_bytes = generar_canvas_historia(deal, verdict)

        # 4. Subir foto binaria a Meta
        photo_id = subir_foto_historia_binario(img_bytes)
        if not photo_id:
            return {"ok": False, "motivo": "error_subida_foto"}

        # 5. Publicar en photo_stories
        story_id = publicar_historia_meta(photo_id)
        if not story_id:
            return {"ok": False, "motivo": "error_publicar_historia"}

        # 6. Registrar en BD y actualizar telemetría
        store.facebook_registrar_historia()
        _TELEMETRIA["historias_publicadas"] = _TELEMETRIA.get("historias_publicadas", 0) + 1
        _TELEMETRIA["ultima_historia"] = deal.title[:40]
        _TELEMETRIA["ultimo_exito"] = dt.datetime.now(dt.timezone.utc).isoformat()

        print(f"  [facebook] HISTORIA publicada con éxito (story_id={story_id}, oferta='{deal.title[:30]}...')")
        return {"ok": True, "story_id": story_id, "photo_id": photo_id}
    finally:
        if propio_store:
            store.close()

