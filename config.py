"""Configuracion central del radar: variables de entorno + watchlist."""
from __future__ import annotations

import json
import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent


def _load_dotenv(path: Path = BASE_DIR / ".env") -> None:
    """Carga un .env sin dependencias externas (no pisa el entorno real)."""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


_load_dotenv()


def _str(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def _float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, "") or default)
    except ValueError:
        return default


def _int(name: str, default: int) -> int:
    try:
        return int(float(os.environ.get(name, "") or default))
    except ValueError:
        return default


def _bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name, "").strip().lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "si", "sí", "on"}


# --- Telegram -----------------------------------------------------------
TELEGRAM_BOT_TOKEN = _str("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = _str("TELEGRAM_CHAT_ID")
TELEGRAM_ADMIN_ID = _str("TELEGRAM_ADMIN_ID", "5583002220")
TELEGRAM_CHANNEL_ID = _str("TELEGRAM_CHANNEL_ID", TELEGRAM_CHAT_ID)
TELEGRAM_CHANNEL_URL = _str("TELEGRAM_CHANNEL_URL", "https://t.me/+GE1nQO-f0HYwNGQx")

# --- eBay (opcional: sin credenciales la fuente se omite sola) ----------
EBAY_CLIENT_ID = _str("EBAY_CLIENT_ID")
EBAY_CLIENT_SECRET = _str("EBAY_CLIENT_SECRET")

# --- Umbrales de alerta -------------------------------------------------
# Piso para que una oferta exista siquiera. Todo lo que pase de aqui se ve,
# pero agrupado en el resumen: al 40% entran ~158 productos por ronda.
MIN_DISCOUNT_PCT = _float("MIN_DISCOUNT_PCT", 40.0)
# Desde aqui la oferta interrumpe con mensaje propio (medido: ~22 por ronda).
INSTANT_DISCOUNT_PCT = _float("INSTANT_DISCOUNT_PCT", 60.0)
# Cada cuanto sale el resumen agrupado, y cuantas ofertas caben en el.
# Apagado: cada oferta llega como su propia tarjeta con foto. Enciendelo si
# el canal se vuelve inmanejable y prefieres una lista agrupada.
DIGEST_ENABLED = _bool("DIGEST_ENABLED", False)
DIGEST_EVERY_HOURS = _float("DIGEST_EVERY_HOURS", 12.0)
DIGEST_MAX_ITEMS = _int("DIGEST_MAX_ITEMS", 20)
# En la primera ronda la base esta vacia y el catalogo entero parece "nuevo":
# sin esto saldrian cientos de alertas de cosas que llevan semanas rebajadas.
# --- Deteccion de rebajas falsas -------------------------------------
# El precio de referencia honesto no es el que declara la tienda, sino el mas
# bajo que el producto tuvo realmente en la ventana reciente.
VERACITY_WINDOW_DAYS = _int("VERACITY_WINDOW_DAYS", 30)
VERACITY_MIN_OBS = _int("VERACITY_MIN_OBS", 3)
VERACITY_MIN_DAYS = _int("VERACITY_MIN_DAYS", 7)
# Subida minima para considerar que inflaron el precio antes de "rebajarlo".
HIKE_PCT = _float("HIKE_PCT", 10.0)
# Cuanto debe bajar del minimo real para avisar, aunque la tienda anuncie poco.
VERACITY_MIN_DROP_PCT = _float("VERACITY_MIN_DROP_PCT", 5.0)
# Un precio que vence dentro de esta ventana es un relampago o un trasnochon.
FLASH_WINDOW_HOURS = _float("FLASH_WINDOW_HOURS", 48.0)
# Una oferta que se vence en horas vale la pena aunque rebaje menos: la
# alternativa es enterarse cuando ya no existe.
FLASH_MIN_DISCOUNT_PCT = _float("FLASH_MIN_DISCOUNT_PCT", 20.0)
# Si es true, una rebaja que no mejora el minimo de la ventana no se avisa.
SUPPRESS_FAKE_DISCOUNTS = _bool("SUPPRESS_FAKE_DISCOUNTS", True)

SEED_ON_EMPTY_DB = _bool("SEED_ON_EMPTY_DB", True)
SEED_MAX_ALERTS = _int("SEED_MAX_ALERTS", 5)
GLITCH_DISCOUNT_PCT = _float("GLITCH_DISCOUNT_PCT", 75.0)
# Con rondas cada 15 minutos, 8 por ronda reparten el cupo diario a lo largo
# del dia en vez de vaciarlo de golpe en la primera media hora.
MAX_ALERTS_PER_RUN = _int("MAX_ALERTS_PER_RUN", 8)
# Freno de mano: con rondas cada 15 minutos, un dia malo podria significar
# cientos de mensajes. Este tope es lo que separa un bot util de uno molesto.
MAX_ALERTS_PER_DAY = _int("MAX_ALERTS_PER_DAY", 40)
# Cuantas ofertas responde un comando. El tope diario no aplica aqui: si lo
# pediste tu, no es interrupcion. El maximo evita chocar con el limite de
# mensajes por minuto de Telegram.
COMANDO_RESULTADOS = _int("COMANDO_RESULTADOS", 15)
COMANDO_MAX_RESULTADOS = _int("COMANDO_MAX_RESULTADOS", 30)
# Secreto con el que Telegram firma cada entrega del webhook. Si se deja
# vacio se deriva del token: es estable entre reinicios y evita tener que
# configurar una variable mas.
TELEGRAM_WEBHOOK_SECRET = _str("TELEGRAM_WEBHOOK_SECRET")
REALERT_DROP_PCT = _float("REALERT_DROP_PCT", 10.0)
REALERT_DAYS = _int("REALERT_DAYS", 14)

# Hasta que porcentaje se le cree el precio de lista a un vendedor externo.
# Medido: el mismo televisor Kalley aparece con identico precio de lista
# ($3.099.900) en la tienda propia de Alkosto y en el marketplace del Exito,
# asi que ese numero es el sugerido del fabricante, no un invento. Los fraudes
# reales que aparecieron estaban todos por encima del 95%.
MARKETPLACE_MAX_DISCOUNT_PCT = _float("MARKETPLACE_MAX_DISCOUNT_PCT", 65.0)
# Y solo en productos de cierto valor: el fraude del precio inflado vive en los
# accesorios baratos (fundas, soportes, joyeros al 70-80%), mientras que en un
# televisor o un celular el precio sugerido suele ser el de verdad.
# Los objetivos de precio no dependen de esto, asi que una ganga barata de
# marketplace sigue avisando si cruza tu tope.
MARKETPLACE_MIN_PRICE_COP = _float("MARKETPLACE_MIN_PRICE_COP", 300000.0)
# Tope maximo de precio: no alertar ni mostrar productos por encima de este valor
# (ej. maximo 2 millones de pesos para evitar ofertas de cosas excesivamente caras).
MAX_PRICE_COP = _float("MAX_PRICE_COP", 2000000.0)

# Un ListPrice inflado (tipico en marketplace) simula descuentos falsos.
# Con esto exigimos que el "glitch" tambien sea bajo frente al historial propio.
REQUIRE_HISTORY_FOR_GLITCH = _bool("REQUIRE_HISTORY_FOR_GLITCH", False)
HISTORY_DROP_PCT = _float("HISTORY_DROP_PCT", 35.0)
MIN_OBSERVATIONS_FOR_HISTORY = _int("MIN_OBSERVATIONS_FOR_HISTORY", 3)

# --- Costo de puesta en Colombia ---------------------------------------
FREIGHT_USD_PER_LB = _float("FREIGHT_USD_PER_LB", 4.0)
FREIGHT_MIN_USD = _float("FREIGHT_MIN_USD", 6.0)
DEFAULT_WEIGHT_LB = _float("DEFAULT_WEIGHT_LB", 2.0)
DUTY_FREE_LIMIT_USD = _float("DUTY_FREE_LIMIT_USD", 200.0)  # TLC: exencion FOB < USD 200
IVA_PCT = _float("IVA_PCT", 19.0)
TARIFF_PCT = _float("TARIFF_PCT", 10.0)  # arancel estimado si se pasa del limite
TRM_FALLBACK = _float("TRM_FALLBACK", 4000.0)

# --- Infraestructura ----------------------------------------------------
DB_PATH = Path(_str("DB_PATH", str(BASE_DIR / "radar.db")))
HTTP_TIMEOUT = _int("HTTP_TIMEOUT", 25)
USER_AGENT = _str(
    "USER_AGENT",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
)

WATCHLIST_PATH = Path(_str("WATCHLIST_PATH", str(BASE_DIR / "watchlist.json")))


def load_watchlist() -> dict:
    if not WATCHLIST_PATH.exists():
        return {}
    return json.loads(WATCHLIST_PATH.read_text(encoding="utf-8"))
