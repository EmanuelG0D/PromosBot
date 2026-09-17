"""Persistencia ligera en SQLite: deduplicacion e historial propio de precios."""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import sqlite3
import statistics
from pathlib import Path

import config
from core.models import Deal

_SCHEMA = """
CREATE TABLE IF NOT EXISTS alerts (
    key               TEXT PRIMARY KEY,
    store             TEXT,
    title             TEXT,
    url               TEXT,
    currency          TEXT,
    first_alert_ts    TEXT,
    last_alert_ts     TEXT,
    last_alert_price  REAL
);
CREATE TABLE IF NOT EXISTS observations (
    key   TEXT NOT NULL,
    ts    TEXT NOT NULL,
    price REAL NOT NULL,
    PRIMARY KEY (key, ts)
);
CREATE INDEX IF NOT EXISTS idx_obs_key ON observations(key);
CREATE TABLE IF NOT EXISTS meta (
    k TEXT PRIMARY KEY,
    v TEXT
);
CREATE TABLE IF NOT EXISTS deals_recientes (
    hash_id TEXT PRIMARY KEY,
    deal_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_deals_recientes_created ON deals_recientes(created_at);
CREATE INDEX IF NOT EXISTS idx_alerts_store_ts ON alerts(store, last_alert_ts);
"""



import time

import re
import unicodedata

_ultimo_ts = 0.0


def _normalizar_titulo(texto: str) -> str:
    plano = unicodedata.normalize("NFKD", (texto or "").lower())
    plano = "".join(c for c in plano if not unicodedata.combining(c))
    return re.sub(r"[^a-z0-9]+", " ", plano).strip()


def _now() -> str:
    global _ultimo_ts
    ahora = time.time()
    if ahora <= _ultimo_ts:
        ahora = _ultimo_ts + 0.000001
    _ultimo_ts = ahora
    return dt.datetime.fromtimestamp(ahora, tz=dt.timezone.utc).isoformat(timespec="microseconds")


_DBS_INICIALIZADAS: set[str] = set()


class Store:
    def __init__(self, path: Path | None = None) -> None:
        self.path = Path(path or config.DB_PATH)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA busy_timeout=5000")
        ruta_str = str(self.path.resolve())
        if ruta_str not in _DBS_INICIALIZADAS:
            self.conn.executescript(_SCHEMA)
            self.conn.commit()
            _DBS_INICIALIZADAS.add(ruta_str)

    # -- metadatos (TRM cacheada, marcas de tiempo) ----------------------
    def get_meta(self, key: str) -> str | None:
        row = self.conn.execute("SELECT v FROM meta WHERE k = ?", (key,)).fetchone()
        return row["v"] if row else None

    def set_meta(self, key: str, value: str) -> None:
        self.conn.execute(
            "INSERT INTO meta(k, v) VALUES(?, ?) ON CONFLICT(k) DO UPDATE SET v = excluded.v",
            (key, value),
        )
        self.conn.commit()

    # -- historial de precios -------------------------------------------
    def record(self, deal: Deal) -> None:
        """Guarda el precio solo si aporta informacion nueva.

        Registrar cada producto en cada ronda haria crecer la base a decenas de
        miles de filas diarias sin cambiar nada: los precios no se mueven cada
        media hora. Se anota si el precio cambio, o una vez al dia como maximo.
        """
        if deal.price is None:
            return
        precio = float(deal.price)
        ultima = self.conn.execute(
            "SELECT ts, price FROM observations WHERE key = ? ORDER BY ts DESC LIMIT 1",
            (deal.key,),
        ).fetchone()

        if ultima is not None and abs(ultima["price"] - precio) < 0.01:
            try:
                edad = dt.datetime.now(dt.timezone.utc) - dt.datetime.fromisoformat(ultima["ts"])
                if edad < dt.timedelta(hours=24):
                    return
            except (TypeError, ValueError):
                pass

        self.conn.execute(
            "INSERT OR REPLACE INTO observations(key, ts, price) VALUES(?, ?, ?)",
            (deal.key, _now(), precio),
        )
        self.conn.commit()

    def historial(self, key: str, dias: int | None = None,
                  limite: int = 200) -> list[tuple[str, float]]:
        """Observaciones del producto, de la mas antigua a la mas reciente."""
        sql = "SELECT ts, price FROM observations WHERE key = ?"
        params: list = [key]
        if dias:
            corte = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=dias)).isoformat()
            sql += " AND ts >= ?"
            params.append(corte)
        sql += " ORDER BY ts DESC LIMIT ?"
        params.append(limite)
        filas = self.conn.execute(sql, params).fetchall()
        return [(f["ts"], f["price"]) for f in reversed(filas)]

    def price_stats(self, key: str) -> tuple[int, float | None, float | None]:
        """(cantidad de observaciones previas, mediana, minimo historico)."""
        precios = [p for _ts, p in self.historial(key, limite=60)]
        if not precios:
            return 0, None, None
        return len(precios), statistics.median(precios), min(precios)

    # -- deduplicacion ---------------------------------------------------
    def should_alert(self, deal: Deal, dias_titulo: int = 2) -> tuple[bool, str]:
        row = self.conn.execute(
            "SELECT last_alert_ts, last_alert_price FROM alerts WHERE key = ?", (deal.key,)
        ).fetchone()
        if row is not None:
            previo = row["last_alert_price"]
            if deal.price is not None and previo:
                umbral = previo * (1 - config.REALERT_DROP_PCT / 100)
                if deal.price <= umbral:
                    caida = round((1 - deal.price / previo) * 100)
                    return True, f"bajo {caida}% mas desde la ultima alerta"

            try:
                ultima = dt.datetime.fromisoformat(row["last_alert_ts"])
                dias = (dt.datetime.now(dt.timezone.utc) - ultima).days
            except (TypeError, ValueError):
                return True, "registro previo ilegible"
            if dias >= config.REALERT_DAYS:
                return True, f"sigue vigente tras {dias} dias"

            return False, "ya avisada"

        # Deduplicación secundaria por título y tienda (ventana de 2 días / 48 horas)
        if deal.title and deal.store and dias_titulo > 0:
            corte_2d = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=dias_titulo)).isoformat()
            filas = self.conn.execute(
                "SELECT title FROM alerts WHERE LOWER(store) = LOWER(?) AND last_alert_ts >= ?",
                (deal.store.strip(), corte_2d),
            ).fetchall()
            t_norm_nuevo = _normalizar_titulo(deal.title)
            for f in filas:
                if _normalizar_titulo(f["title"]) == t_norm_nuevo:
                    return False, f"ya avisada en los ultimos {dias_titulo} dias (mismo producto en {deal.store})"

        return True, "nueva"

    def mark_alerted(self, deal: Deal) -> None:
        ahora = _now()
        self.conn.execute(
            """
            INSERT INTO alerts(key, store, title, url, currency,
                               first_alert_ts, last_alert_ts, last_alert_price)
            VALUES(?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(key) DO UPDATE SET
                last_alert_ts    = excluded.last_alert_ts,
                last_alert_price = excluded.last_alert_price,
                title            = excluded.title,
                url              = excluded.url
            """,
            (deal.key, deal.store, deal.title, deal.url, deal.currency,
             ahora, ahora, deal.price),
        )
        self.conn.commit()

    # -- freno diario ----------------------------------------------------
    def enviadas_hoy(self) -> int:
        zona_co = dt.timezone(dt.timedelta(hours=-5))
        hoy = dt.datetime.now(zona_co).date().isoformat()
        return int(self.get_meta(f"enviadas:{hoy}") or 0)

    def enviadas_hoy_fuente(self, fuente: str) -> int:
        if not fuente:
            return 0
        zona_co = dt.timezone(dt.timedelta(hours=-5))
        hoy = dt.datetime.now(zona_co).date().isoformat()
        f_norm = fuente.lower().strip()
        return int(self.get_meta(f"enviadas:{hoy}:fuente:{f_norm}") or 0)

    def enviadas_hoy_tienda(self, tienda: str) -> int:
        if not tienda:
            return 0
        zona_co = dt.timezone(dt.timedelta(hours=-5))
        hoy = dt.datetime.now(zona_co).date().isoformat()
        t_norm = tienda.lower().strip()
        return int(self.get_meta(f"enviadas:{hoy}:tienda:{t_norm}") or 0)

    def sumar_enviada(self, deal: Deal | None = None) -> int:
        zona_co = dt.timezone(dt.timedelta(hours=-5))
        hoy = dt.datetime.now(zona_co).date().isoformat()
        total = self.enviadas_hoy() + 1
        self.set_meta(f"enviadas:{hoy}", str(total))
        if deal is not None:
            if deal.source:
                f_norm = deal.source.lower().strip()
                prev_fuente = self.enviadas_hoy_fuente(f_norm)
                self.set_meta(f"enviadas:{hoy}:fuente:{f_norm}", str(prev_fuente + 1))
            if deal.store:
                t_norm = deal.store.lower().strip()
                prev_tienda = self.enviadas_hoy_tienda(t_norm)
                self.set_meta(f"enviadas:{hoy}:tienda:{t_norm}", str(prev_tienda + 1))
        return total

    # -- guardado de deals para entrega directa a chat personal (Deep Link) --
    def guardar_deal_reciente(self, deal: Deal) -> str:
        h = hashlib.md5(deal.key.encode()).hexdigest()[:10]
        datos = {
            "source": deal.source,
            "store": deal.store,
            "country": deal.country,
            "key": deal.key,
            "title": deal.title,
            "url": deal.url,
            "price": deal.price,
            "currency": deal.currency,
            "list_price": deal.list_price,
            "coupons": deal.coupons,
            "notes": deal.notes,
            "seller": deal.seller,
            "marketplace": deal.marketplace,
            "in_stock": deal.in_stock,
            "weight_lb": deal.weight_lb,
            "list_price_trusted": deal.list_price_trusted,
            "expires_at": deal.expires_at,
            "image": deal.image,
            "free_shipping_co": deal.free_shipping_co,
        }
        self.conn.execute(
            """
            INSERT INTO deals_recientes(hash_id, deal_json, created_at)
            VALUES (?, ?, ?)
            ON CONFLICT(hash_id) DO UPDATE SET deal_json = excluded.deal_json, created_at = excluded.created_at
            """,
            (h, json.dumps(datos), dt.datetime.now(dt.timezone.utc).isoformat())
        )
        self.conn.commit()
        return h

    def obtener_deal_reciente(self, hash_id: str) -> Deal | None:
        row = self.conn.execute(
            "SELECT deal_json FROM deals_recientes WHERE hash_id = ?", (hash_id,)
        ).fetchone()
        if not row:
            return None
        try:
            datos = json.loads(row["deal_json"])
            return Deal(**datos)
        except Exception:
            return None

    def prune(self, dias: int = 120, dias_alertas: int = 30) -> int:
        corte_obs = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=dias)).isoformat()
        cur_obs = self.conn.execute("DELETE FROM observations WHERE ts < ?", (corte_obs,))
        corte_alertas = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=dias_alertas)).isoformat()
        cur_alerts = self.conn.execute("DELETE FROM alerts WHERE last_alert_ts < ?", (corte_alertas,))
        corte_recientes = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=7)).isoformat()
        cur_recientes = self.conn.execute("DELETE FROM deals_recientes WHERE created_at < ?", (corte_recientes,))
        self.conn.commit()
        total_borradas = cur_obs.rowcount + cur_alerts.rowcount + cur_recientes.rowcount
        if total_borradas:
            self.conn.execute("VACUUM")   # el archivo viaja al respaldo: hay que encogerlo
        return total_borradas

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> "Store":
        return self

    def __exit__(self, *exc) -> None:
        self.close()
