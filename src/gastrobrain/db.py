import re
from contextlib import contextmanager
from typing import Iterator
from urllib.parse import quote

import psycopg
from pgvector.psycopg import register_vector
from psycopg_pool import ConnectionPool

from gastrobrain.config import settings

_pool: ConnectionPool | None = None

_URL_RE = re.compile(
    r"^(postgresql(?:\+\w+)?://[^:/?#]+:)(.+)(@[^@/]+(?::\d+)?(?:/.*)?)$"
)


def _normalize_url(url: str) -> str:
    """URL-encode the password component if it contains characters that
    confuse the URI parser (e.g., '@', ':', '/', '#', '%').

    Supabase auto-generated passwords frequently contain '@', which the
    libpq URI parser would otherwise misinterpret as the user/host delimiter.
    """
    m = _URL_RE.match(url)
    if not m:
        return url
    prefix, password, suffix = m.groups()
    if "%" in password and re.search(r"%[0-9A-Fa-f]{2}", password):
        return url
    return prefix + quote(password, safe="") + suffix


def _configure(conn: psycopg.Connection) -> None:
    register_vector(conn)


def get_pool() -> ConnectionPool:
    global _pool
    if _pool is None:
        _pool = ConnectionPool(
            _normalize_url(settings.database_url),
            min_size=1,
            max_size=8,
            kwargs={"autocommit": False},
            configure=_configure,
            check=ConnectionPool.check_connection,
        )
    return _pool


@contextmanager
def conn() -> Iterator[psycopg.Connection]:
    pool = get_pool()
    with pool.connection() as c:
        yield c
