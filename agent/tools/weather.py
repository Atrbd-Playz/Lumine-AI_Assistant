"""Weather tool backed by Open-Meteo: free, keyless, and account-free."""

from __future__ import annotations

import httpx

from .http_client import shared_client
from . import tool_results
from .tools_compat import RunContext, ToolError, function_tool
from .tools_text import clip

GEOCODE_URL = "https://geocoding-api.open-meteo.com/v1/search"
FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
TIMEOUT = httpx.Timeout(8.0)

# WMO weather interpretation codes, worded so they read naturally aloud.
WEATHER_CODES: dict[int, str] = {
    0: "clear sky",
    1: "mainly clear",
    2: "partly cloudy",
    3: "overcast",
    45: "fog",
    48: "depositing rime fog",
    51: "light drizzle",
    53: "drizzle",
    55: "heavy drizzle",
    56: "light freezing drizzle",
    57: "freezing drizzle",
    61: "light rain",
    63: "rain",
    65: "heavy rain",
    66: "light freezing rain",
    67: "freezing rain",
    71: "light snow",
    73: "snow",
    75: "heavy snow",
    77: "snow grains",
    80: "light showers",
    81: "rain showers",
    82: "violent rain showers",
    85: "light snow showers",
    86: "snow showers",
    95: "thunderstorm",
    96: "thunderstorm with hail",
    99: "thunderstorm with heavy hail",
}


def _number(value: object) -> str:
    try:
        return str(round(float(value)))
    except (TypeError, ValueError):
        return ""


def _degrees(value: object) -> str:
    number = _number(value)
    return f"{number}\u00b0C" if number else ""


def _as_int(value: object) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _as_code(value: object) -> int:
    code = _as_int(value)
    return code if code is not None else -1


def _at(values: object, index: int) -> object:
    if isinstance(values, list) and index < len(values):
        return values[index]
    return None


def _place_label(place: dict) -> str:
    parts: list[str] = []
    for key in ("name", "admin1", "country"):
        value = str(place.get(key) or "").strip()
        if value and value not in parts:
            parts.append(value)
    return clip(", ".join(parts), 60) or "that location"


def format_weather(place: dict, forecast: dict) -> str:
    """Render an Open-Meteo payload as a compact record for the model.

    Only what a reply would actually say: current conditions, and the next two
    days. Everything else the API returns is dropped at the door -- the raw
    payload never reaches the model's context, and a tool result is re-sent on
    every later turn of the conversation, so each field here is paid for again
    and again.

    Keyed JSON rather than prose: the model can pick the one number it needs
    without reading a sentence, and it costs fewer tokens than the filler words
    that make prose readable to a person.
    """
    current = forecast.get("current") or {}
    daily = forecast.get("daily") or {}

    temp = _degrees(current.get("temperature_2m"))
    feels = _degrees(current.get("apparent_temperature"))
    condition = WEATHER_CODES.get(_as_code(current.get("weather_code")))

    if not temp and not condition:
        return tool_results.as_payload({"place": _place_label(place), "now": "unavailable"})

    now = temp or ""
    if feels and feels != temp:
        now += f", feels {feels}"
    if condition:
        now += f", {condition}"

    humidity = _as_int(current.get("relative_humidity_2m"))
    wind = _as_int(current.get("wind_speed_10m"))
    details = []
    if humidity is not None:
        details.append(f"humidity {humidity}%")
    if wind is not None:
        details.append(f"wind {wind} km/h")

    record: dict[str, object] = {"place": _place_label(place), "now": now}
    if details:
        record["details"] = ", ".join(details)

    times = daily.get("time") or []
    for index, label in enumerate(("today", "tomorrow")):
        if index >= len(times):
            break
        high = _number(_at(daily.get("temperature_2m_max"), index))
        low = _number(_at(daily.get("temperature_2m_min"), index))
        if not high and not low:
            continue
        # A range, not two units: "28-33", never "28-33C-C".
        entry = f"{low}-{high}" if low and high else f"{high or low}"
        chance = _at(daily.get("precipitation_probability_max"), index)
        if isinstance(chance, (int, float)):
            entry += f", {round(chance)}% rain"
        record[label] = entry

    return tool_results.as_payload(record)

async def _geocode(client: httpx.AsyncClient, location: str) -> dict:
    response = await client.get(
        GEOCODE_URL,
        params={"name": location, "count": 1, "language": "en", "format": "json"},
        timeout=TIMEOUT,
    )
    response.raise_for_status()
    results = response.json().get("results") or []
    if not results:
        raise ToolError(f"I couldn't find a place called '{clip(location, 40)}'.")
    return results[0]


async def _forecast(client: httpx.AsyncClient, place: dict) -> dict:
    response = await client.get(
        FORECAST_URL,
        params={
            "latitude": place.get("latitude"),
            "longitude": place.get("longitude"),
            "current": (
                "temperature_2m,relative_humidity_2m,apparent_temperature,"
                "weather_code,wind_speed_10m"
            ),
            "daily": "temperature_2m_max,temperature_2m_min,precipitation_probability_max",
            "timezone": "auto",
            "forecast_days": 2,
        },
        timeout=TIMEOUT,
    )
    response.raise_for_status()
    return response.json()


@function_tool()
async def get_weather(context: RunContext, location: str) -> str:
    """Check the weather somewhere and give a short spoken summary.

    Use it whenever the user asks about temperature, rain, or whether it is a
    good time to go outside. Returns current conditions plus today's and
    tomorrow's high and low, ready to paraphrase.

    Args:
        location: City or place name, such as "Dhaka" or "Paris, France".
    """
    query = clip(location, 80).strip()
    if not query:
        raise ToolError("Tell me which place to check the weather for.")

    try:
        async with shared_client() as client:
            place = await _geocode(client, query)
            forecast = await _forecast(client, place)
    except httpx.HTTPError as exc:
        raise ToolError("The weather service didn't respond, try again shortly.") from exc
    except ValueError as exc:
        raise ToolError("The weather service returned an unexpected response.") from exc

    return format_weather(place, forecast)
