"""A small, timed client for the controller REST API."""

from __future__ import annotations

import time
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any
from urllib.parse import urljoin

import requests

from . import __version__
from .platforms import Platform

TERMINAL_JOB_STATES = {"successful", "failed", "error", "canceled"}


class ApiError(Exception):
    def __init__(self, method: str, url: str, status: int | None, body: str = ""):
        self.method = method
        self.url = url
        self.status = status
        self.body = body[:500]
        super().__init__(f"{method} {url} -> {status if status is not None else 'no response'}: {self.body}")


@dataclass
class Timed:
    """A response together with how long it took, in seconds."""

    status: int
    elapsed: float
    data: Any


class ApiClient:
    def __init__(
        self,
        base_url: str,
        platform: Platform,
        username: str | None = None,
        password: str | None = None,
        token: str | None = None,
        verify: bool | str = True,
        timeout: float = 30.0,
    ):
        self.base_url = base_url.rstrip("/")
        self.platform = platform
        self.prefix = platform.api_prefixes[0]
        self._creds = (username, password, token)
        self.verify = verify
        self.timeout = timeout
        self.session = self._new_session()

    def _new_session(self) -> requests.Session:
        s = requests.Session()
        s.verify = self.verify
        s.headers["User-Agent"] = f"awx-tester/{__version__}"
        s.headers["Accept"] = "application/json"
        username, password, token = self._creds
        if token:
            s.headers["Authorization"] = f"Bearer {token}"
        elif username and password is not None:
            s.auth = (username, password)
        return s

    def clone(self) -> ApiClient:
        """A client with its own connection pool, for use from another thread."""
        other = ApiClient.__new__(ApiClient)
        other.__dict__.update(self.__dict__)
        other.session = self._new_session()
        return other

    @property
    def has_credentials(self) -> bool:
        username, password, token = self._creds
        return bool(token or (username and password is not None))

    # -- URLs -----------------------------------------------------------------

    def url(self, path: str) -> str:
        if path.startswith(("http://", "https://")):
            return path
        if path.startswith("/"):
            return self.base_url + path
        return urljoin(self.base_url + self.prefix, path)

    # -- requests ---------------------------------------------------------------

    def request(self, method: str, path: str, *, ok: tuple[int, ...] = (), auth: bool = True,
                **kwargs: Any) -> Timed:
        url = self.url(path)
        kwargs.setdefault("timeout", self.timeout)
        session = self.session if auth else requests.Session()
        if not auth:
            session.verify = self.verify
        start = time.perf_counter()
        try:
            resp = session.request(method, url, **kwargs)
        except requests.RequestException as exc:
            raise ApiError(method, url, None, str(exc)) from exc
        elapsed = time.perf_counter() - start
        if resp.status_code >= 400 and resp.status_code not in ok:
            raise ApiError(method, url, resp.status_code, resp.text)
        data: Any = None
        if resp.content:
            ctype = resp.headers.get("Content-Type", "")
            data = resp.json() if "json" in ctype else resp.text
        return Timed(resp.status_code, elapsed, data)

    def get(self, path: str, params: dict[str, Any] | None = None, **kw: Any) -> Any:
        return self.request("GET", path, params=params, **kw).data

    def post(self, path: str, payload: dict[str, Any] | None = None, **kw: Any) -> Any:
        return self.request("POST", path, json=payload or {}, **kw).data

    def patch(self, path: str, payload: dict[str, Any], **kw: Any) -> Any:
        return self.request("PATCH", path, json=payload, **kw).data

    def delete(self, path: str, **kw: Any) -> None:
        self.request("DELETE", path, ok=(404,), **kw)

    # -- helpers ----------------------------------------------------------------

    def detect(self) -> dict[str, Any]:
        """Find the API root that answers and return its ping document."""
        errors = []
        for prefix in self.platform.api_prefixes:
            try:
                timed = self.request("GET", prefix + "ping/", auth=False)
            except ApiError as exc:
                errors.append(str(exc))
                continue
            if isinstance(timed.data, dict):
                self.prefix = prefix
                return timed.data
            errors.append(f"{prefix}ping/ did not return JSON")
        raise ApiError("GET", self.base_url, None, "; ".join(errors))

    def iter_pages(self, path: str, params: dict[str, Any] | None = None,
                   limit: int | None = None) -> Iterator[dict[str, Any]]:
        params = {"page_size": 200, **(params or {})}
        url: str | None = path
        seen = 0
        while url:
            page = self.get(url, params=params)
            params = None  # the "next" link carries the query string
            for item in page.get("results", []):
                yield item
                seen += 1
                if limit is not None and seen >= limit:
                    return
            url = page.get("next")

    def count(self, path: str, params: dict[str, Any] | None = None) -> int:
        return int(self.get(path, params={**(params or {}), "page_size": 1}).get("count", 0))

    def find_one(self, path: str, **filters: Any) -> dict[str, Any] | None:
        results = self.get(path, params={**filters, "page_size": 1}).get("results", [])
        return results[0] if results else None

    def wait_job(self, url: str, timeout: float = 600, poll: float = 1.0) -> dict[str, Any]:
        """Poll a unified job until it reaches a terminal state."""
        deadline = time.monotonic() + timeout
        while True:
            job = self.get(url)
            if job.get("status") in TERMINAL_JOB_STATES:
                return job
            if time.monotonic() > deadline:
                raise TimeoutError(f"{url} still {job.get('status')} after {timeout:.0f}s")
            time.sleep(poll)
