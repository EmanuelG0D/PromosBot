"""Respaldo del historial en un repositorio de GitHub.

Render no ofrece discos persistentes en el plan gratuito: el archivo `radar.db`
se borra en cada despliegue y en cada reinicio de la instancia. Sin respaldo,
el bot olvida que ya aviso una oferta y vuelve a mandarla desde cero.

Este modulo guarda la base en el mismo repositorio del que Render despliega,
usando la API de contenidos de GitHub. Es **opcional**: si faltan las variables
de entorno, no hace nada y lo advierte una sola vez.
"""
from __future__ import annotations

import base64
import os
from pathlib import Path

import config
from core import http

TOKEN = os.environ.get("GITHUB_TOKEN", "").strip()
REPO = os.environ.get("GITHUB_REPO", "").strip()            # formato: usuario/repositorio
RAMA = os.environ.get("GITHUB_BRANCH", "estado").strip()
RUTA = os.environ.get("GITHUB_STATE_PATH", "estado/radar.db").strip()

# La API de contenidos devuelve el archivo en base64 solo hasta 1 MB.
LIMITE_BYTES = 900_000


def configurado() -> bool:
    return bool(TOKEN and REPO)


def _url() -> str:
    return f"https://api.github.com/repos/{REPO}/contents/{RUTA}"


def _cabeceras() -> dict[str, str]:
    return {
        "Authorization": f"Bearer {TOKEN}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def _metadatos() -> dict | None:
    """Datos del archivo remoto, o None si aun no existe."""
    try:
        return http.get_json(f"{_url()}?ref={RAMA}", headers=_cabeceras(), retries=1)
    except Exception:
        return None


def restaurar() -> bool:
    """Trae la base guardada. Se llama al arrancar, antes de la primera ronda."""
    if not configurado():
        print("[respaldo] sin GITHUB_TOKEN/GITHUB_REPO: el historial no sobrevive a un reinicio")
        return False

    datos = _metadatos()
    if not datos or not datos.get("content"):
        print("[respaldo] todavia no hay copia remota; se empieza de cero")
        return False

    try:
        crudo = base64.b64decode(datos["content"])
        destino = Path(config.DB_PATH)
        destino.parent.mkdir(parents=True, exist_ok=True)
        destino.write_bytes(crudo)
        print(f"[respaldo] historial restaurado ({len(crudo) // 1024} KB)")
        return True
    except Exception as exc:
        print(f"[respaldo] no se pudo restaurar: {exc}")
        return False


def guardar(mensaje: str = "radar: historial de precios") -> bool:
    """Sube la base al repositorio. Se llama al final de cada ronda."""
    if not configurado():
        return False

    ruta = Path(config.DB_PATH)
    if not ruta.exists():
        return False

    crudo = ruta.read_bytes()
    if len(crudo) > LIMITE_BYTES:
        print(f"[respaldo] base demasiado grande ({len(crudo) // 1024} KB); "
              "baja REALERT_DAYS o el retention de prune()")
        return False

    cuerpo = {
        "message": mensaje,
        "content": base64.b64encode(crudo).decode(),
        "branch": RAMA,
    }
    actual = _metadatos()
    if actual and actual.get("sha"):
        cuerpo["sha"] = actual["sha"]   # GitHub exige el sha para sobrescribir

    try:
        http.put_json(_url(), cuerpo, headers=_cabeceras(), retries=1)
        return True
    except Exception as exc:
        print(f"[respaldo] no se pudo guardar: {exc}")
        return False
