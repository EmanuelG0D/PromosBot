"""Publicación automática de ofertas en Página de Facebook vía Graph API."""
from __future__ import annotations

import datetime as dt
import json
import urllib.error
import urllib.parse
import urllib.request

import config
from core.landed import Landed
from core.models import Deal
from core.scoring import Verdict
from core.veracidad import Veracidad


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


def render(deal: Deal, verdict: Verdict, landed: Landed | None = None,
           veracidad: Veracidad | None = None) -> str:
    """Genera el texto formateado optimizado para el muro de Facebook."""
    lineas = []

    # Encabezado llamativo
    pct = round(deal.discount_pct or 0)
    descuento_str = f" (-{pct}%)" if pct > 0 else ""
    tienda = deal.store or "Tienda"

    if verdict.glitch or getattr(deal, "es_glitch", False):
        lineas.append(f"🚨 ¡ERROR DE PRECIO / SÚPER GANGA!{descuento_str} 🚨")
    elif pct >= 50:
        lineas.append(f"🔥 ¡OFERTÓN! {tienda}{descuento_str} 🔥")
    else:
        lineas.append(f"🏷️ {tienda}{descuento_str}")

    lineas.append("")
    lineas.append(deal.title.strip())
    lineas.append("")

    # Precios
    if deal.list_price and deal.price and deal.list_price > deal.price:
        lineas.append(f"💵 Antes: {money(deal.list_price, deal.currency)} ➡️ Ahora: {money(deal.price, deal.currency)}")
    elif deal.price:
        lineas.append(f"💵 Precio: {money(deal.price, deal.currency)}")

    # Cupones
    if deal.coupons:
        cupones = ", ".join(deal.coupons[:2])
        lineas.append(f"🎟️ Cupón de descuento: {cupones}")

    # Costo puesto en Colombia (si aplica importación)
    if landed is not None:
        es_directo = getattr(deal, "free_shipping_co", False) or (
            deal.country == "US" and "amazon" in deal.store.lower() and (deal.price or 0) >= 35
        )
        if es_directo:
            total_cop = round((deal.price or 0) * landed.trm)
            lineas.append(f"✈️🇨🇴 Envío GRATIS directo a Colombia (Total: {money(total_cop, 'COP')})")
        else:
            lineas.append(f"📦 Puesto en Colombia: ≈ {money(landed.total_cop, 'COP')} (TRM {money(landed.trm, 'COP')})")

    # Menciones de urgencia / vigencia
    if deal.expires_at:
        try:
            vence = dt.datetime.fromisoformat(deal.expires_at)
            horas = (vence - dt.datetime.now(dt.timezone.utc)).total_seconds() / 3600
            if 0 < horas <= 24:
                aviso = "menos de 1 hora" if horas < 1 else f"{round(horas)} horas"
                lineas.append(f"⏳ Termina en aprox. {aviso}")
        except Exception:
            pass

    # Notas relevantes
    es_amazon = "amazon" in (deal.store or "").lower()
    for nota in (deal.notes or [])[:2]:
        if not es_amazon and "prime" in nota.lower():
            continue
        lineas.append(f"• {nota}")

    # Enlace de compra a través del Bot de Telegram (Embudo de crecimiento)
    import hashlib
    deal_hash = hashlib.md5(deal.key.encode()).hexdigest()[:10]
    bot_user = getattr(config, "TELEGRAM_BOT_USERNAME", "PromosOn_bot")
    link_bot = f"https://t.me/{bot_user}?start=deal_{deal_hash}"

    lineas.append("👉 Consigue el link directo de compra y cupón aquí 👇")
    lineas.append(link_bot)
    lineas.append("(⚡ Nuestro radar te entrega la oferta verificada y el enlace oficial a la tienda)")
    lineas.append("")
    if config.TELEGRAM_CHANNEL_URL:
        lineas.append(f"📲 O únete al canal para alertas en vivo: {config.TELEGRAM_CHANNEL_URL}")

    return "\n".join(lineas)


_TELEMETRIA: dict = {
    "publicados": 0,
    "fallidos": 0,
    "ultimo_exito": None,
    "ultimo_error": None,
    "ultima_oferta": None,
}


def telemetria() -> dict:
    """Devuelve las métricas de publicaciones en Facebook."""
    return dict(_TELEMETRIA)


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


def publicar_oferta(deal: Deal, verdict: Verdict, landed: Landed | None = None,
                     veracidad: Veracidad | None = None) -> bool:
    """Publica la oferta en la Página de Facebook sin bloquear el bot."""
    if not configurado():
        return False

    texto = render(deal, verdict, landed, veracidad)
    page_id = config.FB_PAGE_ID
    token = config.FB_PAGE_ACCESS_TOKEN

    try:
        # Intentar publicar con foto si tiene URL de imagen
        if deal.image and deal.image.startswith("http"):
            url = f"https://graph.facebook.com/v20.0/{page_id}/photos"
            data = urllib.parse.urlencode({
                "url": deal.image,
                "caption": texto,
                "access_token": token,
            }).encode("utf-8")
            req = urllib.request.Request(url, data=data)
            try:
                resp = json.loads(urllib.request.urlopen(req, timeout=12).read().decode("utf-8"))
                if resp.get("id"):
                    print(f"  [facebook] oferta publicada con foto en muro ({resp['id']})")
                    _TELEMETRIA["publicados"] += 1
                    _TELEMETRIA["ultimo_exito"] = dt.datetime.now(dt.timezone.utc).isoformat()
                    _TELEMETRIA["ultima_oferta"] = deal.title[:40]
                    return True
            except urllib.error.HTTPError as e_img:
                cuerpo = ""
                try:
                    cuerpo = e_img.read().decode("utf-8", errors="replace")
                except Exception:
                    pass
                print(f"  [facebook] aviso: foto rechazada por CDN ({e_img.code}: {cuerpo or e_img.reason}), reintentando como post de feed...")
            except Exception as e_img:
                print(f"  [facebook] aviso: error al subir foto ({e_img}), reintentando como post de feed...")

        # Publicación en feed estándar
        url = f"https://graph.facebook.com/v20.0/{page_id}/feed"
        data = urllib.parse.urlencode({
            "message": texto,
            "access_token": token,
        }).encode("utf-8")
        req = urllib.request.Request(url, data=data)
        resp = json.loads(urllib.request.urlopen(req, timeout=10).read().decode("utf-8"))
        if resp.get("id"):
            print(f"  [facebook] oferta publicada en feed ({resp['id']})")
            _TELEMETRIA["publicados"] += 1
            _TELEMETRIA["ultimo_exito"] = dt.datetime.now(dt.timezone.utc).isoformat()
            _TELEMETRIA["ultima_oferta"] = deal.title[:40]
            return True
        return False

    except urllib.error.HTTPError as exc:
        cuerpo = ""
        try:
            cuerpo = exc.read().decode("utf-8", errors="replace")
        except Exception:
            pass
        mensaje_err = f"HTTP {exc.code}: {cuerpo or exc.reason}"
        print(f"  [facebook] no se pudo publicar oferta: {mensaje_err}")
        _TELEMETRIA["fallidos"] += 1
        _TELEMETRIA["ultimo_error"] = mensaje_err
        return False
    except Exception as exc:
        mensaje_err = f"{type(exc).__name__}: {exc}"
        print(f"  [facebook] no se pudo publicar oferta: {mensaje_err}")
        _TELEMETRIA["fallidos"] += 1
        _TELEMETRIA["ultimo_error"] = mensaje_err
        return False

