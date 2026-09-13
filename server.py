#!/usr/bin/env python3
"""Servidor HTTP para desplegar el radar en Render (plan gratuito).

Render no ofrece cron jobs ni background workers gratis: lo unico gratuito es un
*web service*, y lo apaga tras 15 minutos sin trafico entrante. Por eso el radar
vive aqui adentro:

* un hilo programado corre una ronda cada `RUN_EVERY_MINUTES`;
* un servicio externo de ping golpea `/` cada 5 minutos y evita que se duerma;
* `/run` permite disparar una ronda a mano sin esperar al reloj;
* `/telegram` recibe los comandos por webhook y los responde al instante.

El ping NO ejecuta rondas: solo despierta el servicio. Asi el pinger recibe
respuesta inmediata y nunca se topa con un timeout.

Los comandos llegan por webhook y no por reloj: Telegram los entrega apenas se
escriben. Preguntar cada tanto (getUpdates desde un cron) era lo que hacia que
un /alkosto tardara horas, o no llegara nunca.
"""
from __future__ import annotations

import datetime as dt
import hmac
import json
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import config
import radar
from core import comandos, respaldo, telegram, whitelist

PUERTO = int(os.environ.get("PORT", "10000"))

# Dos ritmos, porque no todo cambia al mismo paso. Las ofertas de comunidad
# duran horas y cuestan 23 peticiones; los catalogos de tienda se mueven por
# dia y cuestan 141. Preguntarle a las 12 tiendas cada 15 minutos era gastar
# 15.744 peticiones diarias para enterarse de lo mismo.
RONDA_COMUNIDAD = ("slickdeals", "promocajita")
RONDA_CATALOGOS = ("vtex", "algolia_co", "falabella", "droguerias",
                   "mercadolibre", "ebay", "koaj")
INTERVALO_COMUNIDAD_MIN = float(os.environ.get("RONDA_COMUNIDAD_MINUTOS", "15"))
# RUN_EVERY_MINUTES quedo obsoleta a proposito: heredarla aqui habria dejado
# los catalogos en 15 minutos, que es justo lo que este cambio evita.
INTERVALO_CATALOGOS_MIN = float(os.environ.get("RONDA_CATALOGOS_MINUTOS", "60"))

# Horario de trabajo, en hora de Colombia. De madrugada las tiendas no
# publican nada, y el servicio gratuito de Render tiene 750 horas al mes:
# dormir seis horas diarias deja margen de sobra.
ZONA_CO = dt.timezone(dt.timedelta(hours=-5))
HORA_DESDE = int(os.environ.get("RONDAS_DESDE_HORA", "6"))
HORA_HASTA = int(os.environ.get("RONDAS_HASTA_HORA", "24"))
RUN_TOKEN = os.environ.get("RUN_TOKEN", "").strip()
ESPERA_INICIAL_S = float(os.environ.get("FIRST_RUN_DELAY_SECONDS", "20"))
# Cada cuanto se le pregunta a Telegram cuando no hay webhook (modo local).
SONDEO_S = float(os.environ.get("POLL_COMANDOS_SECONDS", "1"))

# Ruta donde Telegram entrega los mensajes. No es secreta: lo que autentica
# la entrega es la cabecera con el secreto que se registro en setWebhook.
RUTA_WEBHOOK = "/telegram"

_ronda_en_curso = threading.Lock()
# Un comando a la vez: dos respuestas simultaneas se atropellarian en el
# limite de mensajes por minuto de Telegram.
_comando_en_curso = threading.Lock()
# Telegram reintenta una entrega si cree que no llego. Recordar los ultimos
# update_id evita responder dos veces el mismo comando.
_ATENDIDOS_MAX = 200
_atendidos: list[int] = []
_estado: dict = {
    "arranque": None,
    "rondas": 0,
    "corriendo": False,
    "ultima_ronda": None,
    "ultimo_resultado": None,
    "ultimo_error": None,
    "proxima_ronda": None,
    "webhook": None,
    "comandos_atendidos": 0,
}


def _ahora() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def log(mensaje: str) -> None:
    print(f"[{_ahora().strftime('%Y-%m-%d %H:%M:%S')}Z] {mensaje}", flush=True)


def en_horario(ahora: dt.datetime | None = None) -> bool:
    """True si estamos dentro de la franja en que el bot sale a buscar.

    Se comprueba aqui y no solo en el ping externo: un comando de madrugada
    despierta el servicio, y sin esta guarda arrancaria una ronda completa a
    las tres de la manana.
    """
    hora = (ahora or dt.datetime.now(ZONA_CO)).hour
    fin = HORA_HASTA % 24
    if HORA_DESDE == fin:
        return True                       # franja de 24 horas
    if HORA_DESDE < fin:
        return HORA_DESDE <= hora < fin
    return hora >= HORA_DESDE or hora < fin   # franja que cruza la medianoche


def correr_ronda(motivo: str, fuentes=None, espera_s: float = 0) -> dict:
    """Ejecuta una ronda. Nunca deja que dos se solapen.

    Las rondas programadas esperan su turno (espera_s): descartar la de
    catalogos por chocar con una de comunidad costaria una hora entera. El
    disparador manual no espera, para contestar rapido.
    """
    if not _ronda_en_curso.acquire(blocking=espera_s > 0, timeout=espera_s or -1):
        log(f"ronda '{motivo}' descartada: ya hay una en curso")
        return {"error": "ya hay una ronda en curso"}

    try:
        _estado["corriendo"] = True
        log(f"ronda iniciada ({motivo})")
        resultado = radar.ejecutar_ronda(fuentes)
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


def _ya_atendido(update_id) -> bool:
    """True si ese mensaje ya se respondio (Telegram reintenta a veces)."""
    if update_id is None:
        return False
    if update_id in _atendidos:
        return True
    _atendidos.append(update_id)
    del _atendidos[:-_ATENDIDOS_MAX]
    return False


def _notificar_inicio_busqueda(solicitudes: list[dict]) -> None:
    """Envia confirmacion inmediata a Telegram antes de entrar al candado de busqueda."""
    for req in solicitudes:
        chat_id = req.get("chat_id")
        tipo = req.get("tipo")
        if tipo == "categoria":
            cat = req.get("categoria_nombre", "Categoría")
            tienda = comandos.tienda_activa(chat_id=chat_id)
            if tienda not in comandos.CATALOGO and tienda not in ("objetivos", "estado"):
                tienda = "colombia"
            _, _, tit = comandos.CATALOGO.get(tienda, ("co", None, "Colombia"))
            telegram.accion_escribiendo(chat_id=chat_id)
            telegram.send(f"🔍 <i>Revisando ofertas de <b>{telegram.esc(cat)}</b> en <b>{telegram.esc(tit)}</b>...</i>", chat_id=chat_id)
            req["notificado"] = True
        elif tipo == "todo_tienda":
            tienda = comandos.tienda_activa(chat_id=chat_id)
            if tienda not in comandos.CATALOGO:
                tienda = "colombia"
            _, _, tit = comandos.CATALOGO[tienda]
            telegram.accion_escribiendo(chat_id=chat_id)
            telegram.send(f"🔍 <i>Revisando todo el catálogo de <b>{telegram.esc(tit)}</b>...</i>", chat_id=chat_id)
            req["notificado"] = True
        else:
            cmd = req.get("comando", "")
            if cmd in comandos.CATALOGO:
                _, _, tit = comandos.CATALOGO[cmd]
                telegram.accion_escribiendo(chat_id=chat_id)
                telegram.send(f"🔍 <i>Revisando ofertas en <b>{telegram.esc(tit)}</b>...</i>", chat_id=chat_id)
                req["notificado"] = True


def atender_callback_query(callback_query: dict) -> None:
    """Procesa botones interactivos (como aprobacion o rechazo de usuarios)."""
    cq_id = callback_query.get("id")
    remitente = callback_query.get("from") or {}
    remitente_id = remitente.get("id")
    data = (callback_query.get("data") or "").strip()
    msg = callback_query.get("message") or {}
    chat_id = (msg.get("chat") or {}).get("id")
    msg_id = msg.get("message_id")

    # 0. Petición de siguientes ofertas (paginación)
    if data == "siguientes_ofertas":
        telegram.responder_callback(cq_id, "Buscando siguientes ofertas...")
        solicitud_sig = {
            "comando": "siguientes",
            "chat_id": chat_id or remitente_id,
            "user_id": remitente_id,
            "tipo": "siguientes",
            "notificado": True,
        }
        threading.Thread(target=radar.atender_solicitudes, args=([solicitud_sig],), daemon=True).start()
        return

    # 1. Verificacion de suscripcion al canal (la presiona el propio usuario)
    if data == "verificar_canal":
        nombre = remitente.get("first_name") or "Usuario"
        username = remitente.get("username")
        user_handle = f" (@{username})" if username else ""

        if not telegram.es_miembro_del_canal(remitente_id):
            telegram.responder_callback(
                cq_id,
                "Aún no apareces en el canal. Por favor únete usando el botón de arriba y vuelve a presionar 'Ya me uní'.",
                alerta=True,
            )
            return

        telegram.responder_callback(cq_id, "¡Confirmado que estás en el canal!")

        # Si ya esta en whitelist: dar la bienvenida con el menu directamente
        if whitelist.es_permitido(remitente_id):
            if chat_id and msg_id:
                telegram.editar_mensaje(
                    chat_id=chat_id,
                    message_id=msg_id,
                    texto="✅ <b>¡Membresía verificada!</b> Ya puedes explorar ofertas:",
                )
            telegram.send(
                "🤖 <b>PromosBot — Menú Principal</b>\n\n"
                "Elige una tienda en los botones abajo para explorar ofertas:",
                reply_markup=telegram.teclado_tiendas(),
                chat_id=remitente_id,
            )
            return

        # Si aun no esta en whitelist: registrar la solicitud y avisar al admin
        es_nueva = whitelist.registrar_solicitud(remitente_id, nombre=nombre, username=username)
        if chat_id and msg_id:
            telegram.editar_mensaje(
                chat_id=chat_id,
                message_id=msg_id,
                texto="✅ <b>¡Confirmado que estás en el canal!</b>\n\n"
                      "Tu solicitud de acceso fue enviada al administrador. Te notificaremos apenas sea aprobada.",
            )
        if es_nueva:
            if config.TELEGRAM_ADMIN_ID:
                telegram.send(
                    f"🔔 <b>Nueva solicitud de acceso a PromosBot</b>\n\n"
                    f"👤 <b>Usuario:</b> {telegram.esc(nombre)}{telegram.esc(user_handle)}\n"
                    f"🆔 <b>ID:</b> <code>{remitente_id}</code>\n"
                    f"📢 <b>Canal:</b> ✅ Unido a Ofertas\n\n"
                    f"¿Deseas autorizarlo?",
                    reply_markup=telegram.teclado_aprobacion(remitente_id),
                    chat_id=config.TELEGRAM_ADMIN_ID,
                )
        return

    # 2. Acciones administrativas (solo el Admin configurado puede ejecutarlas)
    if not whitelist.es_admin(remitente_id):
        log(f"callback_query no autorizado de {remitente_id}")
        telegram.responder_callback(cq_id, "No tienes permisos de administrador.", alerta=True)
        return

    if data.startswith("aprobar:"):
        target_id = data.split(":", 1)[1].strip()
        info = whitelist.aprobar(target_id)
        nombre = info.get("nombre") or "Usuario"
        username = info.get("username")
        handle = f" (@{username})" if username else ""
        log(f"usuario {target_id} ({nombre}) aprobado por admin")

        telegram.responder_callback(cq_id, f"Aprobado: {nombre}")
        if chat_id and msg_id:
            telegram.editar_mensaje(
                chat_id=chat_id,
                message_id=msg_id,
                texto=f"✅ <b>Acceso Autorizado</b>\n\n"
                      f"El usuario <b>{telegram.esc(nombre)}</b>{telegram.esc(handle)} con ID <code>{target_id}</code> ha sido <b>aprobado</b>.",
            )
        # Notificar al usuario con el teclado interactivo ya desplegado
        telegram.send(
            f"🎉 <b>¡Tu acceso a PromosBot ha sido aprobado!</b>\n\n"
            f"Ya puedes explorar ofertas tocando las tiendas en el menú interactivo abajo:",
            reply_markup=telegram.teclado_tiendas(),
            chat_id=target_id,
        )

    elif data.startswith("rechazar:"):
        target_id = data.split(":", 1)[1].strip()
        info = whitelist.rechazar(target_id)
        nombre = info.get("nombre") or "Usuario"
        username = info.get("username")
        handle = f" (@{username})" if username else ""
        log(f"usuario {target_id} ({nombre}) rechazado por admin")

        telegram.responder_callback(cq_id, f"Rechazado: {nombre}")
        if chat_id and msg_id:
            telegram.editar_mensaje(
                chat_id=chat_id,
                message_id=msg_id,
                texto=f"❌ <b>Acceso Denegado</b>\n\n"
                      f"La solicitud de <b>{telegram.esc(nombre)}</b>{telegram.esc(handle)} con ID <code>{target_id}</code> ha sido <b>rechazada</b>.",
            )
        telegram.send(
            "Lo sentimos, tu solicitud de acceso no fue aprobada por el administrador.",
            chat_id=target_id,
        )
    else:
        telegram.responder_callback(cq_id, "Opción no reconocida.")


def atender_comando(actualizacion: dict) -> None:
    """Responde un comando o evento llegado por webhook. Corre en su propio hilo."""
    if "callback_query" in actualizacion:
        atender_callback_query(actualizacion["callback_query"])
        _estado["comandos_atendidos"] += 1
        return

    solicitud = comandos.leer_comando(actualizacion.get("message") or {})
    if not solicitud:
        return
    if _ya_atendido(actualizacion.get("update_id")):
        log("comando repetido descartado")
        return

    tipo = solicitud.get("tipo")
    cmd = solicitud.get("comando")
    if tipo in ("menu", "elegir_tienda", "solicitud_acceso", "unirse_canal", "grupo_categoria") or cmd in ("menu", "start", "ayuda", "help", "objetivos", "estado"):
        radar.atender_solicitudes([solicitud])
        _estado["comandos_atendidos"] += 1
        return

    _notificar_inicio_busqueda([solicitud])
    radar.nueva_busqueda()

    with _comando_en_curso:
        try:
            log(f"comando /{solicitud['comando']}")
            radar.atender_solicitudes([solicitud])
            _estado["comandos_atendidos"] += 1
        except Exception as exc:              # un comando roto no tumba el servicio
            log(f"comando fallido: {type(exc).__name__}: {exc}")


def registrar_webhook() -> bool:
    """Le dice a Telegram donde entregar los comandos.

    Render publica la URL del servicio en RENDER_EXTERNAL_URL, asi que no hay
    que escribirla a mano en ninguna parte.
    """
    base = (os.environ.get("RENDER_EXTERNAL_URL")
            or os.environ.get("PUBLIC_URL") or "").strip().rstrip("/")
    if not telegram.enabled():
        _estado["webhook"] = "sin token de Telegram"
        return False
    if not base:
        # En local no hay URL publica; se cae al sondeo. Y hay que quitar el
        # webhook que hubiera quedado de un despliegue: si sigue puesto,
        # Telegram no contesta getUpdates y el bot se queda mudo aqui.
        telegram.quitar_webhook()
        _estado["webhook"] = "sin URL publica: sondeo local"
        return False

    destino = base + RUTA_WEBHOOK
    if telegram.registrar_webhook(destino):
        _estado["webhook"] = destino
        log(f"webhook registrado en {destino}")
        return True
    _estado["webhook"] = "fallo el registro"
    log("no se pudo registrar el webhook: los comandos no responderan")
    return False


def sondeo_local() -> None:
    """Escucha comandos con long-polling de Telegram para responder al instante."""
    log("sin webhook: escuchando comandos interactivos (long-polling activo)")
    while True:
        try:
            solicitudes = comandos.pendientes(timeout=2)
            if solicitudes:
                # Eventos de botones interactivos
                callbacks = [s for s in solicitudes if s.get("tipo") == "callback_query"]
                for cb in callbacks:
                    atender_callback_query(cb["callback_query"])
                    _estado["comandos_atendidos"] += 1

                resto = [s for s in solicitudes if s.get("tipo") != "callback_query"]
                # Comandos de interfaz pura (menu, elegir tienda, ayuda, objetivos, solicitud_acceso):
                # se responden de inmediato en el mismo hilo de sondeo sin bloquear la cola.
                inmediatas = [
                    s for s in resto
                    if s.get("tipo") in ("menu", "elegir_tienda", "solicitud_acceso", "unirse_canal", "grupo_categoria")
                    or s.get("comando") in ("menu", "start", "ayuda", "help", "objetivos", "estado")
                ]
                busquedas = [s for s in resto if s not in inmediatas]

                if inmediatas:
                    radar.atender_solicitudes(inmediatas)
                    _estado["comandos_atendidos"] += len(inmediatas)

                if busquedas:
                    _notificar_inicio_busqueda(busquedas)
                    radar.nueva_busqueda()

                    def _ejecutar_busqueda(reqs):
                        with _comando_en_curso:
                            radar.atender_solicitudes(reqs)
                            _estado["comandos_atendidos"] += len(reqs)

                    threading.Thread(target=_ejecutar_busqueda, args=(busquedas,), daemon=True).start()
                continue
        except Exception as exc:              # un comando roto no tumba el hilo
            log(f"sondeo fallido: {type(exc).__name__}: {exc}")
        time.sleep(0.2)


def programador(nombre: str, fuentes, intervalo_min: float,
                retraso_s: float = 0) -> None:
    """Hilo de fondo: una ronda de esas fuentes cada tantos minutos.

    El retraso inicial evita que los dos ritmos arranquen en el mismo segundo
    y se queden chocando cada hora en punto.
    """
    # Se espera un poco para que el puerto quede escuchando cuanto antes:
    # Render marca el despliegue como fallido si tarda en responder.
    time.sleep(ESPERA_INICIAL_S + retraso_s)
    while True:
        if en_horario():
            correr_ronda(nombre, fuentes, espera_s=120)
        else:
            log(f"ronda '{nombre}' omitida: fuera de horario")
        proxima = _ahora() + dt.timedelta(minutes=intervalo_min)
        _estado["proxima_ronda"] = proxima.isoformat(timespec="seconds")
        time.sleep(intervalo_min * 60)


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
                "ronda_comunidad_min": INTERVALO_COMUNIDAD_MIN,
                "ronda_catalogos_min": INTERVALO_CATALOGOS_MIN,
                "horario": f"{HORA_DESDE}:00 a {HORA_HASTA}:00 (Colombia)",
                "en_horario_ahora": en_horario(),
                "telegram_configurado": telegram.enabled(),
                "webhook": _estado["webhook"],
                "comandos_atendidos": _estado["comandos_atendidos"],
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

        self._responder(404, {"error": "ruta desconocida", "rutas": ["/", "/healthz", "/run", RUTA_WEBHOOK]})

    def do_POST(self) -> None:  # noqa: N802  (lo exige BaseHTTPRequestHandler)
        # El cuerpo se lee SIEMPRE y antes de decidir nada. Responder sin
        # haberlo leido deja al cliente escribiendo en una conexion que ya se
        # cerro, y el sistema la aborta: Telegram lo contaria como entrega
        # fallida y la repetiria.
        try:
            largo = int(self.headers.get("Content-Length") or 0)
            crudo = self.rfile.read(largo)
        except (ValueError, OSError):
            self._responder(400, {"error": "cuerpo ilegible"})
            return

        if urlparse(self.path).path != RUTA_WEBHOOK:
            self._responder(404, {"error": "ruta desconocida"})
            return

        # Telegram firma cada entrega con el secreto que le dimos en setWebhook.
        # Sin comprobarlo, cualquiera que adivine la URL podria inventarle
        # comandos al bot.
        firma = self.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
        if not hmac.compare_digest(firma, telegram.secreto_webhook()):
            self._responder(403, {"error": "secreto invalido"})
            return

        try:
            actualizacion = json.loads(crudo or b"{}")
        except ValueError:
            self._responder(400, {"error": "cuerpo ilegible"})
            return

        # Se confirma de inmediato y se trabaja aparte: responder un comando
        # tarda minutos (una tarjeta cada 3.5s) y Telegram, sin un 200 pronto,
        # da la entrega por perdida y la repite.
        self._responder(200, {"ok": True})
        threading.Thread(target=atender_comando, args=(actualizacion,),
                         daemon=True).start()

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
    log(f"telegram configurado: {telegram.enabled()}")
    log(f"comunidad cada {INTERVALO_COMUNIDAD_MIN:g} min | "
        f"catalogos cada {INTERVALO_CATALOGOS_MIN:g} min | "
        f"horario {HORA_DESDE}:00-{HORA_HASTA}:00 Colombia")

    respaldo.restaurar()

    # El menu que Telegram sugiere al escribir "/" y la entrega de comandos.
    # Antes lo hacia el flujo de GitHub Actions; ahora vive aqui.
    telegram_menu = comandos.registrar_menu()
    log(f"menu de comandos publicado: {telegram_menu}")
    webhook_activo = registrar_webhook()
    if not webhook_activo and telegram.enabled():
        threading.Thread(target=sondeo_local, daemon=True).start()

    # Las rondas de fondo automaticas (scraping masivo de miles de productos)
    # son para despliegue 24/7 en la nube (Render con webhook).
    # En modo local, NO se ejecutan en segundo plano para no saturar la red
    # ni congelar la respuesta del bot mientras el usuario interactua con los menus.
    if webhook_activo or os.environ.get("ENABLE_LOCAL_ROUNDS") == "1":
        threading.Thread(target=programador, daemon=True, args=(
            "comunidad", RONDA_COMUNIDAD, INTERVALO_COMUNIDAD_MIN)).start()
        threading.Thread(target=programador, daemon=True, args=(
            "catalogos", RONDA_CATALOGOS, INTERVALO_CATALOGOS_MIN, 90)).start()
    else:
        log("modo interactivo local: rondas de fondo desactivadas para maxima velocidad de respuesta")

    servidor = ThreadingHTTPServer(("0.0.0.0", PUERTO), Manejador)
    try:
        servidor.serve_forever()
    except KeyboardInterrupt:
        log("apagando")
    finally:
        servidor.server_close()


if __name__ == "__main__":
    main()
