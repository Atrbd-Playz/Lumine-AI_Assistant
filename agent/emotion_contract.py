import re
from typing import Any, Literal, TypedDict

CANONICAL_EMOTIONS = [
    "neutral",
    "happy",
    "loving",
    "excited",
    "playful",
    "jealous",
    "wink",
    "sleepy",
    "sad",
    "surprised",
    "embarrassed",
    "shy",
    "confused",
    "thinking",
    "angry",
    "curious",
    "focused",
    "proud",
    "worried",
    "idle",
]

CANONICAL_EMOTION_SET = set(CANONICAL_EMOTIONS)
EMOTION_ALIASES = {
    "idle": "neutral",
    "neutral": "neutral",
    "default": "neutral",
    "none": "neutral",
}

EmotionPrimary = Literal[
    "neutral",
    "happy",
    "loving",
    "excited",
    "playful",
    "jealous",
    "wink",
    "sleepy",
    "sad",
    "surprised",
    "embarrassed",
    "shy",
    "confused",
    "thinking",
    "angry",
    "curious",
    "focused",
    "proud",
    "worried",
]

EmotionSource = Literal["llm", "heuristic", "voice", "system", "user", "lab"]

class EmotionIntent(dict):
    def __init__(
        self,
        *,
        primary: EmotionPrimary = "neutral",
        secondary: EmotionPrimary | None = None,
        intensity: float = 0.5,
        duration_ms: int | None = None,
        source: EmotionSource = "system",
        priority: int | None = 0,
    ) -> None:
        super().__init__(
            primary=primary,
            secondary=secondary,
            intensity=float(intensity),
            duration_ms=duration_ms,
            source=source,
            priority=priority,
        )

    def __getattr__(self, name: str):
        try:
            return self[name]
        except KeyError as exc:  # pragma: no cover - attribute access guard
            raise AttributeError(name) from exc

    def __setattr__(self, name: str, value: Any) -> None:
        self[name] = value


class EmotionPayload(dict):
    def __init__(
        self,
        *,
        primary: EmotionPrimary = "neutral",
        secondary: EmotionPrimary | None = None,
        intensity: float = 0.5,
        durationMs: int | None = None,
        source: EmotionSource = "system",
        priority: int | None = 0,
    ) -> None:
        super().__init__(
            primary=primary,
            secondary=secondary,
            intensity=float(intensity),
            durationMs=durationMs,
            source=source,
            priority=priority,
        )

    def __getattr__(self, name: str):
        try:
            return self[name]
        except KeyError as exc:
            raise AttributeError(name) from exc

    def __setattr__(self, name: str, value: Any) -> None:
        self[name] = value


class EmotionResponse(dict):
    def __init__(self, *, response: str = "", emotion: EmotionPayload | dict[str, Any] | None = None) -> None:
        super().__init__(response=response, emotion=emotion or EmotionPayload())

    def __getattr__(self, name: str):
        try:
            return self[name]
        except KeyError as exc:
            raise AttributeError(name) from exc

    def __setattr__(self, name: str, value: Any) -> None:
        self[name] = value


def normalize_emotion(value: Any) -> str:
    if not isinstance(value, str):
        return "neutral"
    cleaned = value.strip().lower().replace("-", "_")
    if not cleaned:
        return "neutral"
    if cleaned in EMOTION_ALIASES:
        return EMOTION_ALIASES[cleaned]
    if cleaned in CANONICAL_EMOTION_SET:
        return cleaned
    return "neutral"


def clamp_intensity(value: Any) -> float:
    try:
        amount = float(value)
    except (TypeError, ValueError):
        return 0.5
    if amount < 0:
        return 0.0
    if amount > 1:
        return 1.0
    return amount


def validate_emotion_event(event: Any) -> bool:
    if not isinstance(event, dict):
        return False
    primary = normalize_emotion(event.get("primary"))
    secondary = event.get("secondary")
    if primary not in CANONICAL_EMOTION_SET:
        return False
    if secondary is not None:
        secondary = normalize_emotion(secondary)
        if secondary not in CANONICAL_EMOTION_SET:
            return False
    intensity = clamp_intensity(event.get("intensity", 0.5))
    if intensity < 0.0 or intensity > 1.0:
        return False
    source = str(event.get("source", "system")).lower()
    if source not in {"llm", "heuristic", "voice", "system", "user", "lab"}:
        return False
    return True


def build_emotion_event(payload: dict[str, Any]) -> dict[str, Any]:
    primary = normalize_emotion(payload.get("primary", "neutral"))
    secondary = payload.get("secondary")
    event = {
        "version": 1,
        "type": "lumine.emotion",
        "payload": {
            "primary": primary,
            "secondary": normalize_emotion(secondary) if secondary is not None else None,
            "intensity": clamp_intensity(payload.get("intensity", 0.0)),
            "durationMs": int(payload.get("durationMs", 0)) if payload.get("durationMs") is not None else None,
            "source": str(payload.get("source", "system")).lower() if str(payload.get("source", "system")).lower() in {"llm", "heuristic", "voice", "system", "user", "lab"} else "system",
            "priority": int(payload.get("priority", 0)) if payload.get("priority") is not None else 0,
        },
    }
    if not validate_emotion_event(event["payload"]):
        event["payload"] = {
            "primary": "neutral",
            "secondary": None,
            "intensity": 0.0,
            "durationMs": None,
            "source": "system",
            "priority": 0,
        }
    return event


def parse_llm_emotion_response(data: Any) -> tuple[str, dict[str, Any]]:
    if not isinstance(data, dict):
        return "", {"primary": "neutral", "secondary": None, "intensity": 0.0, "source": "system"}

    response_text = str(data.get("response", "")).strip()
    emotion = data.get("emotion") if isinstance(data.get("emotion"), dict) else {}
    normalized = {
        "primary": normalize_emotion(emotion.get("primary", "neutral")),
        "secondary": normalize_emotion(emotion.get("secondary")) if emotion.get("secondary") is not None else None,
        "intensity": clamp_intensity(emotion.get("intensity", 0.0)),
        "source": str(emotion.get("source", "llm")).lower() if str(emotion.get("source", "llm")).lower() in {"llm", "heuristic", "voice", "system", "user", "lab"} else "llm",
    }
    if not validate_emotion_event(normalized):
        normalized = {"primary": "neutral", "secondary": None, "intensity": 0.0, "source": "llm"}
    return response_text, normalized


def strict_llm_schema() -> dict[str, Any]:
    return {
        "type": "json_schema",
        "json_schema": {
            "name": "LumineResponseWithEmotion",
            "schema": {
                "type": "object",
                "properties": {
                    "response": {"type": "string"},
                    "emotion": {
                        "type": "object",
                        "properties": {
                            "primary": {"type": "string", "enum": list(CANONICAL_EMOTIONS)},
                            "secondary": {"type": ["string", "null"], "enum": [*list(CANONICAL_EMOTIONS), None]},
                            "intensity": {"type": "number", "minimum": 0, "maximum": 1},
                            "source": {"type": "string", "enum": ["llm", "heuristic", "voice", "system", "user", "lab"]},
                            "durationMs": {"type": ["integer", "null"], "minimum": 0},
                        },
                        "required": ["primary", "intensity", "source"],
                        "additionalProperties": False,
                    },
                },
                "required": ["response", "emotion"],
                "additionalProperties": False,
            },
            "strict": True,
        },
    }
