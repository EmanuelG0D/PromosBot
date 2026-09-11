"""Control de acceso por lista blanca (whitelist) para PromosBot.

Permite que solo usuarios aprobados por el Administrador puedan consultar
ofertas por privado. Se sincroniza con la rama 'estado' de GitHub para que
los usuarios aprobados persistan entre despliegues sin reiniciar Render.
"""
from __future__ import annotations

import base64
import json
import os
import time
from pathlib import Path
from typing import Any

import config
from core import http

ARCHIVO_LOCAL = Path(config.BASE_DIR / "whitelist.json")
RAMA_ESTADO = os.environ.get("GITHUB_BRANCH", "estado").strip()
RUTA_GITHUB = "estado/whitelist.json"


def _admin_id() -> str:
    return str(config.TELEGRAM_ADMIN_ID or "5583002220").strip()


def _datos_iniciales() -> dict[str, Any]:
    admin = _admin_id()
    return {
        "admin": admin,
        "usuarios_permitidos": [admin] if admin else [],
        "solicitudes_pendientes": {},
    }


def _url_github() -> str:
    repo = os.environ.get("GITHUB_REPO", "").strip()
    return f"https://api.github.com/repos/{repo}/contents/{RUTA_GITHUB}"


def _cabeceras_github() -> dict[str, str]:
    token = os.environ.get("GITHUB_TOKEN", "").strip()
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def _metadatos_github() -> dict | None:
    try:
        url = f"{_url_github()}?ref={RAMA_ESTADO}"
        return http.get_json(url, headers=_cabeceras_github(), retries=1)
    except Exception:
        return None


def _sync_desde_github() -> dict[str, Any] | None:
    if ARCHIVO_LOCAL.name != "whitelist.json":
        return None
    token = os.environ.get("GITHUB_TOKEN", "").strip()
    repo = os.environ.get("GITHUB_REPO", "").strip()
    if not (token and repo):
        return None
    try:
        resp = _metadatos_github()
        if resp and resp.get("content"):
            crudo = base64.b64decode(resp["content"]).decode("utf-8")
            return json.loads(crudo)
    except Exception as exc:
        print(f"  [whitelist] error leyendo de GitHub: {exc}")
    return None


def _sync_hacia_github(datos: dict[str, Any]) -> bool:
    if ARCHIVO_LOCAL.name != "whitelist.json":
        return False
    token = os.environ.get("GITHUB_TOKEN", "").strip()
    repo = os.environ.get("GITHUB_REPO", "").strip()
    if not (token and repo):
        return False
    try:
        crudo = json.dumps(datos, indent=2, ensure_ascii=False).encode("utf-8")
        cuerpo = {
            "message": "radar: actualizar lista blanca de usuarios",
            "content": base64.b64encode(crudo).decode("utf-8"),
            "branch": RAMA_ESTADO,
        }
        # Obtener sha previo para sobrescribir (si ya existe el archivo en GitHub)
        actual = _metadatos_github()
        if actual and actual.get("sha"):
            cuerpo["sha"] = actual["sha"]
        resp = http.put_json(_url_github(), cuerpo, headers=_cabeceras_github(), retries=1)
        return bool(resp and resp.get("content"))
    except Exception as exc:
        print(f"  [whitelist] error guardando en GitHub: {exc}")
        return False


def cargar() -> dict[str, Any]:
    """Carga los usuarios permitidos. Prioriza archivo local, luego GitHub."""
    if ARCHIVO_LOCAL.exists():
        try:
            return json.loads(ARCHIVO_LOCAL.read_text(encoding="utf-8"))
        except Exception:
            pass

    remoto = _sync_desde_github()
    if remoto:
        guardar_local(remoto)
        return remoto

    base = _datos_iniciales()
    guardar_local(base)
    return base


def guardar_local(datos: dict[str, Any]) -> None:
    try:
        ARCHIVO_LOCAL.write_text(json.dumps(datos, indent=2, ensure_ascii=False), encoding="utf-8")
    except Exception as exc:
        print(f"  [whitelist] error escribiendo local: {exc}")


def guardar(datos: dict[str, Any]) -> None:
    guardar_local(datos)
    _sync_hacia_github(datos)


def es_admin(user_id: int | str | None) -> bool:
    if user_id is None:
        return False
    admin = _admin_id()
    return bool(admin and str(user_id).strip() == admin)


def es_permitido(user_id: int | str | None) -> bool:
    if user_id is None:
        return False
    uid = str(user_id).strip()
    if es_admin(uid):
        return True
    datos = cargar()
    permitidos = {str(u).strip() for u in datos.get("usuarios_permitidos", [])}
    return uid in permitidos


def registrar_solicitud(user_id: int | str, nombre: str = "", username: str | None = None) -> bool:
    """Registra una solicitud pendiente de acceso. Retorna True si es nueva."""
    uid = str(user_id).strip()
    if es_permitido(uid):
        return False
    datos = cargar()
    pendientes = datos.setdefault("solicitudes_pendientes", {})
    es_nueva = uid not in pendientes
    pendientes[uid] = {
        "nombre": nombre or "Usuario",
        "username": username or "",
    }
    guardar(datos)
    return es_nueva


def obtener_info_solicitud(user_id: int | str) -> dict[str, str]:
    uid = str(user_id).strip()
    datos = cargar()
    return datos.get("solicitudes_pendientes", {}).get(uid, {"nombre": "Usuario", "username": ""})


def aprobar(user_id: int | str) -> dict[str, str]:
    """Aprueba al usuario, guardando en whitelist y GitHub. Retorna sus datos."""
    uid = str(user_id).strip()
    datos = cargar()
    permitidos = set(str(u).strip() for u in datos.get("usuarios_permitidos", []))
    permitidos.add(uid)
    datos["usuarios_permitidos"] = list(permitidos)
    info = datos.get("solicitudes_pendientes", {}).pop(uid, {"nombre": "Usuario", "username": ""})
    guardar(datos)
    return info


def rechazar(user_id: int | str) -> dict[str, str]:
    """Rechaza al usuario eliminandolo de pendientes. Retorna sus datos."""
    uid = str(user_id).strip()
    datos = cargar()
    permitidos = set(str(u).strip() for u in datos.get("usuarios_permitidos", []))
    permitidos.discard(uid)
    datos["usuarios_permitidos"] = list(permitidos)
    info = datos.get("solicitudes_pendientes", {}).pop(uid, {"nombre": "Usuario", "username": ""})
    guardar(datos)
    return info


_ultimas_sesiones: dict[str, float] = {}


def registrar_inicio_sesion(user_id: int | str | None, ventana_segundos: int = 1800) -> bool:
    """Registra actividad de usuario. Retorna True si inicia nueva sesion tras ventana de inactividad."""
    if not user_id:
        return False
    uid = str(user_id).strip()
    if es_admin(uid):
        return False
    ahora = time.time()
    ultima = _ultimas_sesiones.get(uid, 0.0)
    es_nueva = (ahora - ultima) > ventana_segundos
    _ultimas_sesiones[uid] = ahora
    return es_nueva


def limpiar_sesiones() -> None:
    """Limpia el registro de sesiones en memoria (util para pruebas)."""
    _ultimas_sesiones.clear()

