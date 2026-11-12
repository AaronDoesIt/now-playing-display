"""A thin HTTP client: timeouts everywhere, size caps, one error type."""

from __future__ import annotations

import json
import logging
from typing import Any, Mapping

import requests

log = logging.getLogger(__name__)

DEFAULT_TIMEOUT = (5.0, 15.0)          # (connect, read) seconds
MAX_JSON_BYTES = 4 * 1024 * 1024
MAX_IMAGE_BYTES = 15 * 1024 * 1024


class HttpError(Exception):
    """A request failed: network problem, unexpected status, or unusable body."""

    def __init__(self, message: str, status: int | None = None,
                 retry_after: float | None = None, body: Any = None) -> None:
        super().__init__(message)
        self.status = status
        self.retry_after = retry_after
        self.body = body


class HttpClient:
    def __init__(self, user_agent: str, timeout: tuple[float, float] = DEFAULT_TIMEOUT,
                 session: requests.Session | None = None) -> None:
        self._session = session or requests.Session()
        self._session.headers["User-Agent"] = user_agent
        self._timeout = timeout

    def close(self) -> None:
        self._session.close()

    def request_json(self, method: str, url: str, *, params: Mapping[str, Any] | None = None,
                     data: Mapping[str, Any] | None = None, files: Any = None,
                     headers: Mapping[str, str] | None = None,
                     timeout: float | None = None,
                     allow_statuses: tuple[int, ...] = ()) -> tuple[int, Any]:
        """Return (status, parsed JSON). Statuses outside 2xx raise unless allowed."""
        status, response_headers, body = self._fetch(
            method, url, params=params, data=data, files=files, headers=headers,
            timeout=timeout, max_bytes=MAX_JSON_BYTES)
        parsed: Any = None
        if body:
            try:
                parsed = json.loads(body)
            except ValueError:
                parsed = None
        if not (200 <= status < 300) and status not in allow_statuses:
            raise HttpError(f"{method} {_short(url)} returned HTTP {status}", status=status,
                            retry_after=_retry_after(response_headers), body=parsed)
        if parsed is None:
            raise HttpError(f"{method} {_short(url)} did not return JSON", status=status)
        return status, parsed

    def get_json(self, url: str, params: Mapping[str, Any] | None = None, **kwargs: Any) -> Any:
        return self.request_json("GET", url, params=params, **kwargs)[1]

    def get_bytes(self, url: str, max_bytes: int = MAX_IMAGE_BYTES,
                  timeout: float | None = None) -> bytes:
        status, headers, body = self._fetch("GET", url, timeout=timeout, max_bytes=max_bytes)
        if status != 200:
            raise HttpError(f"GET {_short(url)} returned HTTP {status}", status=status,
                            retry_after=_retry_after(headers))
        if not body:
            raise HttpError(f"GET {_short(url)} returned an empty body", status=status)
        return body

    def _fetch(self, method: str, url: str, *, params: Any = None, data: Any = None,
               files: Any = None, headers: Any = None, timeout: float | None = None,
               max_bytes: int) -> tuple[int, Mapping[str, str], bytes]:
        limits = (self._timeout[0], timeout) if timeout else self._timeout
        try:
            with self._session.request(method, url, params=params, data=data, files=files,
                                       headers=headers, timeout=limits, stream=True) as resp:
                chunks: list[bytes] = []
                total = 0
                for chunk in resp.iter_content(64 * 1024):
                    total += len(chunk)
                    if total > max_bytes:
                        raise HttpError(f"{method} {_short(url)}: response larger than "
                                        f"{max_bytes} bytes", status=resp.status_code)
                    chunks.append(chunk)
                return resp.status_code, resp.headers, b"".join(chunks)
        except requests.RequestException as exc:
            raise HttpError(f"{method} {_short(url)} failed: {exc.__class__.__name__}: {exc}") \
                from None


def _retry_after(headers: Mapping[str, str]) -> float | None:
    value = headers.get("Retry-After") if headers else None
    if not value:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        return None


def _short(url: str) -> str:
    """URL without its query string, for log messages (queries can hold API keys)."""
    return url.split("?", 1)[0]
