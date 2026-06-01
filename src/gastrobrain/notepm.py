"""NotePM API client — read-only subset for the ingestion pipeline.

Per PRD §4.1 NotePM is rate-limited at 60 req/min server-side; this client
buckets at 50 req/min to leave headroom for concurrent webhook traffic.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Iterator

import httpx
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from gastrobrain.config import settings


@dataclass
class User:
    user_code: str
    name: str
    email: str
    status: str


@dataclass
class Page:
    page_code: str
    note_code: str
    folder_id: int | None
    title: str
    body: str
    created_at: str
    updated_at: str
    created_by_user_code: str
    updated_by_user_code: str
    url: str


class _Retryable(Exception):
    """Raised on 429/5xx so tenacity retries; non-retryable 4xx escape as HTTPStatusError."""


class _Bucket:
    """Token bucket — capacity refills at rate/sec, acquire() blocks when empty."""

    def __init__(self, rate_per_minute: int) -> None:
        self._capacity = float(rate_per_minute)
        self._tokens = float(rate_per_minute)
        self._rate = rate_per_minute / 60.0
        self._last = time.monotonic()

    def acquire(self) -> None:
        while True:
            now = time.monotonic()
            self._tokens = min(self._capacity, self._tokens + (now - self._last) * self._rate)
            self._last = now
            if self._tokens >= 1.0:
                self._tokens -= 1.0
                return
            time.sleep((1.0 - self._tokens) / self._rate)


class NotePMClient:
    def __init__(self, token: str | None = None, subdomain: str | None = None) -> None:
        self._token = token or settings.notepm_api_token
        if not self._token:
            raise RuntimeError("NOTEPM_API_TOKEN is not set (env or .env)")
        sub = subdomain or settings.notepm_team_subdomain
        self._host = f"https://{sub}.notepm.jp"
        self._base = f"{self._host}/api/v1"
        self._http = httpx.Client(
            headers={"Authorization": f"Bearer {self._token}"},
            timeout=httpx.Timeout(90.0, connect=10.0),
        )
        self._bucket = _Bucket(rate_per_minute=50)

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> "NotePMClient":
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    @retry(
        stop=stop_after_attempt(5),
        wait=wait_exponential(multiplier=1, min=2, max=60),
        retry=retry_if_exception_type(_Retryable),
        reraise=True,
    )
    def _get(self, path_or_url: str, **params: object) -> dict:
        self._bucket.acquire()
        is_full_url = path_or_url.startswith("http")
        url = path_or_url if is_full_url else f"{self._base}{path_or_url}"
        try:
            resp = self._http.get(url, params=None if is_full_url else params)
        except httpx.TransportError as e:
            raise _Retryable(f"{type(e).__name__}: {e}") from e
        if resp.status_code == 429 or 500 <= resp.status_code < 600:
            raise _Retryable(f"HTTP {resp.status_code}: {resp.text[:200]}")
        resp.raise_for_status()
        return resp.json()

    def list_users(self) -> Iterator[User]:
        next_url: str | None = None
        page = 1
        while True:
            data = self._get(next_url) if next_url else self._get("/users", page=page, per_page=100)
            for u in data.get("users", []):
                yield User(
                    user_code=u["user_code"],
                    name=u.get("name", ""),
                    email=u.get("email", ""),
                    status=u.get("status", ""),
                )
            next_url = (data.get("meta") or {}).get("next_page")
            if not next_url:
                return

    def list_notes(self) -> dict[str, str]:
        """Return note_code → note name. Used to populate folder_path
        (NotePM's REST API doesn't expose a hierarchical path on /pages)."""
        notes: dict[str, str] = {}
        next_url: str | None = None
        while True:
            data = self._get(next_url) if next_url else self._get("/notes", per_page=100)
            for n in data.get("notes", []):
                notes[n["note_code"]] = n.get("name", "")
            next_url = (data.get("meta") or {}).get("next_page")
            if not next_url:
                return notes

    def list_pages(self, per_page: int = 100) -> Iterator[Page]:
        next_url: str | None = None
        page = 1
        while True:
            data = self._get(next_url) if next_url else self._get("/pages", page=page, per_page=per_page)
            for p in data.get("pages", []):
                yield Page(
                    page_code=p["page_code"],
                    note_code=p.get("note_code", ""),
                    folder_id=p.get("folder_id"),
                    title=p.get("title", ""),
                    body=p.get("body", ""),
                    created_at=p["created_at"],
                    updated_at=p["updated_at"],
                    created_by_user_code=(p.get("created_by") or {}).get("user_code", ""),
                    updated_by_user_code=(p.get("updated_by") or {}).get("user_code", ""),
                    url=f"{self._host}/page/{p['page_code']}",
                )
            next_url = (data.get("meta") or {}).get("next_page")
            if not next_url:
                return
