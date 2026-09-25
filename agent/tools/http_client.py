"""Shared async HTTP client for Lumine's tools.

Building an ``httpx.AsyncClient`` creates a new SSL context and re-reads the CA
bundle from disk. Doing that inside a tool call blocked the LiveKit event loop
for hundreds of milliseconds, so the client is created lazily once and reused
for every request.

The reference count lets each room session hold the client for its own
lifetime while the last one to finish closes it. Nothing is created at import
time, no background task is started, and no client outlives the worker.

Per-request timeouts and redirect behaviour stay with the individual tools, so
sharing a client does not change any tool's HTTP semantics.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from typing import AsyncIterator

import httpx

DEFAULT_TIMEOUT = httpx.Timeout(10.0)
MAX_KEEPALIVE_CONNECTIONS = 8

_client: httpx.AsyncClient | None = None
_users = 0


def _new_client() -> httpx.AsyncClient:
    # Redirects stay opt-in per request; weather and news do not follow them.
    return httpx.AsyncClient(
        timeout=DEFAULT_TIMEOUT,
        limits=httpx.Limits(max_keepalive_connections=MAX_KEEPALIVE_CONNECTIONS),
    )


async def acquire() -> httpx.AsyncClient:
    """Take a reference to the shared client, creating it on first use.

    Creation runs in a thread because building the first client loads the CA
    bundle into a new SSL context, which blocks for a few hundred milliseconds.
    If two callers race, the loser closes its own client and shares the winner,
    so exactly one client survives.
    """
    global _client, _users
    if _client is not None and not _client.is_closed:
        _users += 1
        return _client

    created = await asyncio.to_thread(_new_client)
    if _client is not None and not _client.is_closed:
        await created.aclose()
    else:
        _client = created
    _users += 1
    return _client


async def release() -> None:
    """Drop a reference; the last one out closes the client."""
    global _users
    if _client is None:
        return
    _users = max(0, _users - 1)
    if _users == 0:
        await close()


async def close() -> None:
    """Close the shared client if it exists."""
    global _client, _users
    client, _client = _client, None
    _users = 0
    if client is not None and not client.is_closed:
        await client.aclose()


def is_shared() -> bool:
    return _client is not None


def user_count() -> int:
    return _users


@asynccontextmanager
async def shared_client() -> AsyncIterator[httpx.AsyncClient]:
    """Borrow the shared client for the duration of one tool call."""
    client = await acquire()
    try:
        yield client
    finally:
        await release()
