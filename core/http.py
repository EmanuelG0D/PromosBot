"""Cliente HTTP minimo sobre la libreria estandar (cero dependencias)."""
from __future__ import annotations

import gzip
import json
import time
import urllib.error
import urllib.request
from typing import Any

import config

PERMANENT_CODES = {400, 401, 403, 404, 410}


class HttpError(RuntimeError):
    pass


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
            last_error = HttpError(f"HTTP {exc.code} en {url}")
            if exc.code in PERMANENT_CODES:
                break
        except Exception as exc:  # timeouts, DNS, TLS, conexion cortada
            last_error = HttpError(f"{type(exc).__name__}: {exc} en {url}")
        if attempt < retries:
            time.sleep(1.5 * (attempt + 1))
    raise last_error or HttpError(f"Fallo desconocido en {url}")


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
