"""Motor de generación de plantillas gráficas de marca (1080x1080) para ofertas.

Aplica a todas las ofertas con imagen limpia (tiendas nacionales, PromoHunter y Promocajita).
Excluye automáticamente a DescuentosTech para preservar su formato nativo.
Respeta la moneda original de la oferta (USD o COP) sin conversiones forzadas.
Rota los colores dinámicamente entre Verde Neón, Azul Eléctrico y Naranja Fuego.
"""
from __future__ import annotations

import hashlib
import io
import math
import os
import urllib.request
from PIL import Image, ImageDraw, ImageFont

from core.models import Deal

PALETAS = {
    "verde": {
        "acento": (34, 197, 94),      # Verde neón esmeralda
        "badge": (15, 23, 20),        # Negro verdoso
        "estrella": (250, 204, 21),   # Dorado
    },
    "azul": {
        "acento": (14, 165, 233),     # Azul eléctrico / Cyan
        "badge": (11, 19, 36),        # Negro azulado profundo
        "estrella": (56, 189, 248),   # Celeste brillante
    },
    "naranja": {
        "acento": (249, 115, 22),     # Naranja fuego
        "badge": (24, 15, 12),        # Negro carbón cálido
        "estrella": (251, 191, 36),   # Amarillo ámbar
    },
}

NOMBRES_PALETAS = ("verde", "azul", "naranja")


def _obtener_fuente(tamano: int, bold: bool = True) -> ImageFont.ImageFont | ImageFont.FreeTypeFont:
    """Busca fuentes robustas compatibles en Windows y Linux (Render)."""
    candidatas = [
        "C:/Windows/Fonts/seguibl.ttf",
        "C:/Windows/Fonts/segoeuib.ttf" if bold else "C:/Windows/Fonts/segoeui.ttf",
        "C:/Windows/Fonts/arialbd.ttf" if bold else "C:/Windows/Fonts/arial.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf" if bold else "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf" if bold else "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
        "/usr/share/fonts/truetype/freefont/FreeSansBold.ttf",
        "arialbd.ttf" if bold else "arial.ttf",
    ]
    for ruta in candidatas:
        if os.path.exists(ruta):
            try:
                return ImageFont.truetype(ruta, tamano)
            except Exception:
                continue
    try:
        return ImageFont.load_default(size=tamano)
    except Exception:
        return ImageFont.load_default()


def _dibujar_estrella(draw: ImageDraw.ImageDraw, cx: float, cy: float, radio: float, color: tuple[int, int, int]) -> None:
    """Dibuja una estrella estilizada de 4 puntas idéntica a la referencia de diseño."""
    puntos = []
    for i in range(8):
        ang = i * math.pi / 4.0 - math.pi / 2.0
        r = radio if i % 2 == 0 else radio * 0.28
        puntos.append((cx + r * math.cos(ang), cy + r * math.sin(ang)))
    draw.polygon(puntos, fill=color)


def debe_aplicar_branding(deal: Deal) -> bool:
    """Determina si la oferta califica para generar la tarjeta gráfica con marco."""
    if not deal or not getattr(deal, "image", None) or not deal.image.startswith("http"):
        return False
    if not getattr(deal, "price", None) or deal.price <= 0:
        return False
    # Exclusión estricta de DescuentosTech (ya traen su propio marco nativo)
    fuente = (deal.source or "").strip().lower()
    tienda = (deal.store or "").strip().lower()
    if "descuentostech" in fuente or "descuentostech" in tienda:
        return False
    return True


def es_importacion_usa(deal: Deal) -> bool:
    """Determina si la oferta proviene de tiendas de Estados Unidos o Slickdeals."""
    if not deal:
        return False
    fuente = (deal.source or "").strip().lower()
    pais = (deal.country or "").strip().upper()
    return fuente == "slickdeals" or pais == "US"


def formatear_precio_branding(deal: Deal) -> tuple[str, str]:
    """Formatea el precio respetando la moneda original (USD o COP) y la etiqueta de importación."""
    moneda = (deal.currency or "COP").upper()
    precio = deal.price or 0.0
    es_usa = es_importacion_usa(deal)

    if moneda == "USD":
        if precio == int(precio):
            txt_precio = f"US$ {precio:,.0f}"
        else:
            txt_precio = f"US$ {precio:,.2f}"

        if es_usa:
            sub_nota = "*IMPORTACIÓN USA"
        elif getattr(deal, "free_shipping_co", False):
            sub_nota = "*ENVÍO GRATIS A COLOMBIA"
        else:
            sub_nota = "*OFERTA VERIFICADA"
    else:
        cop_val = round(precio)
        txt_precio = f"COP ${cop_val:,.0f}".replace(",", ".")
        if es_usa:
            sub_nota = "*IMPORTACIÓN USA"
        elif getattr(deal, "free_shipping_co", False):
            sub_nota = "*ENVÍO GRATIS DIRECTO"
        else:
            sub_nota = "*OFERTA VERIFICADA"

    return txt_precio, sub_nota



def descargar_foto_producto(url_foto: str, timeout: float = 4.0) -> Image.Image | None:
    """Descarga la imagen del producto a memoria RAM para procesarla."""
    if not url_foto or not url_foto.startswith("http"):
        return None
    try:
        req = urllib.request.Request(
            url_foto,
            headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = resp.read()
            return Image.open(io.BytesIO(data)).convert("RGBA")
    except Exception:
        return None


MAPA_LOGOS = {
    # Cadenas y Tiendas Principales
    "amazon": "assets/logos/amazon.png",
    "falabella": "assets/logos/falabella.png",
    "alkosto": "assets/logos/alkosto.png",
    "exito": "assets/logos/exito.png",
    "éxito": "assets/logos/exito.png",
    "mercadolibre": "assets/logos/mercadolibre.png",
    "mercado libre": "assets/logos/mercadolibre.png",
    "promocajita": "assets/logos/promocajita.png",
    "ktronix": "assets/logos/ktronix.png",
    "k-tronix": "assets/logos/ktronix.png",
    "alkomprar": "assets/logos/alkomprar.png",
    "homecenter": "assets/logos/homecenter.png",
    "jumbo": "assets/logos/jumbo.png",
    "carulla": "assets/logos/carulla.png",
    "olimpica": "assets/logos/olimpica.png",
    "olímpica": "assets/logos/olimpica.png",
    "ebay": "assets/logos/ebay.png",
    # Marcas Deportivas y Moda
    "puma": "assets/logos/puma.png",
    "adidas": "assets/logos/adidas.png",
    "nike": "assets/logos/nike.png",
    "koaj": "assets/logos/koaj.png",
    "totto": "assets/logos/totto.png",
    "arturocalle": "assets/logos/arturocalle.png",
    "arturo calle": "assets/logos/arturocalle.png",
    # Electrohogar
    "imusa": "assets/logos/imusa.png",
}


def _superponer_logo_tienda(canvas: Image.Image, tienda_str: str | None, color_acento: tuple[int, int, int]) -> None:
    """Superpone el logotipo gráfico oficial de la tienda dentro de una cápsula premium en la esquina superior derecha."""
    if not tienda_str:
        return
    tienda_norm = tienda_str.lower().strip()
    ruta_logo = None
    for k, ruta in MAPA_LOGOS.items():
        if k in tienda_norm:
            if os.path.exists(ruta):
                ruta_logo = ruta
                break
    if not ruta_logo:
        return

    try:
        logo_img = Image.open(ruta_logo).convert("RGBA")

        # Contenedor / Cápsula en esquina superior derecha
        box_w, box_h = 260, 96
        box_x = 1080 - box_w - 30
        box_y = 28

        card = Image.new("RGBA", (box_w, box_h), (0, 0, 0, 0))
        card_draw = ImageDraw.Draw(card)
        card_draw.rounded_rectangle(
            [(0, 0), (box_w - 1, box_h - 1)],
            radius=18,
            fill=(255, 255, 255, 252),
            outline=color_acento,
            width=3,
        )

        max_logo_w = box_w - 36
        max_logo_h = box_h - 24
        ratio = min(max_logo_w / logo_img.width, max_logo_h / logo_img.height)
        lw = max(1, int(logo_img.width * ratio))
        lh = max(1, int(logo_img.height * ratio))
        logo_resized = logo_img.resize((lw, lh), Image.Resampling.LANCZOS)

        lx = (box_w - lw) // 2
        ly = (box_h - lh) // 2
        card.paste(logo_resized, (lx, ly), logo_resized)

        canvas.paste(card, (box_x, box_y), card)
    except Exception as exc:
        print(f"  [branding] aviso: no se pudo superponer logo de tienda ({exc})")


def _superponer_tienda_texto(canvas: Image.Image, tienda_str: str, color_acento: tuple[int, int, int]) -> None:
    """Superpone el nombre de la tienda en texto dentro de una cápsula premium (sin logos gráficos, 100% seguro para Facebook)."""
    texto = tienda_str.strip().upper()
    if not texto:
        return

    f_tienda = _obtener_fuente(28, bold=True)

    temp_img = Image.new("RGBA", (10, 10))
    temp_draw = ImageDraw.Draw(temp_img)
    try:
        bbox = temp_draw.textbbox((0, 0), texto, font=f_tienda)
        tw = bbox[2] - bbox[0]
    except Exception:
        tw = len(texto) * 16

    pad_x = 24
    box_w = max(200, tw + pad_x * 2)
    box_h = 76
    box_x = 1080 - box_w - 30
    box_y = 28

    card = Image.new("RGBA", (box_w, box_h), (0, 0, 0, 0))
    card_draw = ImageDraw.Draw(card)
    card_draw.rounded_rectangle(
        [(0, 0), (box_w - 1, box_h - 1)],
        radius=16,
        fill=(255, 255, 255, 252),
        outline=color_acento,
        width=3,
    )

    card_draw.text((box_w // 2, box_h // 2), texto, font=f_tienda, fill=(15, 23, 42), anchor="mm")
    canvas.paste(card, (box_x, box_y), card)


def componer_canvas_branding(
    img_producto: Image.Image,
    precio_str: str,
    nota_sub_precio: str = "*OFERTA VERIFICADA",
    paleta_nombre: str = "verde",
    tienda_str: str | None = None,
    es_importacion_usa: bool = False,
    solo_texto_tienda: bool = False,
) -> Image.Image:
    """Compone la imagen final cuadrada 1080x1080 con marco, badges y logotipo compacto."""
    W, H = 1080, 1080
    canvas = Image.new("RGB", (W, H), (255, 255, 255))
    draw = ImageDraw.Draw(canvas)

    colores = PALETAS.get(paleta_nombre, PALETAS["verde"])
    color_acento = colores["acento"]
    color_badge = colores["badge"]
    color_estrella = colores["estrella"]

    # 1. Centrar producto sobre fondo blanco (área útil: 800x620)
    max_w, max_h = 800, 620
    ratio = min(max_w / img_producto.width, max_h / img_producto.height)
    new_w = max(1, int(img_producto.width * ratio))
    new_h = max(1, int(img_producto.height * ratio))
    prod_resized = img_producto.resize((new_w, new_h), Image.Resampling.LANCZOS)

    pos_x = (W - new_w) // 2
    pos_y = 170 + (max_h - new_h) // 2

    if prod_resized.mode == "RGBA":
        canvas.paste(prod_resized, (pos_x, pos_y), prod_resized)
    else:
        canvas.paste(prod_resized, (pos_x, pos_y))

    # 2. Marco perimetral decorativo
    b_th = 12
    draw.line([(0, b_th // 2), (W, b_th // 2)], fill=color_acento, width=b_th)
    draw.line([(0, H - b_th // 2), (W, H - b_th // 2)], fill=color_acento, width=b_th)
    draw.line([(b_th // 2, 0), (b_th // 2, H)], fill=color_acento, width=b_th)
    draw.line([(W - b_th // 2, 0), (W - b_th // 2, H)], fill=color_acento, width=b_th)

    # 3. Badge Superior Izquierdo: "POR SÓLO"
    pts_top = [(0, 0), (330, 0), (280, 150), (0, 180)]
    draw.polygon(pts_top, fill=color_badge)
    draw.line(pts_top + [pts_top[0]], fill=color_acento, width=7)

    _dibujar_estrella(draw, 45, 120, 24, color_estrella)
    _dibujar_estrella(draw, 275, 45, 24, color_estrella)

    f_porsolo = _obtener_fuente(36, bold=True)
    draw.text((155, 80), "POR SÓLO", font=f_porsolo, fill=(255, 255, 255), anchor="mm")

    # 4. Badge Inferior Izquierdo: Branding "RADAR PROMOS" (compacto y sin enlace)
    pts_brand = [(0, 930), (280, 930), (320, 1080), (0, 1080)]
    draw.polygon(pts_brand, fill=color_badge)
    draw.line(pts_brand + [pts_brand[0]], fill=color_acento, width=6)

    f_b1 = _obtener_fuente(26, bold=True)
    f_b2 = _obtener_fuente(40, bold=True)
    draw.text((30, 960), "RADAR", font=f_b1, fill=color_acento)
    draw.text((30, 995), "PROMOS", font=f_b2, fill=(255, 255, 255))

    # 5. Badge Inferior Derecho: PRECIO DESTACADO
    shadow_offset = 6
    pts_price_s = [
        (430 + shadow_offset, 810 + shadow_offset),
        (990 + shadow_offset, 810 + shadow_offset),
        (960 + shadow_offset, 930 + shadow_offset),
        (400 + shadow_offset, 930 + shadow_offset),
    ]
    draw.polygon(pts_price_s, fill=(200, 200, 200))

    pts_price = [(430, 810), (990, 810), (960, 930), (400, 930)]
    draw.polygon(pts_price, fill=color_badge)
    draw.line(pts_price + [pts_price[0]], fill=(30, 30, 30), width=4)

    f_precio = _obtener_fuente(64, bold=True)
    draw.text((690, 870), precio_str, font=f_precio, fill=(255, 255, 255), anchor="mm")

    # Sub-badge debajo del precio (con soporte para bandera USA en importaciones)
    pts_sub = [(610, 940), (990, 940), (975, 1000), (595, 1000)]
    draw.polygon(pts_sub, fill=(15, 15, 15))
    draw.line(pts_sub + [pts_sub[0]], fill=color_acento, width=3)

    f_sub = _obtener_fuente(22, bold=True)
    ruta_bandera = "assets/logos/bandera_usa.png"
    if es_importacion_usa and os.path.exists(ruta_bandera):
        try:
            bandera = Image.open(ruta_bandera).convert("RGBA")
            bw, bh = 32, 20
            bandera_res = bandera.resize((bw, bh), Image.Resampling.LANCZOS)
            canvas.paste(bandera_res, (640, 960), bandera_res)
            draw.rectangle([(640, 960), (640 + bw - 1, 960 + bh - 1)], outline=(200, 200, 200), width=1)
            draw.text((682, 970), nota_sub_precio, font=f_sub, fill=(255, 255, 255), anchor="lm")
        except Exception:
            draw.text((795, 970), nota_sub_precio, font=f_sub, fill=(255, 255, 255), anchor="mm")
    else:
        draw.text((800, 970), nota_sub_precio, font=f_sub, fill=(255, 255, 255), anchor="mm")

    # 6. Identificación de la tienda en esquina superior derecha
    if tienda_str:
        if solo_texto_tienda:
            _superponer_tienda_texto(canvas, tienda_str, color_acento)
        else:
            _superponer_logo_tienda(canvas, tienda_str, color_acento)

    return canvas


def generar_tarjeta_branding_bytes(
    deal: Deal,
    paleta: str | None = None,
    timeout: float = 4.0,
    solo_texto_tienda: bool = False,
) -> bytes | None:
    """Genera la imagen JPEG en memoria RAM con marco, precio y branding. Devuelve bytes o None."""
    if not debe_aplicar_branding(deal):
        return None

    try:
        foto = descargar_foto_producto(deal.image, timeout=timeout)
        if not foto:
            return None

        # Selección de color: si no se especifica, se rota de forma determinista mediante el hash de la oferta
        if not paleta:
            idx = int(hashlib.md5((deal.key or deal.url or deal.title).encode("utf-8")).hexdigest(), 16) % len(NOMBRES_PALETAS)
            paleta = NOMBRES_PALETAS[idx]

        precio_str, nota_sub = formatear_precio_branding(deal)
        es_usa = es_importacion_usa(deal)
        tienda_val = getattr(deal, "store", None) or getattr(deal, "source", None)
        canvas = componer_canvas_branding(
            foto,
            precio_str,
            nota_sub,
            paleta_nombre=paleta,
            tienda_str=tienda_val,
            es_importacion_usa=es_usa,
            solo_texto_tienda=solo_texto_tienda,
        )

        buf = io.BytesIO()
        canvas.save(buf, format="JPEG", quality=95)
        return buf.getvalue()
    except Exception as exc:
        print(f"  [branding] aviso: no se pudo generar tarjeta con marco ({exc})")
        return None


_CACHE_FOTOS_BYTES: dict[str, bytes] = {}
_CACHE_FOTOS_MAX: int = 50


def guardar_foto_cache(hash_id: str, foto_bytes: bytes) -> None:
    """Almacena en memoria RAM la imagen JPEG pregenerada para entrega instantánea a Facebook (< 5ms)."""
    if not hash_id or not foto_bytes:
        return
    _CACHE_FOTOS_BYTES[hash_id] = foto_bytes
    if len(_CACHE_FOTOS_BYTES) > _CACHE_FOTOS_MAX:
        primer_key = next(iter(_CACHE_FOTOS_BYTES))
        _CACHE_FOTOS_BYTES.pop(primer_key, None)


def obtener_foto_cache(hash_id: str) -> bytes | None:
    """Recupera la imagen JPEG pregenerada de la memoria RAM."""
    return _CACHE_FOTOS_BYTES.get(hash_id)


_BANNER_FALLBACK_CACHE: bytes | None = None


def generar_banner_fallback_bytes() -> bytes:
    """Genera y cachea un banner oficial JPEG 1080x1080 para garantizar que Facebook siempre reciba una imagen válida."""
    global _BANNER_FALLBACK_CACHE
    if _BANNER_FALLBACK_CACHE is not None:
        return _BANNER_FALLBACK_CACHE

    try:
        W, H = 1080, 1080
        canvas = Image.new("RGB", (W, H), (15, 23, 42))
        draw = ImageDraw.Draw(canvas)

        color_acento = (34, 197, 94)    # Verde neón esmeralda
        color_badge = (11, 19, 36)      # Negro azulado profundo
        color_estrella = (250, 204, 21) # Dorado

        # Marco decorativo
        b_th = 14
        draw.rectangle([(b_th // 2, b_th // 2), (W - b_th // 2, H - b_th // 2)], outline=color_acento, width=b_th)

        # Estrellas de soporte
        _dibujar_estrella(draw, 120, 120, 28, color_estrella)
        _dibujar_estrella(draw, 960, 120, 28, color_estrella)
        _dibujar_estrella(draw, 120, 960, 28, color_estrella)
        _dibujar_estrella(draw, 960, 960, 28, color_estrella)

        # Badge central premium
        pts_centro = [(140, 260), (940, 260), (900, 820), (180, 820)]
        draw.polygon(pts_centro, fill=color_badge)
        draw.line(pts_centro + [pts_centro[0]], fill=color_acento, width=6)

        f_tit = _obtener_fuente(54, bold=True)
        f_sub = _obtener_fuente(34, bold=True)
        f_desc = _obtener_fuente(26, bold=False)

        draw.text((W // 2, 380), "RADAR PROMOS", font=f_tit, fill=color_acento, anchor="mm")
        draw.text((W // 2, 450), "COLOMBIA", font=f_tit, fill=(255, 255, 255), anchor="mm")

        draw.line([(300, 520), (780, 520)], fill=(71, 85, 105), width=3)

        draw.text((W // 2, 590), "🔥 OFERTA DESTACADA 🔥", font=f_sub, fill=color_estrella, anchor="mm")
        draw.text((W // 2, 670), "Revisa los detalles y el enlace oficial", font=f_desc, fill=(241, 245, 249), anchor="mm")
        draw.text((W // 2, 720), "de compra directa en esta publicación.", font=f_desc, fill=(148, 163, 184), anchor="mm")

        buf = io.BytesIO()
        canvas.save(buf, format="JPEG", quality=90)
        _BANNER_FALLBACK_CACHE = buf.getvalue()
        return _BANNER_FALLBACK_CACHE
    except Exception as exc:
        print(f"  [branding] error generando banner fallback ({exc})")
        # Fallback mínimo de emergencia en blanco con borde verde
        emergencia = Image.new("RGB", (800, 800), (20, 30, 45))
        draw_em = ImageDraw.Draw(emergencia)
        draw_em.rectangle([(10, 10), (790, 790)], outline=(34, 197, 94), width=10)
        buf_em = io.BytesIO()
        emergencia.save(buf_em, format="JPEG", quality=85)
        _BANNER_FALLBACK_CACHE = buf_em.getvalue()
        return _BANNER_FALLBACK_CACHE



