"""Prove a provider credential works, with one cheap authenticated request.

The Providers screen can say a key is *stored*, because the OS keyring says so.
It cannot say the key is *good*: that needs a request. This is that request, and
it is a script rather than desktop-layer code so the provider knowledge stays in
the catalog.

Why a separate process at all: the secret never has to enter the desktop layer's
address space or its logs. Rust reads the keyring, passes the secret in this
process's environment, and reads back a JSON verdict with no key material in it.

Usage::

    python agent/provider_probe.py google
    python agent/provider_probe.py --list

Output is always one JSON object on stdout::

    {"ok": true,  "verdict": "valid",      "status": 200, "latencyMs": 330}
    {"ok": false, "verdict": "rejected",   "status": 401, "detail": "Invalid API Key"}
    {"ok": false, "verdict": "inconclusive", "status": 405, "detail": "..."}

The three-way verdict is the point. A provider that is down, or a request of ours
that is malformed, must never be reported to the user as "your key is wrong".
Only the statuses the catalog lists for that provider are allowed to say that.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from typing import Any

try:
    from .providers import PROVIDERS, ProbeDefinition, get_provider
except ImportError:  # running as `python agent/provider_probe.py`
    from providers import PROVIDERS, ProbeDefinition, get_provider

#: Long enough for a cold TLS handshake on a slow link, short enough that a
#: wedged provider cannot leave the settings screen waiting.
DEFAULT_TIMEOUT = 12.0

VERDICT_VALID = "valid"
VERDICT_REJECTED = "rejected"
VERDICT_INCONCLUSIVE = "inconclusive"
VERDICT_NO_PROBE = "no_probe"
VERDICT_NO_SECRET = "no_secret"
VERDICT_UNKNOWN_PROVIDER = "unknown_provider"


def _load_environment() -> None:
    """Load ``agent/.env`` so a key entered there is testable too.

    An already-set variable wins, which is how the desktop layer passes a
    keyring secret in ahead of this.
    """
    try:
        from dotenv import load_dotenv
    except ImportError:
        return
    from pathlib import Path

    load_dotenv(Path(__file__).resolve().parent / ".env")


def _read_secret(provider_id: str) -> str:
    """The credential for a provider, from the variable the catalog names."""
    provider = get_provider(provider_id)
    if provider is None or not provider.key_env:
        return ""
    for name in provider.key_env:
        value = (os.getenv(name) or "").strip()
        if value:
            return value
    return ""


def _read_secrets(provider_id: str) -> dict[str, str]:
    """Every variable a provider needs, as ``{name: value}``.

    Needed by LiveKit, which has no single key: the URL, the API key and the API
    secret only mean anything together, and the first version of the gate asked
    for "any value for this provider" and so reported a three-parts-missing
    credential as present.
    """
    provider = get_provider(provider_id)
    if provider is None:
        return {}
    return {
        name: (os.getenv(name) or "").strip()
        for name in provider.key_env
        if (os.getenv(name) or "").strip()
    }


def _livekit_token(url: str, api_key: str, api_secret: str) -> str:
    """Mint the short-lived JWT LiveKit authenticates an API call with.

    Uses LiveKit's own ``AccessToken`` rather than signing a payload by hand, so
    the claim shape is the one the server expects. The grants are the minimum
    that lets the probe list rooms: read-only, and it creates nothing.
    """
    import datetime

    from livekit import api

    token = api.AccessToken(api_key, api_secret)
    token.with_grants(api.VideoGrants(room_list=True))
    # An identity is required even for a service call; this one is never a
    # participant in a room, and never leaves this process.
    token.identity = "lumine-credential-probe"
    token.ttl = datetime.timedelta(minutes=5)
    return token.to_jwt()


def _probe_livekit(
    definition: ProbeDefinition,
    values: dict[str, str],
    timeout: float,
) -> dict[str, Any]:
    """Check a LiveKit server URL, API key and API secret as one credential.

    LiveKit has no bearer key to send, so the probe is the only place the three
    values are ever seen together. That is exactly why it is worth having: a URL
    that is wrong, a key that is wrong and a secret that is wrong all produce
    different answers here, where before there was no test at all and the failure
    surfaced as a session that silently never started.
    """
    import httpx

    url = (os.getenv("LIVEKIT_URL") or values.get("LIVEKIT_URL") or "").strip().rstrip("/")
    api_key = values.get("LIVEKIT_API_KEY", "")
    api_secret = values.get("LIVEKIT_API_SECRET", "")

    absent = [
        name
        for name, value in (
            ("LIVEKIT_URL", url),
            ("LIVEKIT_API_KEY", api_key),
            ("LIVEKIT_API_SECRET", api_secret),
        )
        if not value
    ]
    if absent:
        return {
            "ok": False,
            "verdict": VERDICT_NO_SECRET,
            "provider": "livekit",
            "detail": (
                f"LiveKit needs {len(absent)} more value"
                f"{'' if len(absent) == 1 else 's'}: {', '.join(absent)}."
            ),
        }

    if not url.startswith(("http://", "https://", "ws://", "wss://")):
        # A URL that is not a URL is a typo, and saying so is more use than the
        # connection error it would otherwise produce.
        return {
            "ok": False,
            "verdict": VERDICT_REJECTED,
            "provider": "livekit",
            "detail": f"LIVEKIT_URL is not a server address. It should start with https:// or wss://.",
        }

    # Twirp is served over HTTP even when the client speaks WebRTC, so a `wss://`
    # server URL is rewritten rather than rejected. Users copy this value out of
    # the LiveKit console, where it is always a WebSocket address.
    http_url = url.replace("wss://", "https://", 1).replace("ws://", "http://", 1)
    endpoint = f"{http_url}{definition.token_path}"

    started = time.perf_counter()
    try:
        token = _livekit_token(url, api_key, api_secret)
    except Exception as exc:  # noqa: BLE001 - any failure is a verdict
        return {
            "ok": False,
            "verdict": VERDICT_INCONCLUSIVE,
            "provider": "livekit",
            "detail": f"Could not mint a LiveKit token: {type(exc).__name__}.",
        }

    try:
        with httpx.Client(timeout=timeout) as client:
            response = client.post(
                endpoint,
                headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
                content=b"{}",
            )
    except Exception as exc:  # noqa: BLE001 - any failure is a verdict
        return {
            "ok": False,
            "verdict": VERDICT_INCONCLUSIVE,
            "provider": "livekit",
            "latencyMs": int((time.perf_counter() - started) * 1000),
            "detail": (
                f"{type(exc).__name__}: could not reach {http_url}. Check the server "
                "address, and that the project is not paused."
            ),
        }

    latency = int((time.perf_counter() - started) * 1000)
    if 200 <= response.status_code < 300:
        return {
            "ok": True,
            "verdict": VERDICT_VALID,
            "provider": "livekit",
            "status": response.status_code,
            "latencyMs": latency,
        }

    detail = _detail(response)
    if response.status_code in definition.invalid_status:
        return {
            "ok": False,
            "verdict": VERDICT_REJECTED,
            "provider": "livekit",
            "status": response.status_code,
            "latencyMs": latency,
            "detail": detail
            or "LiveKit refused the key or secret. Check both were copied in full.",
        }

    return {
        "ok": False,
        "verdict": VERDICT_INCONCLUSIVE,
        "provider": "livekit",
        "status": response.status_code,
        "latencyMs": latency,
        "detail": (
            f"LiveKit answered {response.status_code}, which does not indicate a "
            "credential problem. This test could not conclude either way."
        ),
    }


def _detail(response: Any, limit: int = 160) -> str:
    """A short, safe explanation from the provider's own response.

    Only a message is taken, never the body: a provider that echoes the key back
    in an error would otherwise put it in a log.
    """
    try:
        body = response.json()
    except ValueError:
        text = (response.text or "").strip().replace("\n", " ")
        return text[:limit]
    if isinstance(body, dict):
        error = body.get("error")
        if isinstance(error, dict):
            for key in ("message", "detail", "type"):
                if error.get(key):
                    return str(error[key])[:limit]
        for key in ("message", "detail", "error"):
            value = body.get(key)
            if isinstance(value, str) and value:
                return value[:limit]
    return ""


def probe(provider_id: str, timeout: float = DEFAULT_TIMEOUT) -> dict[str, Any]:
    """Make one authenticated request and classify the answer.

    Never raises. Every failure becomes a verdict, because the caller is a
    settings screen that needs something to show rather than an exception.
    """
    provider = get_provider(provider_id)
    if provider is None:
        return {"ok": False, "verdict": VERDICT_UNKNOWN_PROVIDER, "provider": provider_id}

    definition: ProbeDefinition | None = provider.probe
    if definition is None:
        return {
            "ok": False,
            "verdict": VERDICT_NO_PROBE,
            "provider": provider_id,
            "detail": (
                "No cheap authenticated request is known for this provider, so its "
                "key cannot be tested from here."
            ),
        }

    if definition.auth_kind == "livekit_token":
        # LiveKit is checked before the single-secret path, because it has no
        # single secret: the URL, key and secret are one credential.
        return _probe_livekit(definition, _read_secrets(provider_id), timeout)

    secret = _read_secret(provider_id)
    if not secret:
        return {
            "ok": False,
            "verdict": VERDICT_NO_SECRET,
            "provider": provider_id,
            "detail": f"No credential is set. Expected one of: {', '.join(provider.key_env)}.",
        }

    try:
        import httpx
    except ImportError:  # pragma: no cover - httpx ships with the agent
        return {
            "ok": False,
            "verdict": VERDICT_INCONCLUSIVE,
            "provider": provider_id,
            "detail": "httpx is not installed, so no request could be made.",
        }

    started = time.perf_counter()
    try:
        with httpx.Client(timeout=timeout) as client:
            response = client.request(
                definition.method,
                definition.url,
                headers=definition.headers_for(secret),
            )
    except Exception as exc:  # noqa: BLE001 - any failure is a verdict
        return {
            "ok": False,
            "verdict": VERDICT_INCONCLUSIVE,
            "provider": provider_id,
            "latencyMs": int((time.perf_counter() - started) * 1000),
            # The exception text can echo a URL; the secret is in a header, so
            # this cannot contain it, but the URL is dropped anyway.
            "detail": f"{type(exc).__name__}: could not reach {definition.url.split('?')[0]}",
        }

    latency = int((time.perf_counter() - started) * 1000)
    detail = _detail(response)

    if 200 <= response.status_code < 300:
        return {
            "ok": True,
            "verdict": VERDICT_VALID,
            "provider": provider_id,
            "status": response.status_code,
            "latencyMs": latency,
        }

    if response.status_code in definition.invalid_status:
        return {
            "ok": False,
            "verdict": VERDICT_REJECTED,
            "provider": provider_id,
            "status": response.status_code,
            "latencyMs": latency,
            "detail": detail or f"The provider answered {response.status_code}.",
        }

    # Anything else is our problem or theirs, not the key's. Saying "invalid"
    # here would send a user re-entering a working credential, so the reassurance
    # is part of the message rather than something the caller has to remember:
    # the provider's own words are kept, but never on their own.
    reassurance = (
        f"The provider answered {response.status_code}, which does not indicate a "
        "credential problem. This test could not conclude either way."
    )
    return {
        "ok": False,
        "verdict": VERDICT_INCONCLUSIVE,
        "provider": provider_id,
        "status": response.status_code,
        "latencyMs": latency,
        "detail": f"{detail} {reassurance}" if detail else reassurance,
    }


def _describe_all() -> list[dict[str, Any]]:
    return [
        {
            "provider": provider.id,
            "label": provider.label,
            "probeable": provider.probe is not None,
            "url": provider.probe.url if provider.probe else None,
        }
        for provider in sorted(PROVIDERS.values(), key=lambda item: item.id)
    ]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("provider", nargs="?", help="the provider id to test")
    parser.add_argument(
        "--list", action="store_true", help="print which providers can be tested"
    )
    parser.add_argument(
        "--timeout", type=float, default=DEFAULT_TIMEOUT, help="seconds to wait"
    )
    args = parser.parse_args(argv)

    _load_environment()

    if args.list:
        print(json.dumps(_describe_all(), indent=2))
        return 0

    if not args.provider:
        parser.error("a provider id is required, or --list")

    payload = probe(args.provider, timeout=args.timeout)
    print(json.dumps(payload, separators=(",", ":")))
    # Only a rejected credential is a non-zero exit; inconclusive is not, because
    # a provider outage must not look like a failure to a calling script.
    return 1 if payload["verdict"] == VERDICT_REJECTED else 0


if __name__ == "__main__":
    sys.exit(main())
