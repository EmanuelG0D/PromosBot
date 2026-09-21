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
import json
import os
import random
import urllib.error
import urllib.parse
import urllib.request

import config
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

SPINTAX_CAMUFLAJE = [
    "☕ Buenos días cazadores de ofertas. ¿Qué producto están esperando que baje de precio esta semana? Cuéntennos en los comentarios 👇",
    "💡 Tip de Ahorro: Antes de comprar en línea, revisa siempre si la tienda tiene cupón de primera compra o descuento adicional por pagar con tarjeta débito o crédito específica.",
    "🛒 Pregunta para la comunidad: Si tuvieras que elegir una sola tienda para comprar tecnología con descuento en Colombia, ¿cuál elegirías?",
    "Verdad del comprador online: Llenar el carrito de compras a medianoche solo para ver cuánto sería el total y luego no comprar nada 😂 ¿A quién más le pasa?",
    "🎯 ¿Cuál ha sido la mejor ganga o error de precio que has logrado comprar en internet? ¡Déjanos tu historia en los comentarios!",
    "📱 ¿Prefieres comprar desde la aplicación móvil de las tiendas o directamente desde la página web en el computador? Déjanos tu opinión 👇",
]


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
    return random.choice(SPINTAX_CAMUFLAJE)


# --- RENDERIZADORES DE TEXTO (REGLA: CERO URLS EN EL POST PRINCIPAL) -------

def render_individual(deal: Deal, verdict: Verdict | None = None,
                      landed: Landed | None = None,
                      veracidad: Veracidad | None = None) -> str:
    """Genera el caption para una oferta individual SIN URLs (para máximo alcance en Meta)."""
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
    lineas.append(spintax_aviso_comentario())
    return "\n".join(lineas)


def render_agrupado(deals: list[Deal]) -> str:
    """Genera el caption para un lote de 2 a 4 ofertas agrupadas con descripciones truncadas a 60 chars."""
    encabezado = spintax_encabezado()
    lineas = [encabezado, ""]
    emojis_num = ["1️⃣", "2️⃣", "3️⃣", "4️⃣"]

    for i, d in enumerate(deals[:4]):
        num = emojis_num[i] if i < len(emojis_num) else f"{i+1}."
        pct = round(d.discount_pct or 0)
        dcto = f" (-{pct}%)" if pct > 0 else ""
        tienda = d.store or d.source or "Tienda"
        titulo_corto = truncar(d.title, 60)
        precio_str = money(d.price, d.currency)

        lineas.append(f"{num} {tienda}{dcto}")
        lineas.append(f"📦 {titulo_corto}")
        lineas.append(f"💵 Ahora: {precio_str}")
        if d.coupons:
            lineas.append(f"🎟️ Cupón: {d.coupons[0]}")
        lineas.append("")

    lineas.append(spintax_aviso_comentario())
    return "\n".join(lineas)


def render_comentario_links(items: list[tuple[str, Deal]], base_url: str | None = None) -> str:
    """Construye el primer comentario con los enlaces protegidos mediante link cloaking (/ir/{id})."""
    dominio = (base_url or DOMINIO_DEFAULT).rstrip("/")
    emojis_num = ["1️⃣", "2️⃣", "3️⃣", "4️⃣"]
    lineas = ["🛍️ ENLACES DIRECTOS A LAS TIENDAS OFICIALES:", ""]

    for i, (hash_id, d) in enumerate(items[:4]):
        num = emojis_num[i] if i < len(emojis_num) else f"{i+1}."
        pct = round(d.discount_pct or 0)
        dcto = f" (-{pct}%)" if pct > 0 else ""
        tienda = d.store or d.source or "Tienda"
        lineas.append(f"{num} {tienda}{dcto}:")
        lineas.append(f"👉 {dominio}/ir/{hash_id}")
        if d.coupons:
            lineas.append(f"🎟️ Cupón: {d.coupons[0]}")
        lineas.append("")

    lineas.append("⚡ Tócalos para ver fotos, cupón y comprar directo en la tienda oficial.")
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
    "ultimo_exito": None,
    "ultimo_error": None,
    "ultima_oferta": None,
}


def telemetria() -> dict:
    """Devuelve las métricas de publicaciones en Facebook."""
    datos = dict(_TELEMETRIA)
    try:
        with Store() as store:
            cola = store.obtener_cola_facebook(limite=100)
            datos["cola_pendientes"] = len(cola)
            datos["promos_desde_camuflaje"] = store.facebook_contador_promos()
    except Exception:
        datos["cola_pendientes"] = 0
        datos["promos_desde_camuflaje"] = 0
    return datos


def verificar_conexion() -> dict:
    """Valida en tiempo real que el token y la página tengan conexión activa con Graph API."""
    if not configurado():
        return {"ok": False, "error": "credenciales no configuradas"}
    page_id = config.FB_PAGE_ID
    token = config.FB_PAGE_ACCESS_TOKEN
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

def subir_foto_oculta(url_imagen: str) -> str | None:
    """Sube una foto a Meta como 'published=false' para obtener media_fbid y adjuntarla en carrusel."""
    if not configurado() or not url_imagen or not url_imagen.startswith("http"):
        return None
    page_id = config.FB_PAGE_ID
    token = config.FB_PAGE_ACCESS_TOKEN
    url = f"https://graph.facebook.com/v20.0/{page_id}/photos"
    data = urllib.parse.urlencode({
        "url": url_imagen,
        "published": "false",
        "temporary": "true",
        "access_token": token,
    }).encode("utf-8")
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
    token = config.FB_PAGE_ACCESS_TOKEN
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
    token = config.FB_PAGE_ACCESS_TOKEN
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
        print(f"  [facebook] oferta '{deal.title[:30]}...' encolada (hash={h})")
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

        # 3. Subir fotos a Meta con published=false
        media_ids = []
        for d in deals:
            if d.image:
                m_id = subir_foto_oculta(d.image)
                if m_id:
                    media_ids.append(m_id)

        # 4. Construir caption principal (CERO URLs)
        if len(deals) == 1:
            texto_post = render_individual(deals[0])
        else:
            texto_post = render_agrupado(deals)

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
