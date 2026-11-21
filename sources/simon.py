"""Simon Premium Outlets (EE. UU.): agregador de ofertas y liquidaciones outlet (USD).

Lee el catálogo directamente desde la plataforma Shopify de shop.simon.com a través
de sus endpoints públicos de colecciones y búsqueda predictiva:
- /collections/{handle}/products.json (liquidaciones, price drops y tiendas oficiales)
- /search/suggest.json?q={query}&resources[type]=product (búsqueda por término)

Filtra productos con descuento comprobable (list_price > price), porcentaje mínimo,
tope de precio máximo en USD (por defecto máx $40 USD para gangas imbatibles) y
filtra exclusivamente marcas reconocidas y populares (Adidas, Puma, Nike, Under Armour,
Reebok, Tommy Hilfiger, Calvin Klein, Coach, Levi's, Crocs, Clarks, Nautica, etc.),
descartando marcas genéricas o desconocidas de marketplace.
"""
from __future__ import annotations

import json
import re
import urllib.parse
from concurrent.futures import ThreadPoolExecutor

from core import http
from core.models import Deal

BASE_URL = "https://shop.simon.com"
STORE_NAME = "Simon Outlet"
COUNTRY = "US"
CURRENCY = "USD"
HILOS = 6

# Marcas reconocidas / oficiales con sus insignias (badges) específicas
MARCAS_TOP: dict[str, dict[str, str]] = {
    # Calzado y deportivo
    "adidas": {"nombre": "Adidas", "badge": "👟 Adidas Outlet"},
    "puma": {"nombre": "Puma", "badge": "🐆 Puma Outlet"},
    "nike": {"nombre": "Nike", "badge": "✔️ Nike Outlet"},
    "jordan": {"nombre": "Jordan", "badge": "🏀 Jordan Outlet"},
    "under armour": {"nombre": "Under Armour", "badge": "🛡️ Under Armour"},
    "reebok": {"nombre": "Reebok", "badge": "⚡ Reebok Outlet"},
    "new balance": {"nombre": "New Balance", "badge": "👟 New Balance"},
    "asics": {"nombre": "Asics", "badge": "🏃 Asics"},
    "converse": {"nombre": "Converse", "badge": "⭐ Converse"},
    "vans": {"nombre": "Vans", "badge": "🛹 Vans"},
    "skechers": {"nombre": "Skechers", "badge": "👟 Skechers"},
    "champion": {"nombre": "Champion", "badge": "🏆 Champion"},
    "brooks": {"nombre": "Brooks", "badge": "🏃 Brooks"},
    "saucony": {"nombre": "Saucony", "badge": "🏃 Saucony"},
    "fila": {"nombre": "Fila", "badge": "👟 Fila"},
    "on running": {"nombre": "On Running", "badge": "👟 On Running"},
    "hoka": {"nombre": "Hoka", "badge": "👟 Hoka"},

    # Calzado casual / lifestyle
    "clarks": {"nombre": "Clarks", "badge": "👞 Clarks Outlet"},
    "crocs": {"nombre": "Crocs", "badge": "🐊 Crocs Outlet"},
    "timberland": {"nombre": "Timberland", "badge": "🥾 Timberland"},
    "steve madden": {"nombre": "Steve Madden", "badge": "👠 Steve Madden"},
    "birkenstock": {"nombre": "Birkenstock", "badge": "👡 Birkenstock"},
    "dr. martens": {"nombre": "Dr. Martens", "badge": "🥾 Dr. Martens"},
    "doc martens": {"nombre": "Dr. Martens", "badge": "🥾 Dr. Martens"},
    "ugg": {"nombre": "UGG", "badge": "🥾 UGG"},
    "aldo": {"nombre": "Aldo", "badge": "👞 Aldo"},
    "cole haan": {"nombre": "Cole Haan", "badge": "👞 Cole Haan"},
    "sperry": {"nombre": "Sperry", "badge": "👞 Sperry"},

    # Ropa casual y moda urbana
    "tommy hilfiger": {"nombre": "Tommy Hilfiger", "badge": "🔴 Tommy Hilfiger"},
    "calvin klein": {"nombre": "Calvin Klein", "badge": "🖤 Calvin Klein"},
    "levi's": {"nombre": "Levi's", "badge": "👖 Levi's"},
    "levis": {"nombre": "Levi's", "badge": "👖 Levi's"},
    "dockers": {"nombre": "Dockers", "badge": "👖 Dockers"},
    "aeropostale": {"nombre": "Aéropostale", "badge": "👕 Aéropostale"},
    "nautica": {"nombre": "Nautica", "badge": "⛵ Nautica"},
    "columbia": {"nombre": "Columbia", "badge": "🏔️ Columbia"},
    "the north face": {"nombre": "The North Face", "badge": "🧗 The North Face"},
    "north face": {"nombre": "The North Face", "badge": "🧗 The North Face"},
    "gap": {"nombre": "Gap", "badge": "👕 Gap"},
    "banana republic": {"nombre": "Banana Republic", "badge": "👔 Banana Republic"},
    "american eagle": {"nombre": "American Eagle", "badge": "🦅 American Eagle"},
    "hollister": {"nombre": "Hollister", "badge": "🌊 Hollister"},
    "abercrombie": {"nombre": "Abercrombie", "badge": "🦌 Abercrombie"},
    "diesel": {"nombre": "Diesel", "badge": "🔥 Diesel Outlet"},
    "hugo boss": {"nombre": "Hugo Boss", "badge": "👔 Hugo Boss"},
    "boss": {"nombre": "Hugo Boss", "badge": "👔 Hugo Boss"},
    "lacoste": {"nombre": "Lacoste", "badge": "🐊 Lacoste"},
    "polo ralph lauren": {"nombre": "Ralph Lauren", "badge": "🏇 Ralph Lauren"},
    "ralph lauren": {"nombre": "Ralph Lauren", "badge": "🏇 Ralph Lauren"},

    # Bolsos, accesorios y relojes
    "coach": {"nombre": "Coach", "badge": "👜 Coach Outlet"},
    "michael kors": {"nombre": "Michael Kors", "badge": "👜 Michael Kors"},
    "kate spade": {"nombre": "Kate Spade", "badge": "♠️ Kate Spade"},
    "guess": {"nombre": "Guess", "badge": "❓ Guess Outlet"},
    "fossil": {"nombre": "Fossil", "badge": "⌚ Fossil Outlet"},
    "ray-ban": {"nombre": "Ray-Ban", "badge": "🕶️ Ray-Ban"},
    "ray ban": {"nombre": "Ray-Ban", "badge": "🕶️ Ray-Ban"},
    "oakley": {"nombre": "Oakley", "badge": "🕶️ Oakley"},
    "kenneth cole": {"nombre": "Kenneth Cole", "badge": "👞 Kenneth Cole"},
    "dkny": {"nombre": "DKNY", "badge": "🗽 DKNY"},
}

MARCAS_ORDENADAS = sorted(MARCAS_TOP.keys(), key=len, reverse=True)


def detectar_marca(title: str, vendor: str = "", tags: list[str] | str | None = None) -> tuple[str, str] | None:
    """Identifica si el producto pertenece a una marca top/reconocida.

    Retorna (nombre_marca, badge) o None si es una marca genérica / desconocida.
    """
    if isinstance(tags, str):
        tags_list = [x.strip() for x in tags.split(",") if x.strip()]
    elif isinstance(tags, list):
        tags_list = tags
    else:
        tags_list = []

    # 1. Tags de Shopify con prefijo brand_ o brand:
    for t in tags_list:
        t_low = t.lower()
        if t_low.startswith("brand_") or t_low.startswith("brand:"):
            b_val = t_low.replace("brand_", "").replace("brand:", "").strip()
            for m in MARCAS_ORDENADAS:
                if m == b_val or b_val.startswith(m) or f" {m} " in f" {b_val} ":
                    meta = MARCAS_TOP[m]
                    return meta["nombre"], meta["badge"]

    # 2. Fabricante / Vendor (si no es puramente numérico)
    v_low = vendor.strip().lower()
    if v_low and not v_low.isdigit():
        for m in MARCAS_ORDENADAS:
            if re.search(rf"\b{re.escape(m)}\b", v_low):
                meta = MARCAS_TOP[m]
                return meta["nombre"], meta["badge"]

    # 3. Título del producto
    t_low = title.lower()
    for m in MARCAS_ORDENADAS:
        if re.search(rf"\b{re.escape(m)}\b", t_low):
            meta = MARCAS_TOP[m]
            return meta["nombre"], meta["badge"]

    return None


# Expresiones regulares para filtrar estrictamente Zapatos / Tenis y Bolsos / Morrales
CALZADO_RE = re.compile(
    r'\b(shoes?|sneakers?|boots?|sandals?|slides?|slippers?|clogs?|footwear|loafers?|cleats?|trainers?|streettalk|slip-on|flats?|mules?|heels?|pumps?|air force|air max|ultraboost|stan smith|superstar|gazelle|samba|chuck taylor|old skool|classic leather|pegasus|air zoom)\b',
    re.IGNORECASE
)
BOLSOS_RE = re.compile(
    r'\b(bags?|backpacks?|morral(es)?|totes?|crossbod(y|ies)|shoulder bag|duffels?|duffles?|wallets?|wristlets?|handbags?|satchels?|clutch(es)?|pouches?|card case|luggage|messenger bag|fanny pack|waist bag|sling bag)\b',
    re.IGNORECASE
)
ROPA_VETO_RE = re.compile(
    r'\b(dress|dresses|skirt|skirts|jacket|jackets|coat|coats|vest|vests|hoodie|hoodies|sweater|sweaters|sweatshirt|sweatshirts|shirt|shirts|t-shirt|t-shirts|tee|tees|polo|polos|pants|trousers|jeans|shorts|leggings|tights|bra|bras|underwear|boxer|boxers|panties|swimsuit|bikini|belt|belts|hat|hats|cap|caps|beanie|beanies|socks|watch|watches|sunglasses|perfume|fragrance)\b',
    re.IGNORECASE
)


def es_calzado_o_bolso(title: str, product_type: str = "", tags: list[str] | str | None = None) -> bool:
    """Valida que el producto sea estrictamente calzado/tenis o bolso/morral/cartera."""
    ptype = (product_type or "").lower()

    # 1. Por product_type oficial de Shopify
    if any(k in ptype for k in ("shoes", "footwear", "boots", "sandals", "sneakers", "slides", "slippers", "clogs")):
        return True
    if any(k in ptype for k in ("handbags", "bags", "backpacks", "wallets", "wristlets", "totes", "crossbody", "satchels", "clutches", "luggage", "card-cases")):
        return True
    if any(k in ptype for k in ("clothing", "underwear", "socks", "shirts", "tops", "dresses", "pants", "jeans", "shorts", "outerwear", "jackets", "coats", "hoodies", "sweatshirts", "jewelry", "watches", "eyewear", "belts", "hats")):
        if not (CALZADO_RE.search(title) or BOLSOS_RE.search(title)):
            return False

    # 2. Por título
    es_calz = bool(CALZADO_RE.search(title))
    es_bols = bool(BOLSOS_RE.search(title))
    es_ropa = bool(ROPA_VETO_RE.search(title))

    if es_calz or es_bols:
        if not es_ropa:
            return True
        if es_calz:
            return True
        if any(w in title.lower() for w in ("backpack", "handbag", "crossbody", "wristlet", "wallet", "duffel", "tote bag", "shoulder bag")):
            return True

    return False


COLECCIONES_DEFECTO = [
    # Adidas calzado y liquidaciones
    "store-adidas",
    "curated-adidas-price-drops",
    "curated-adidas-sale-top-picks",
    # Puma calzado y bolsos
    "store-puma",
    # Nike calzado
    "store-nike",
    "curated-nike-top-deals",
    # Marcas top de calzado
    "store-reebok",
    "curated-reebok-sale",
    "store-new-balance",
    "store-under-armour",
    "curated-under-armour-sale",
    "store-clarks-shoes",
    "curated-clarks-extra-40-off",
    "store-crocs",
    # Bolsos, morrales y carteras de marca
    "store-coach-outlet",
    "curated-coach-outlet-sale",
    "store-michael-kors-outlet",
    "store-kate-spade",
    "store-guess-factory",
]


def extraer_deals_productos_json(
    raw_json: str | dict,
    min_descuento: float = 30.0,
    max_usd: float | None = 40.0,
    solo_marcas_top: bool = True,
    solo_calzado_y_bolsos: bool = True,
) -> list[Deal]:
    """Parsea el listado de productos de una colección de Shopify (products.json)."""
    deals: list[Deal] = []
    try:
        data = json.loads(raw_json) if isinstance(raw_json, str) else raw_json
    except Exception:
        return []

    if not isinstance(data, dict):
        return []

    products = data.get("products") or []
    for p in products:
        try:
            pid = str(p.get("id") or "").strip()
            title = (p.get("title") or "").strip()
            handle = (p.get("handle") or "").strip()
            vendor = (p.get("vendor") or "").strip()
            ptype = (p.get("product_type") or "").strip()
            tags = p.get("tags") or []
            if not pid or not title:
                continue

            # Filtro exclusivo de calzado/tenis y bolsos/morrales
            if solo_calzado_y_bolsos and not es_calzado_o_bolso(title, product_type=ptype, tags=tags):
                continue

            # Filtro de marca: omitir marcas desconocidas / genéricas si solo_marcas_top=True
            info_marca = detectar_marca(title, vendor=vendor, tags=tags)
            if solo_marcas_top and not info_marca:
                continue

            variants = p.get("variants") or []
            if not variants:
                continue

            # Buscar la variante disponible con mejor descuento que cumpla con max_usd
            mejor_v = None
            max_disc_pct = 0.0
            for v in variants:
                if not v.get("available", True):
                    continue
                pr = float(v.get("price") or 0)
                cp = float(v.get("compare_at_price") or 0)
                if max_usd is not None and pr > max_usd:
                    continue
                if cp > pr and pr > 0:
                    disc_pct = round(((cp - pr) / cp) * 100)
                    if disc_pct > max_disc_pct:
                        max_disc_pct = disc_pct
                        mejor_v = (pr, cp, disc_pct)

            if not mejor_v or max_disc_pct < min_descuento:
                continue

            price, list_price, pct = mejor_v
            p_url = f"{BASE_URL}/products/{handle}" if handle else BASE_URL
            images = p.get("images") or []
            img_url = images[0].get("src") if images else None

            notes = []
            if info_marca:
                notes.append(info_marca[1])
            elif vendor and not vendor.isdigit() and vendor.lower() not in title.lower():
                notes.append(f"🏷️ {vendor}")

            if CALZADO_RE.search(title) or any(k in ptype.lower() for k in ("shoes", "footwear", "boots", "sandals", "sneakers", "slides")):
                notes.append("👟 Calzado y Tenis")
            elif BOLSOS_RE.search(title) or any(k in ptype.lower() for k in ("handbags", "bags", "backpacks", "wallets", "wristlets")):
                notes.append("🎒 Bolsos y Morrales")

            notes.append(f"-{pct}% OFF")
            if pct >= 50:
                notes.append("🔥 50%+ EXTRA OFF")
            notes.append(f"🇺🇸 {STORE_NAME}")

            deals.append(Deal(
                source="simon",
                store=STORE_NAME,
                country=COUNTRY,
                key=f"simon:{pid}",
                title=title,
                url=p_url,
                price=price,
                currency=CURRENCY,
                list_price=list_price,
                image=img_url,
                notes=notes,
                free_shipping_co=False,
                in_stock=True,
            ))
        except Exception:
            continue

    return deals


def extraer_deals_suggest_json(
    raw_json: str | dict,
    min_descuento: float = 30.0,
    max_usd: float | None = 40.0,
    solo_marcas_top: bool = True,
    solo_calzado_y_bolsos: bool = True,
) -> list[Deal]:
    """Parsea productos devueltos por el buscador predictivo de Shopify (search/suggest.json)."""
    deals: list[Deal] = []
    try:
        data = json.loads(raw_json) if isinstance(raw_json, str) else raw_json
    except Exception:
        return []

    if not isinstance(data, dict):
        return []

    prods = data.get("resources", {}).get("results", {}).get("products", [])
    for p in prods:
        try:
            pid = str(p.get("id") or "").strip()
            title = (p.get("title") or "").strip()
            vendor = (p.get("vendor") or "").strip()
            ptype = (p.get("type") or p.get("product_type") or "").strip()
            tags = p.get("tags") or []
            if not pid or not title:
                continue

            # Filtro exclusivo de calzado/tenis y bolsos/morrales
            if solo_calzado_y_bolsos and not es_calzado_o_bolso(title, product_type=ptype, tags=tags):
                continue

            # Filtro de marca: omitir marcas desconocidas / genéricas si solo_marcas_top=True
            info_marca = detectar_marca(title, vendor=vendor, tags=tags)
            if solo_marcas_top and not info_marca:
                continue

            price = float(p.get("price") or 0)
            if max_usd is not None and price > max_usd:
                continue

            list_price = float(p.get("compare_at_price_max") or 0)
            if list_price <= price or price <= 0:
                continue

            pct = round(((list_price - price) / list_price) * 100)
            if pct < min_descuento:
                continue

            rel_url = (p.get("url") or "").split("?")[0]
            p_url = f"{BASE_URL}{rel_url}" if rel_url.startswith("/") else rel_url
            img_url = p.get("image") or p.get("featured_image")

            notes = []
            if info_marca:
                notes.append(info_marca[1])
            elif vendor and not vendor.isdigit() and vendor.lower() not in title.lower():
                notes.append(f"🏷️ {vendor}")

            if CALZADO_RE.search(title) or any(k in ptype.lower() for k in ("shoes", "footwear", "boots", "sandals", "sneakers", "slides")):
                notes.append("👟 Calzado y Tenis")
            elif BOLSOS_RE.search(title) or any(k in ptype.lower() for k in ("handbags", "bags", "backpacks", "wallets", "wristlets")):
                notes.append("🎒 Bolsos y Morrales")

            notes.append(f"-{pct}% OFF")
            if pct >= 50:
                notes.append("🔥 50%+ EXTRA OFF")
            notes.append(f"🇺🇸 {STORE_NAME}")

            deals.append(Deal(
                source="simon",
                store=STORE_NAME,
                country=COUNTRY,
                key=f"simon:{pid}",
                title=title,
                url=p_url,
                price=price,
                currency=CURRENCY,
                list_price=list_price,
                image=img_url,
                notes=notes,
                free_shipping_co=False,
                in_stock=True,
            ))
        except Exception:
            continue

    return deals


def _una_coleccion(
    handle: str,
    por_consulta: int = 24,
    min_descuento: float = 30.0,
    max_usd: float | None = 40.0,
    solo_marcas_top: bool = True,
    solo_calzado_y_bolsos: bool = True,
) -> list[Deal]:
    """Descarga y extrae productos de una colección específica de Simon."""
    url = f"{BASE_URL}/collections/{handle}/products.json?limit={por_consulta}"
    try:
        raw = http.get_text(url, timeout=12)
        return extraer_deals_productos_json(
            raw,
            min_descuento=min_descuento,
            max_usd=max_usd,
            solo_marcas_top=solo_marcas_top,
            solo_calzado_y_bolsos=solo_calzado_y_bolsos,
        )
    except Exception as exc:
        print(f"  [simon] aviso: error en colección '{handle}': {exc}")
        return []


def _una_busqueda(
    query: str,
    por_consulta: int = 24,
    min_descuento: float = 30.0,
    max_usd: float | None = 40.0,
    solo_marcas_top: bool = True,
    solo_calzado_y_bolsos: bool = True,
) -> list[Deal]:
    """Busca ofertas por término en el buscador de Simon."""
    q_enc = urllib.parse.quote_plus(query.strip())
    url = f"{BASE_URL}/search/suggest.json?q={q_enc}&resources[type]=product&resources[limit]={por_consulta}"
    try:
        raw = http.get_text(url, timeout=12)
        return extraer_deals_suggest_json(
            raw,
            min_descuento=min_descuento,
            max_usd=max_usd,
            solo_marcas_top=solo_marcas_top,
            solo_calzado_y_bolsos=solo_calzado_y_bolsos,
        )
    except Exception as exc:
        print(f"  [simon] aviso: error en búsqueda '{query}': {exc}")
        return []


def fetch(consultas: list[str] | None = None,
          colecciones: list[str] | None = None,
          por_consulta: int = 24,
          min_descuento: float = 30.0,
          max_usd: float | None = 40.0,
          solo_marcas_top: bool = True,
          solo_calzado_y_bolsos: bool = True) -> list[Deal]:
    """Recolecta ofertas de Simon Premium Outlets en paralelo filtrando solo marcas reconocidas y solo calzado/bolsos."""
    col_efectivas = colecciones if colecciones is not None else COLECCIONES_DEFECTO
    q_efectivas = consultas or []

    tareas = []
    for c in col_efectivas:
        if c.strip():
            tareas.append(("col", c.strip()))
    for q in q_efectivas:
        if q.strip():
            tareas.append(("search", q.strip()))

    if not tareas:
        return []

    deals: list[Deal] = []
    vistos: set[str] = set()

    def _ejecutar(t):
        tipo, valor = t
        if tipo == "col":
            return _una_coleccion(
                valor,
                por_consulta=por_consulta,
                min_descuento=min_descuento,
                max_usd=max_usd,
                solo_marcas_top=solo_marcas_top,
                solo_calzado_y_bolsos=solo_calzado_y_bolsos,
            )
        else:
            return _una_busqueda(
                valor,
                por_consulta=por_consulta,
                min_descuento=min_descuento,
                max_usd=max_usd,
                solo_marcas_top=solo_marcas_top,
                solo_calzado_y_bolsos=solo_calzado_y_bolsos,
            )

    with ThreadPoolExecutor(max_workers=HILOS) as pool:
        for lote in pool.map(_ejecutar, tareas):
            for d in lote:
                if d.key in vistos:
                    continue
                vistos.add(d.key)
                deals.append(d)

    return deals
