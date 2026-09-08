"""Motor de extraccion de cupones ocultos dentro del texto de una oferta."""
from __future__ import annotations

import re

# Frases que preceden a un codigo. Se buscan sin distinguir mayusculas...
_TRIGGERS = re.compile(
    r"(?:"
    r"w/\s*(?:promo\s+|coupon\s+|discount\s+)?code|"
    r"(?:with|use|using|apply|enter|add)\s+(?:promo\s+|coupon\s+|discount\s+)?code|"
    r"(?:promo|coupon|discount|voucher)\s+code|"
    r"c[oó]digo(?:\s+de)?(?:\s+descuento|\s+promocional)?|"
    r"cup[oó]n"
    r")\s*[:\-]?\s*",
    re.IGNORECASE,
)

# ...pero el codigo en si debe venir en mayusculas para evitar falsos positivos.
_CODE = re.compile(r"^([A-Z0-9][A-Z0-9._-]{3,19})(?![a-z])")

# Palabras que siguen a la frase gatillo sin ser un codigo real.
_STOPWORDS = {"AND", "THE", "FOR", "WITH", "FROM", "YOUR", "THIS", "THAT", "AT", "IN", "ON"}

# Cupones que no son codigo sino un boton que hay que marcar en la pagina.
_CLIP = re.compile(r"clip(?:ping|ped)?\s+(?:the\s+)?(?:\$?\d+(?:\.\d+)?%?\s*)?coupon", re.IGNORECASE)


def extract_coupons(*texts: str | None) -> list[str]:
    """Devuelve los codigos alfanumericos hallados, sin repetir y en orden."""
    found: list[str] = []
    for text in texts:
        if not text:
            continue
        for trigger in _TRIGGERS.finditer(text):
            tail = text[trigger.end(): trigger.end() + 30]
            match = _CODE.match(tail)
            if not match:
                continue
            code = match.group(1).rstrip("._-")
            if len(code) < 4 or code in _STOPWORDS or code.isalpha() and len(code) < 5:
                continue
            if code not in found:
                found.append(code)
    return found


def needs_clipping(*texts: str | None) -> bool:
    """True si la oferta exige activar el cupon manualmente en la tienda."""
    return any(_CLIP.search(t) for t in texts if t)
