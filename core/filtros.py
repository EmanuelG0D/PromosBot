"""Descarte de ofertas por palabras, para que no se cuele lo que no interesa.

Las busquedas por palabra clave siempre arrastran ruido: buscar "adidas" en un
foro de ofertas puede devolver un set de perfumes que menciona la marca. Este
filtro es la ultima linea: si el titulo contiene una palabra vetada, la oferta
no llega al canal.
"""
from __future__ import annotations

import re
import unicodedata

# Ruido recurrente en foros de ofertas de EE. UU. que no es tecnologia ni hogar.
VETADAS_POR_DEFECTO = (
    "fragrance", "perfume", "cologne", "eau de", "body spray", "deodorant",
    "gift card", "giftcard", "credit card", "insurance", "subscription",
    "vitamin", "supplement", "protein powder", "cbd", "vape",
    "mattress", "life insurance", "car rental", "hotel", "flight",
)

# Lo que acompaña al producto pero no es el producto: uno busca la aspiradora,
# no el empaque de la aspiradora. Vivia en objetivos.py y solo se aplicaba a los
# objetivos de precio; aqui la usan todas las fuentes.
ACCESORIOS = (
    "base", "soporte", "funda", "forro", "cubierta", "estuche", "canasta",
    "repuesto", "repuestos", "accesorio", "accesorios", "kit", "filtro",
    "molde", "moldes", "bandeja", "papel", "adaptador", "cargador", "cable",
    "correa", "empaque", "bolsa", "manual", "protector",
    # El mueble donde va el televisor no es un televisor. Las tiendas por
    # departamento los devuelven al buscar "televisor" y llenan la ronda.
    "rack", "panel", "centro de entretenimiento", "mesa para", "mueble para",
    "organizador", "porta",
    # Repuestos y partes internas de electrodomésticos (ej. Haceb, Whirlpool)
    "compresor", "motor", "termostato", "resistencia", "valvula", "tarjeta",
    "tarjeta electronica", "condensador", "evaporador", "perilla", "manguera",
    "bomba", "bomba de desague", "filtro secador",
)

# Categorias que no interesan aunque la tienda las tenga en oferta. Las tiendas
# colombianas venden de todo, y una busqueda de "licuadora" en un supermercado
# arrastra el vaso de repuesto, el florero y el tetero.
RUIDO_CO = (
    "tetero", "biberon", "chupo", "pañalera", "panalera", "florero",
    "portarretrato", "porta retrato", "adorno", "figura decorativa",
    # Las marcas deportivas venden perfume, y no es lo que se busca al pedir
    # "adidas": "Perfume Adidas Ice Dive" no es ropa ni calzado.
    "perfume", "eau de", "colonia", "desodorante", "body splash",
)

# Lo que se veta por defecto en las tiendas colombianas cuando la watchlist no
# trae su propia lista.
VETADAS_CO = ACCESORIOS + RUIDO_CO


def _normalizar(texto: str) -> str:
    plano = unicodedata.normalize("NFKD", (texto or "").lower())
    plano = "".join(c for c in plano if not unicodedata.combining(c))
    return re.sub(r"[^a-z0-9]+", " ", plano)


def pertinente(titulo: str, incluir: tuple | list | None = None) -> bool:
    """True si el titulo menciona algo de la lista de intereses.

    Con la lista vacia deja pasar todo. Sirve para quedarse solo con las marcas
    y categorias que importan cuando la fuente trae de todo, como el feed de
    portada de un foro de ofertas.
    """
    if not incluir:
        return True
    limpio = _normalizar(titulo)
    return any(_normalizar(p) in limpio for p in incluir)


def descartado(titulo: str, vetadas: tuple | list | None = None) -> bool:
    """True si el titulo contiene alguna palabra vetada."""
    palabras = VETADAS_POR_DEFECTO if vetadas is None else vetadas
    if not palabras:
        return False
    limpio = _normalizar(titulo)
    return any(_normalizar(p) in limpio for p in palabras)


def es_accesorio(titulo: str, palabras: tuple | list | None = None) -> bool:
    """True si el titulo ARRANCA con una palabra de accesorio.

    Buscarla en cualquier parte del titulo descartaba el producto que solo la
    menciona: "Aspiradora T-FAL X-FORCE, bateria 45 min, 4 accesorios" es una
    aspiradora, no un accesorio. En los catalogos el accesorio se nombra de
    primero -"Base TECHNOSOPORTES para televisores de 32 a 65"-, asi que esa
    es la señal que sirve.
    """
    palabras = ACCESORIOS if palabras is None else palabras
    if not palabras:
        return False
    limpio = _normalizar(titulo).strip()

    # Componentes de PC y hardware que no deben ser descartados como accesorios
    if any(limpio.startswith(hw) for hw in (
        "tarjeta de video", "tarjeta grafica", "tarjeta madre", "tarjeta de sonido",
        "board", "motherboard", "placa base", "placa madre", "soporte de monitor", "brazo para monitor",
        "soporte monitor", "base para monitor"
    )):
        return False

    return any(limpio == _normalizar(p).strip()
               or limpio.startswith(_normalizar(p).strip() + " ")
               for p in palabras)


def _palabra_coincide(p_titulo: str, p_termino: str) -> bool:
    if p_titulo == p_termino:
        return True
    if len(p_termino) >= 4:
        # Flexibilidad para plurales habituales en español (camiseta/camisetas, pantalon/pantalones)
        if p_titulo == p_termino + "s" or p_titulo == p_termino + "es":
            return True
        if p_termino == p_titulo + "s" or p_termino == p_titulo + "es":
            return True
    return False


def menciona(titulo: str, termino: str) -> bool:
    """True si el titulo nombra el termino como palabra o secuencia de palabras.

    Soporta coincidencia exacta y plural/singular en español (ej. camiseta/camisetas),
    evitando falsos positivos por subcadenas (ej. "puma" en "espumados").
    """
    palabras = _normalizar(termino).split()
    if not palabras:
        return True
    titulo_palabras = _normalizar(titulo).split()
    n = len(palabras)

    def _secuencia_coincide(sub_titulo: list[str]) -> bool:
        return all(_palabra_coincide(t, q) for t, q in zip(sub_titulo, palabras))

    return any(_secuencia_coincide(titulo_palabras[i:i + n])
               for i in range(len(titulo_palabras) - n + 1))


PESADOS_CASILLERO = (
    "tv", "televisor", "television", "smart tv", "oled", "refrigerator", "refrigerador",
    "nevera", "washing machine", "lavadora", "dryer", "secadora", "air conditioner",
    "aire acondicionado", "caminadora", "treadmill", "furniture", "mueble", "generator",
    "generador", "estufa", "stove", "range",
)


def es_pesado_para_casillero(titulo: str) -> bool:
    """True si el producto es excesivamente pesado o voluminoso para flete de casillero aereo."""
    limpio = _normalizar(titulo).split()
    return any(p in limpio for p in PESADOS_CASILLERO)


# Expresiones regulares para la subclasificación de familias (Fase 1)
REGLAS_FAMILIA: dict[str, re.Pattern] = {
    "computadores_y_hardware": re.compile(
        r"\b(computador|computadores|computadora|computadoras|pc\s*gamer|laptop|laptops|portatil|portatiles|"
        r"procesador|procesadores|ryzen|intel\s*core|cpu|"
        r"board|motherboard|placa\s*base|placa\s*madre|"
        r"tarjeta\s*de\s*video|tarjeta\s*grafica|grafica|gpu|geforce|rtx|gtx|radeon|"
        r"memoria\s*ram|ddr4|ddr5|disco\s*ssd|disco\s*duro|nvme|m\.2|"
        r"fuente\s*de\s*poder|gabinete|chasis|torre\s*gamer|"
        r"refrigeracion\s*liquida|enfriador|cooler|disipador)\b"
    ),
    "tv_y_monitores": re.compile(
        r"\b(tv|televisor|televisores|smart\s*tv|oled|qled|nanocell|uhd|monitor|monitores|pantalla\s*gamer|pantalla\s*curva)\b"
    ),
    "refrigeracion": re.compile(
        r"\b(nevera|neveras|refrigerador|refrigeradores|nevecon|nevecones|congelador|congeladores|freezer|minibar)\b"
    ),
    "smartphones": re.compile(
        r"\b(celular|celulares|smartphone|smartphones|telefono\s*movil|iphone|galaxy\s*(?:s\d+|a\d+|z|fold|flip)|redmi|xiaomi|motorola\s*(?:edge|g\d+))\b"
    ),
    "pequenos_electro": re.compile(
        r"\b(airfryer|freidora|licuadora|cafetera|sandwichera|microondas|arrocera|waflera|tostadora|batidora|procesador\s*de\s*alimentos|extractor|olla\s*a\s*presion)\b"
    ),
    "perifericos": re.compile(
        r"\b(teclado|teclados|mouse|raton|mousepad|tapete\s*gamer|"
        r"diadema|diademas|audifonos|auriculares|headset|earbuds|airpods|"
        r"microfono|microfonos|webcam|camara\s*web|capturadora|"
        r"parlante|parlantes|brazo\s*para\s*monitor|soporte\s*monitor|"
        r"smartwatch|reloj\s*inteligente|cargador|powerbank)\b"
    ),
    "ropa_basica": re.compile(
        r"\b(camiseta|camisetas|polo|polos|jean|jeans|pantalon|pantalones|pantaloneta|short|shorts|bermuda|bermudas|ropa\s*interior|boxer|boxers|panty|panties|calcetines|medias|pijama|pijamas|esqueleto|buzo|chaqueta|sueter)\b"
    ),
    "otros": re.compile(r".*"),
}


def asignar_familia(titulo: str) -> str:
    """Retorna la subfamilia del producto según el título. Si no hace match, retorna 'otros'."""
    limpio = _normalizar(titulo)
    for familia, patron in REGLAS_FAMILIA.items():
        if familia == "otros":
            continue
        if patron.search(limpio):
            return familia
    return "otros"


_asignar_familia = asignar_familia


