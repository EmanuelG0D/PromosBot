"""Cliente HTTP minimo sobre la libreria estandar (cero dependencias)."""
from __future__ import annotations

import gzip
import json
import secrets
import re
import time
import urllib.error
import urllib.request
from typing import Any

import config

PERMANENT_CODES = {400, 401, 403, 404, 410}


class HttpError(RuntimeError):
    pass


# El token de un bot de Telegram viaja dentro de la URL. Si un mensaje de error
# la incluye tal cual, el token termina en los logs -y en un repositorio
# publico eso es una fuga-. Se enmascara siempre, antes de cualquier registro.
_TOKEN_EN_URL = re.compile(r"/bot\d+:[A-Za-z0-9_-]+")


def url_segura(url: str) -> str:
    return _TOKEN_EN_URL.sub("/bot***", url or "")


def _request(
    url: str,
    *,
    data: bytes | None = None,
    headers: dict[str, str] | None = None,
    timeout: int | None = None,
    retries: int = 2,
    method: str | None = None,
) -> bytes:
    hdrs = {
        "User-Agent": config.USER_AGENT,
        "Accept": "*/*",
        "Accept-Encoding": "gzip",
    }
    if headers:
        hdrs.update(headers)

    last_error: Exception | None = None
    for attempt in range(retries + 1):
        req = urllib.request.Request(url, data=data, headers=hdrs, method=method)
        try:
            with urllib.request.urlopen(req, timeout=timeout or config.HTTP_TIMEOUT) as resp:
                raw = resp.read()
                if resp.headers.get("Content-Encoding") == "gzip":
                    raw = gzip.decompress(raw)
                return raw
        except urllib.error.HTTPError as exc:
            # El cuerpo trae la explicacion real ("chat not found", "group chat
            # was upgraded"...). Sin el, un 400 no dice nada util.
            detalle = ""
            try:
                detalle = url_segura(exc.read()[:400].decode("utf-8", "replace"))
            except Exception:
                pass
            last_error = HttpError(
                f"HTTP {exc.code} en {url_segura(url)}" + (f" -> {detalle}" if detalle else ""))
            if exc.code in PERMANENT_CODES:
                break
        except Exception as exc:  # timeouts, DNS, TLS, conexion cortada
            last_error = HttpError(f"{type(exc).__name__}: {exc} en {url_segura(url)}")
        if attempt < retries:
            time.sleep(1.5 * (attempt + 1))
    raise last_error or HttpError(f"Fallo desconocido en {url_segura(url)}")


def get_text(url: str, **kwargs: Any) -> str:
    return _request(url, **kwargs).decode("utf-8", errors="replace")


def get_json(url: str, **kwargs: Any) -> Any:
    headers = {"Accept": "application/json"}
    headers.update(kwargs.pop("headers", {}) or {})
    return json.loads(_request(url, headers=headers, **kwargs).decode("utf-8", errors="replace"))


def post_json(url: str, payload: Any, **kwargs: Any) -> Any:
    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    headers.update(kwargs.pop("headers", {}) or {})
    body = json.dumps(payload).encode("utf-8")
    return json.loads(_request(url, data=body, headers=headers, **kwargs).decode("utf-8", errors="replace"))


def put_json(url: str, payload: Any, **kwargs: Any) -> Any:
    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    headers.update(kwargs.pop("headers", {}) or {})
    body = json.dumps(payload).encode("utf-8")
    crudo = _request(url, data=body, headers=headers, method="PUT", **kwargs)
    return json.loads(crudo.decode("utf-8", errors="replace"))


def post_form(url: str, form: str, **kwargs: Any) -> Any:
    headers = {"Content-Type": "application/x-www-form-urlencoded"}
    headers.update(kwargs.pop("headers", {}) or {})
    return json.loads(_request(url, data=form.encode(), headers=headers, **kwargs).decode("utf-8", errors="replace"))


def post_multipart(url: str, campos: dict[str, str],
                   archivo: tuple[str, str, bytes], **kwargs: Any) -> Any:
    """POST de formulario con un archivo adjunto, sin dependencias.

    `archivo` es (nombre del campo, nombre de archivo, contenido). El cuerpo se
    arma a mano porque la libreria estandar no trae multipart, y no vale la
    pena una dependencia por treinta lineas.

    Hace falta para las fotos que Telegram no puede bajar por su cuenta: hay
    CDN de tienda que no responden desde fuera del pais.
    """
    frontera = "----promosbot" + secrets.token_hex(16)
    sep = ("--" + frontera).encode()
    salto = b"\r\n"

    partes: list[bytes] = []
    for clave, valor in campos.items():
        partes += [
            sep, salto,
            f'Content-Disposition: form-data; name="{clave}"'.encode(),
            salto, salto,
            str(valor).encode("utf-8"), salto,
        ]

    campo, nombre, contenido = archivo
    partes += [
        sep, salto,
        (f'Content-Disposition: form-data; name="{campo}"; '
         f'filename="{nombre}"').encode(),
        salto,
        b"Content-Type: application/octet-stream", salto, salto,
        contenido, salto,
        sep, b"--", salto,
    ]

    cabeceras = {"Content-Type": "multipart/form-data; boundary=" + frontera}
    cabeceras.update(kwargs.pop("headers", {}) or {})
    crudo = _request(url, data=b"".join(partes), headers=cabeceras, **kwargs)
    return json.loads(crudo.decode("utf-8", errors="replace"))


def descargar(url: str, **kwargs: Any) -> bytes:
    """El contenido crudo de una URL, para reenviarlo tal cual."""
    return _request(url, **kwargs)
