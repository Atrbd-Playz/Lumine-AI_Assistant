"""Ask a local OpenAI-compatible server which models it has.

Lumine's catalog is a list Lumine knows about. A model server on the same machine
is a list that server knows about, and the two are not the same list: someone who
has `qwen3:8b` pulled has never heard of it, and a chooser that does not offer it
is a chooser that answers "no" to a model that is sitting right there.

So this asks the server. It is a script rather than desktop-layer code for the same
reason `provider_probe.py` is: the desktop layer has no HTTP client of its own, and
adding one for a single `GET` on localhost would be a dependency to keep current
in exchange for six lines of `httpx`.

Usage::

    python agent/local_models.py
    python agent/local_models.py http://localhost:11434/v1

Output is one JSON object on stdout::

    {"ok": true,  "baseUrl": "http://localhost:11434/v1", "models": [{"id": "llama3.2:3b", "size": 2019393189}]}
    {"ok": false, "baseUrl": "...", "error": "ConnectError: ...", "detail": "..."}

`ok` is about *reaching the server*, never about whether a model is good. A server
that is running with nothing pulled answers 200 and an empty list, which is a
reachable server and a useless chooser, and those are reported differently.
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any

try:
    from .providers import get_provider
except ImportError:  # running as `python agent/local_models.py`
    from providers import get_provider

#: A request to a process on this machine should be instant. Anything slower than
#: this is a wrong address or a server that is not listening, and waiting longer
#: only makes the settings screen feel broken.
DEFAULT_TIMEOUT = 4.0

#: The address the catalog publishes for a local provider. Read from the catalog
#: rather than repeated, so a profile saved with the default and one that resolves
#: from the catalog cannot disagree.
FALLBACK_BASE_URL = "http://localhost:11434/v1"


def default_base_url(provider_id: str = "ollama") -> str:
    """The catalog's declared address for a local provider.

    Falls back to Ollama's, because that is the one server whose default port
    every other OpenAI-compatible server on a laptop is usually configured to
    imitate, and a wrong guess here produces a connection error the user can act
    on rather than an empty chooser they cannot.
    """
    provider = get_provider(provider_id)
    if provider is None:
        return FALLBACK_BASE_URL
    # The address is an option on the provider's models rather than on the
    # provider, because that is where every other model setting lives and a
    # second home for one would need its own resolution path.
    for model in provider.models_for("llm"):
        definition = model.option("base_url")
        if definition is not None and definition.default is not None:
            return str(definition.default)
    return FALLBACK_BASE_URL


def _root(base_url: str) -> str:
    """The server's own root, from an OpenAI-compatible base URL.

    `/v1` is the OpenAI surface; `/api/tags` is the server's native one, and the
    two live side by side on the same port. So the discovery path is appended to
    the root, and the version prefix is dropped rather than a request being made
    to a route that does not exist.
    """
    root = base_url.strip().rstrip("/")
    # Longest first, and that ordering is load-bearing rather than tidiness. `/v1`
    # is a suffix of `/api/v1`, so a list starting with the short one strips
    # `/v1` off `/api/v1` and leaves `http://host/api` -- which then gets
    # `/api/tags` appended and answers 404 on a server that was running the whole
    # time. A test caught exactly that.
    for suffix in ("/openai/v1", "/api/v1", "/v1"):
        if root.endswith(suffix):
            return root[: -len(suffix)]
    return root


def _models_from_tags(body: Any) -> list[dict[str, Any]]:
    """Read Ollama's ``/api/tags`` shape into ``{id, size}`` rows.

    Only the name and the size survive. A tag entry also carries a digest, a
    family tree and a set of parameter templates, none of which belongs in a
    dropdown, and all of which is a schema this module would then have to keep up
    with.
    """
    if not isinstance(body, dict):
        return []
    entries = body.get("models")
    if not isinstance(entries, list):
        return []
    models: list[dict[str, Any]] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        name = entry.get("name") or entry.get("model")
        if not isinstance(name, str) or not name.strip():
            continue
        row: dict[str, Any] = {"id": name.strip()}
        size = entry.get("size")
        if isinstance(size, int) and size > 0:
            row["size"] = size
        models.append(row)
    # Alphabetical, because a server's own order is pull order, and pull order
    # is not a ranking of anything.
    models.sort(key=lambda row: str(row["id"]).lower())
    return models


def _models_from_openai(body: Any) -> list[dict[str, Any]]:
    """Read the OpenAI-compatible ``/v1/models`` shape.

    The fallback for the servers that speak OpenAI but are not Ollama: LM Studio,
    vLLM, llama.cpp's server, and text-generation-webui. They answer the same
    route with the same field, and none of them answer ``/api/tags``.
    """
    if not isinstance(body, dict):
        return []
    entries = body.get("data")
    if not isinstance(entries, list):
        return []
    models: list[dict[str, Any]] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        name = entry.get("id")
        if not isinstance(name, str) or not name.strip():
            continue
        models.append({"id": name.strip()})
    models.sort(key=lambda row: str(row["id"]).lower())
    return models


def discover(base_url: str, timeout: float = DEFAULT_TIMEOUT) -> dict[str, Any]:
    """List the models a local server is serving.

    Tries the server's native route first and falls back to the OpenAI one,
    because which of the two exists is a property of the server and not something
    to ask the user about.

    Never raises. Every failure is a payload, because the caller is a dropdown
    that needs a reason to show rather than an exception to propagate.
    """
    root = _root(base_url)

    try:
        import httpx
    except ImportError:  # pragma: no cover - httpx ships with the agent
        return {
            "ok": False,
            "baseUrl": base_url,
            "error": "httpx is not installed, so the server could not be asked.",
        }

    started = _now()
    attempts = (
        ("/api/tags", _models_from_tags),
        ("/v1/models", _models_from_openai),
    )
    last = ""

    for path, parse in attempts:
        try:
            response = httpx.get(f"{root}{path}", timeout=timeout)
        except Exception as exc:  # noqa: BLE001 - any failure is a payload
            last = f"{type(exc).__name__}: {exc}".split(" for ")[0]
            continue

        if 200 <= response.status_code < 300:
            try:
                body = response.json()
            except ValueError:
                last = f"{path} answered {response.status_code} with no JSON."
                continue
            models = parse(body)
            return {
                "ok": True,
                "baseUrl": base_url,
                "route": path,
                "latencyMs": _elapsed(started),
                "models": models,
                # Reachable but empty is a distinct state from unreachable, and
                # the two need different words: one means "pull a model", the
                # other means "start the server".
                "detail": ""
                if models
                else "The server is running but has no models. Pull one, then try again.",
            }

        last = f"{path} answered {response.status_code}."

    return {
        "ok": False,
        "baseUrl": base_url,
        "latencyMs": _elapsed(started),
        "error": last or "No response.",
        "detail": (
            f"Nothing answered at {root}. Start the model server, and check that "
            "the address above is where it is listening."
        ),
    }


def _now() -> float:
    import time

    return time.perf_counter()


def _elapsed(started: float) -> int:
    import time

    return int((time.perf_counter() - started) * 1000)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "base_url",
        nargs="?",
        default="",
        help="the server's OpenAI-compatible base URL",
    )
    parser.add_argument(
        "--timeout", type=float, default=DEFAULT_TIMEOUT, help="seconds to wait"
    )
    args = parser.parse_args(argv)

    base_url = (args.base_url or default_base_url()).strip()
    if not base_url:
        base_url = FALLBACK_BASE_URL

    if not base_url.startswith(("http://", "https://")):
        # A bare host or port is the most likely typo, and naming it beats the
        # connection error it would otherwise produce.
        print(
            json.dumps(
                {
                    "ok": False,
                    "baseUrl": base_url,
                    "error": "not_an_address",
                    "detail": (
                        f"{base_url} is not a server address. It should start with "
                        "http:// or https://."
                    ),
                },
                separators=(",", ":"),
            )
        )
        return 0

    payload = discover(base_url, timeout=args.timeout)
    print(json.dumps(payload, separators=(",", ":")))
    # Always zero, because the payload *is* the answer.
    #
    # The desktop layer runs this through a helper that discards stdout when the
    # child exits non-zero, so an exit code of 1 here would throw away the one
    # message worth reading: "nothing is listening at this address". A person
    # would see "discovery failed" instead of the sentence that tells them to
    # start their server.
    #
    # So `ok` in the JSON carries the verdict, and the exit code only reports
    # whether the script ran at all -- which, by the time we get here, it did.
    return 0


if __name__ == "__main__":
    sys.exit(main())
