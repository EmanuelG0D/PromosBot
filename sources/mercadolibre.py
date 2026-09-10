"""Mercado Libre Colombia: ofertas destacadas y cupones leidos de su centro oficial de ofertas.

No requiere credenciales ni OAuth: el centro de ofertas publico de Mercado Libre
sirve las liquidaciones oficiales y cupones activos directamente en formato JSON
estructurado.
"""
from __future__ import annotations

import json
import re
from typing import Any

from core import http
from core.models import Deal

OFERTAS_URL = "https://www.mercadolibre.com.co/ofertas"

# Mapeo de terminos y categorias a IDs de categoria en Mercado Libre Colombia
CATEGORIAS_MAP: dict[str, str] = {
    "televisor": "MCO1000",
    "smart tv": "MCO1000",
    "tv": "MCO1000",
    "audio": "MCO1000",
    "audifonos": "MCO1000",
    "parlante": "MCO1000",
    "diadema": "MCO1000",
    "celular": "MCO1051",
    "smartphone": "MCO1051",
    "iphone": "MCO1051",
    "portatil": "MCO1648",
    "laptop": "MCO1648",
    "computador": "MCO1648",
    "monitor": "MCO1648",
    "electrodomesticos": "MCO5726",
    "nevera": "MCO5726",
    "lavadora": "MCO5726",
    "freidora": "MCO5726",
    "licuadora": "MCO5726",
    "aspiradora": "MCO5726",
    "tenis": "MCO1276",
    "zapatos": "MCO1430",
    "zapatillas": "MCO1276",
    "sneakers": "MCO1276",
    "botas": "MCO1430",
    "ropa": "MCO1430",
    "chaqueta": "MCO1430",
    "pantalon": "MCO1430",
    "camiseta": "MCO1430",
    "jean": "MCO1430",
}

# Etiquetas de interfaz que conviene limpiar de las notas
_TAGS_RE = re.compile(r"\{[^}]+\}")


def _categoria_para_consultas(consultas: list[str]) -> str | None:
    for c in consultas:
        c_norm = c.strip().lower()
        if c_norm in CATEGORIAS_MAP:
            return CATEGORIAS_MAP[c_norm]
        for k, cat_id in CATEGORIAS_MAP.items():
            if k in c_norm or c_norm in k:
                return cat_id
    return None


def _extraer_ofertas(categoria_id: str | None = None) -> list[Deal]:
    url = OFERTAS_URL
    if categoria_id:
        url += f"?category={categoria_id}"

    try:
        txt = http.get_text(url, timeout=10, retries=1)
    except Exception as exc:
        print(f"  [mercadolibre] error consultando {url}: {exc}")
        return []

    m = re.search(r"_n\.ctx\.r\s*=\s*(\{.*?\});?\s*(?:window|_n|</script>|\Z)", txt, re.S)
    if not m:
        m = re.search(r"_n\.ctx\.r\s*=\s*(\{.*?\})\s*;", txt, re.S)
        if not m:
            return []

    try:
        data = json.loads(m.group(1))
    except Exception as exc:
        print(f"  [mercadolibre] error decodificando estado JSON: {exc}")
        return []

    items = (data.get("appProps", {})
                 .get("pageProps", {})
                 .get("data", {})
                 .get("items", []))

    deals: list[Deal] = []
    for it in items:
        card = it.get("card") or {}
        mid = card.get("metadata", {}).get("id")
        raw_url = card.get("metadata", {}).get("url", "")
        if not mid or not raw_url:
            continue

        url_deal = ("https://" + raw_url) if not raw_url.startswith("http") else raw_url
        comps: dict[str, Any] = {c.get("type"): c for c in card.get("components", []) if isinstance(c, dict)}

        title_comp = comps.get("title", {}).get("title", {})
        title = title_comp.get("text") if isinstance(title_comp, dict) else None
        if not title:
            continue

        price_data = comps.get("price", {}).get("price", {})
        curr_price_val = price_data.get("current_price", {}).get("value")
        if curr_price_val is None:
            continue
        try:
            curr_price = float(curr_price_val)
        except (ValueError, TypeError):
            continue

        prev_prices = []
        labels = price_data.get("price_labels") or []
        for lbl in labels:
            for val in lbl.get("values", []):
                if val.get("key") == "previous_price":
                    p = val.get("price", {}).get("value")
                    if p:
                        try:
                            prev_prices.append(float(p))
                        except (ValueError, TypeError):
                            pass

        list_price = prev_prices[0] if prev_prices else curr_price
        trusted = bool(prev_prices) and list_price >= curr_price

        # Imagen de alta resolucion
        pic_list = card.get("pictures", {}).get("pictures", [])
        pic_id = pic_list[0].get("id") if pic_list and isinstance(pic_list[0], dict) else None
        image = f"https://http2.mlstatic.com/D_NQ_NP_{pic_id}-F.jpg" if pic_id else ""

        # Notas, cupones y vendedor
        notes: list[str] = []
        if "promotions" in comps:
            for promo in comps["promotions"].get("promotions", []):
                t = promo.get("text")
                if t:
                    limpio = _TAGS_RE.sub("", t).strip()
                    if limpio and limpio not in notes:
                        if "cup" in limpio.lower():
                            notes.append(f"🎟️ {limpio}")
                        else:
                            notes.append(limpio)

        if "seller" in comps:
            s = comps["seller"].get("seller", {}).get("text")
            if s:
                s_limpio = _TAGS_RE.sub("", s).strip()
                if s_limpio and s_limpio not in notes:
                    notes.append(s_limpio)

        deals.append(Deal(
            source="mercadolibre",
            store="Mercado Libre",
            country="CO",
            key=f"mercadolibre:{mid}",
            title=title,
            url=url_deal,
            price=curr_price,
            list_price=list_price,
            list_price_trusted=trusted,
            currency="COP",
            image=image,
            notes=notes,
            in_stock=True,
        ))

    return deals


def fetch(consultas: list[str] | None = None,
          por_consulta: int = 48,
          categoria_id: str | None = None) -> list[Deal]:
    """Obtiene las mejores ofertas de Mercado Libre.

    Si se pasan consultas, busca la categoria mas afine y filtra por termino.
    Si no, devuelve las mejores ofertas generales ordenadas por mayor descuento.
    """
    cat_id = categoria_id
    if not cat_id and consultas:
        cat_id = _categoria_para_consultas(consultas)

    todas = _extraer_ofertas(cat_id)

    # Si hay consultas de categoria, filtrar los productos relevantes
    if consultas:
        palabras = [c.strip().lower() for c in consultas if len(c.strip()) >= 2]
        filtradas = [
            d for d in todas
            if any(p in d.title.lower() for p in palabras)
        ]
        # Si el filtro estricto devolvio pocas y hay categoria, conservar las de la categoria
        if len(filtradas) >= 3:
            todas = filtradas
        elif cat_id and todas:
            # Mantener las de la categoria
            pass
        elif filtradas:
            todas = filtradas

    # Ordenar por mejor descuento verificable
    todas.sort(key=lambda d: -d.discount_verificable)
    return todas[:por_consulta]
