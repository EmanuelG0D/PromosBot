#!/usr/bin/env python3
"""Servidor HTTP para desplegar el radar en Render (plan gratuito).

Render no ofrece cron jobs ni background workers gratis: lo unico gratuito es un
*web service*, y lo apaga tras 15 minutos sin trafico entrante. Por eso el radar
vive aqui adentro:

* un hilo programado corre una ronda cada `RUN_EVERY_MINUTES`;
* un servicio externo de ping golpea `/` cada 5 minutos y evita que se duerma;
* `/run` permite disparar una ronda a mano sin esperar al reloj.

El ping NO ejecuta rondas: solo despierta el servicio. Asi el pinger recibe
respuesta inmediata y nunca se topa con un timeout.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import radar
from core import respaldo, telegram

PUERTO = int(os.environ.get("PORT", "10000"))
INTERVALO_MIN = float(os.environ.get("RUN_EVERY_MINUTES", "30"))
RUN_TOKEN = os.environ.get("RUN_TOKEN", "").strip()
ESPERA_INICIAL_S = float(os.environ.get("FIRST_RUN_DELAY_SECONDS", "20"))

_ronda_en_curso = threading.Lock()
_estado: dict = {
    "arranque": None,
    "rondas": 0,
    "corriendo": False,
    "ultima_ronda": None,
    "ultimo_resultado": None,
    "ultimo_error": None,
    "proxima_ronda": None,
}


def _ahora() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def log(mensaje: str) -> None:
    print(f"[{_ahora().strftime('%Y-%m-%d %H:%M:%S')}Z] {mensaje}", flush=True)


def correr_ronda(motivo: str) -> dict:
    """Ejecuta una ronda. Nunca deja que dos se solapen."""
    if not _ronda_en_curso.acquire(blocking=False):
        log(f"ronda '{motivo}' descartada: ya hay una en curso")
        return {"error": "ya hay una ronda en curso"}

    try:
        _estado["corriendo"] = True
        log(f"ronda iniciada ({motivo})")
        resultado = radar.ejecutar_ronda()
        _estado["rondas"] += 1
        _estado["ultima_ronda"] = _ahora().isoformat(timespec="seconds")
        _estado["ultimo_resultado"] = resultado
        _estado["ultimo_error"] = resultado.get("error")
        log(f"ronda terminada: {resultado}")

        if respaldo.guardar():
            log("historial respaldado en GitHub")
        return resultado
    except Exception as exc:                      # una ronda rota no tumba el servicio
        _estado["ultimo_error"] = f"{type(exc).__name__}: {exc}"
        log(f"ronda fallida: {_estado['ultimo_error']}")
        return {"error": _estado["ultimo_error"]}
    finally:
        _estado["corriendo"] = False
        _ronda_en_curso.release()


def programador() -> None:
    """Hilo de fondo: una ronda cada INTERVALO_MIN minutos."""
    # Se espera un poco para que el puerto quede escuchando cuanto antes:
    # Render marca el despliegue como fallido si tarda en responder.
    time.sleep(ESPERA_INICIAL_S)
    while True:
        correr_ronda("programada")
        proxima = _ahora() + dt.timedelta(minutes=INTERVALO_MIN)
        _estado["proxima_ronda"] = proxima.isoformat(timespec="seconds")
        time.sleep(INTERVALO_MIN * 60)


class Manejador(BaseHTTPRequestHandler):
    server_version = "RadarOfertas/1.0"

    def _responder(self, codigo: int, cuerpo, tipo: str = "application/json") -> None:
        if tipo == "application/json":
            datos = json.dumps(cuerpo, ensure_ascii=False, indent=2).encode("utf-8")
        else:
            datos = str(cuerpo).encode("utf-8")
        self.send_response(codigo)
        self.send_header("Content-Type", f"{tipo}; charset=utf-8")
        self.send_header("Content-Length", str(len(datos)))
        self.end_headers()
        self.wfile.write(datos)

    def do_GET(self) -> None:  # noqa: N802  (lo exige BaseHTTPRequestHandler)
        ruta = urlparse(self.path)

        if ruta.path in ("/", "/status"):
            self._responder(200, {
                "ok": True,
                "servicio": "radar de ofertas",
                "arranque": _estado["arranque"],
                "rondas_completadas": _estado["rondas"],
                "corriendo_ahora": _estado["corriendo"],
                "ultima_ronda": _estado["ultima_ronda"],
                "ultimo_resultado": _estado["ultimo_resultado"],
                "ultimo_error": _estado["ultimo_error"],
                "proxima_ronda": _estado["proxima_ronda"],
                "intervalo_minutos": INTERVALO_MIN,
                "telegram_configurado": telegram.enabled(),
                "respaldo_configurado": respaldo.configurado(),
            })
            return

        if ruta.path == "/healthz":
            self._responder(200, "ok", tipo="text/plain")
            return

        if ruta.path == "/run":
            if RUN_TOKEN:
                enviado = parse_qs(ruta.query).get("token", [""])[0]
                if enviado != RUN_TOKEN:
                    self._responder(403, {"error": "token invalido"})
                    return
            if _estado["corriendo"]:
                self._responder(409, {"error": "ya hay una ronda en curso"})
                return
            threading.Thread(target=correr_ronda, args=("manual",), daemon=True).start()
            self._responder(202, {"ok": True, "mensaje": "ronda lanzada en segundo plano"})
            return

        self._responder(404, {"error": "ruta desconocida", "rutas": ["/", "/healthz", "/run"]})

    def log_message(self, *args) -> None:
        """Silencia el log por peticion: el ping cada 5 min lo inundaria."""
        return


def main() -> None:
    # Sin esto los logs quedan en el bufer y Render los muestra tarde o los
    # pierde si la instancia se reinicia. Ademas evita que los emojis de las
    # alertas revienten en consolas que no son UTF-8.
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(line_buffering=True, encoding="utf-8", errors="replace")

    _estado["arranque"] = _ahora().isoformat(timespec="seconds")
    log(f"iniciando radar en el puerto {PUERTO}")
    log(f"telegram configurado: {telegram.enabled()} | ronda cada {INTERVALO_MIN} min")

    respaldo.restaurar()

    threading.Thread(target=programador, daemon=True).start()

    servidor = ThreadingHTTPServer(("0.0.0.0", PUERTO), Manejador)
    try:
        servidor.serve_forever()
    except KeyboardInterrupt:
        log("apagando")
    finally:
        servidor.server_close()


if __name__ == "__main__":
    main()
