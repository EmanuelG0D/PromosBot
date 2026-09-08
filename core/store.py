"""Persistencia ligera en SQLite: deduplicacion e historial propio de precios."""
from __future__ import annotations

import datetime as dt
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
"""


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="microseconds")


class Store:
    def __init__(self, path: Path | None = None) -> None:
        self.path = Path(path or config.DB_PATH)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(_SCHEMA)
        self.conn.commit()

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
    def should_alert(self, deal: Deal) -> tuple[bool, str]:
        row = self.conn.execute(
            "SELECT last_alert_ts, last_alert_price FROM alerts WHERE key = ?", (deal.key,)
        ).fetchone()
        if row is None:
            return True, "nueva"

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
        hoy = dt.datetime.now(dt.timezone.utc).date().isoformat()
        return int(self.get_meta(f"enviadas:{hoy}") or 0)

    def sumar_enviada(self) -> int:
        hoy = dt.datetime.now(dt.timezone.utc).date().isoformat()
        total = self.enviadas_hoy() + 1
        self.set_meta(f"enviadas:{hoy}", str(total))
        return total

    def prune(self, dias: int = 120) -> int:
        corte = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=dias)).isoformat()
        cur = self.conn.execute("DELETE FROM observations WHERE ts < ?", (corte,))
        self.conn.commit()
        if cur.rowcount:
            self.conn.execute("VACUUM")   # el archivo viaja al respaldo: hay que encogerlo
        return cur.rowcount

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> "Store":
        return self

    def __exit__(self, *exc) -> None:
        self.close()
