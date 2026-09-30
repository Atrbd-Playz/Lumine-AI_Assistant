import re
from typing import Any, Literal

CANONICAL_EMOTIONS = [
    "neutral",
    "happy",
    "loving",
    "delighted",
    "amused",
    "excited",
    "playful",
    "mischievous",
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
    "relieved",
    "determined",
    "calm",
    "alert",
    "concerned",
    "idle",
]

CANONICAL_EMOTION_SET = set(CANONICAL_EMOTIONS)
EMOTION_ALIASES = {
    "idle": "neutral",
    "neutral": "neutral",
    "default": "neutral",
    "none": "neutral",
    "delighted": "delighted",
    "amused": "amused",
    "mischievous": "mischievous",
    "relieved": "relieved",
    "determined": "determined",
    "calm": "calm",
    "alert": "alert",
    "concerned": "concerned",
}

EmotionPrimary = Literal[
    "neutral",
    "happy",
    "loving",
    "delighted",
    "amused",
    "excited",
    "playful",
    "mischievous",
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
    "relieved",
    "determined",
    "calm",
    "alert",
    "concerned",
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
        except KeyError as exc:
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


EMOTION_HINTS: dict[str, list[str]] = {
    "happy": [
        "happy", "smile", "smiling", "grin", "laugh", "cheerful", "joyful", "glad", "delighted",
        "thrilled", "pleasant", "yay", "so happy", "i'm glad", "beautiful", "that makes me smile",
        "খুশি", "আনন্দ", "ভালো", "সুখী", "হাসি", "মজা", "মজার", "সুন্দর", "سعيد", "ممتاز",
        "أحب", "رائع", "ضحك", "فرح",
    ],
    "delighted": [
        "delighted", "overjoyed", "ecstatic", "thrilled", "blissful", "giddy", "amazing",
        "wonderful", "অসাধারণ", "ভীষণ খুশি", "আনন্দিত", "مبهج", "مستمتع", "ممتاز", "روعه",
    ],
    "amused": [
        "amused", "amuse", "giggle", "chuckle", "tease", "banter", "joke", "funny", "silly",
        "হাসি", "মজার", "কৌতুক", "তামাশা", "مزاح", "مضحك", "بريء",
    ],
    "excited": [
        "excited", "buzzing", "energized", "let's go", "this is huge", "so exciting", "can't wait",
        "eager", "উচ্ছ্বসিত", "আনন্দিত", "দারুণ", "চলুন", "বিস্মিত", "متحمس", "مثير", "استثنائي", "هيا",
    ],
    "playful": [
        "playful", "cute", "adorable", "act cute", "be cute", "tease", "silly", "flirty", "wink",
        "naughty", "হেহে", "মজার", "কৌতুক", "চুটকি", "সিলি", "আদর", "مزاح", "مضحك", "لطيف",
    ],
    "mischievous": [
        "mischievous", "scheming", "sneaky", "plotting", "secretive", "trickster", "little prank",
        "naughty", "চতুর", "খেলা", "গোপন", "মراوغ", "মকائد", "مزاح",
    ],
    "jealous": [
        "jealous", "act jealous", "be jealous", "who is she", "you seem interested in her",
        "i'm not jealous", "hmm, who's that", "envy", "হিংসা", "ঈর্ষা", "কার সাথে", "উৎসুক",
        "حسد", "منزعج", "من يـهي؟", "غيرة",
    ],
    "thinking": [
        "let me think", "hmm", "consider", "ponder", "evaluate", "analyze", "thinking",
        "reflecting", "চিন্তা কর", "হুম", "বিবেচনা", "ভেবে দেখি", "আলোচনা", "ফিরে", "ফকর",
        "أفكر", "اعتبر", "حلل",
    ],
    "confused": [
        "confused", "unclear", "not sure", "what do you mean", "huh", "i don't get it",
        "puzzled", "mixed up", "ভুল বুঝেছি", "বুঝতে পারছি না", "কী বলছ", "হু", "غير واضح",
        "لا أفهم", "مربك", "ما الذي تقصده",
    ],
    "sad": [
        "sad", "down", "upset", "hurt", "disappointed", "lonely", "teary", "crying", "downcast",
        "দুঃখ", "বিষণ্ণ", "আহত", "হতাশ", "একাকী", "দুর্বল", "حزين", "مكتئব", "মؤلم", "آسف",
    ],
    "angry": [
        "angry", "mad", "furious", "annoyed", "frustrated", "that's not fair", "i'm upset",
        "rage", "irritated", "রাগ", "ক্রুদ্ধ", "চট", "অপমান", "অন্যায়", "غاضب", "مستاء",
        "غير عادل", "متوتر",
    ],
    "curious": [
        "curious", "wonder", "ask", "learn", "tell me more", "what happened", "tell me",
        "inquisitive", "কৌতূহল", "জানতে চাই", "বিস্তারিত", "কি ঘটেছে", "استفسار", "أرغب في معرفة",
        "ما الذي حدث",
    ],
    "focused": [
        "focus", "important", "urgent", "serious", "need to work", "we need to fix this",
        "locked in", "concentrated", "ফোকাস", "গুরুত্বপূর্ণ", "জরুরি", "গুরুতর", "কাজ করতে",
        "تركيز", "مهم", "عاجل", "نحتاج إلى إصلاح",
    ],
    "proud": [
        "proud", "great job", "excellent", "accomplished", "well done", "nice work", "confident",
        "heroic", "গর্ব", "ভাল কাজ", "অসাধারণ", "সফল", "চমৎকার", "ফুর", "عمل رائع", "ممتاز",
        "ফخور",
    ],
    "worried": [
        "worried", "nervous", "afraid", "stress", "anxious", "unsafe", "i'm scared", "fearful",
        "tense", "চিন্তিত", "ভয়", "ডর", "বিষণ্ন", "অস্বস্তি", "قلق", "خائف", "متوتر", "غير آمن",
    ],
    "surprised": [
        "surprised", "wow", "oh wow", "unexpected", "impossible", "that shocked me", "omg",
        "shocked", "আশ্চর্য", "ওহ", "অপ্রত্যাশিত", "চমকে গেছি", "মন্দহশ", "مفاجأة", "wow",
        "أوه",
    ],
    "shy": [
        "shy", "blush", "blushing", "embarrassed", "awkward", "timid", "bashful", "soft voice",
        "লজ্জা", "শরম", "অস্বস্তি", "হতাশ", "خجل", "محرج", "মكتুম", "লাজুক",
    ],
    "embarrassed": [
        "embarrassed", "ashamed", "awkward", "i feel silly", "red face", "flustered", "cringe",
        "লজ্জিত", "শরম", "হতাশ", "অস্বস্তি", "মরজ", "খজোল", "أشعر بالغباء", "محرج",
    ],
    "loving": [
        "aww", "i'm really glad you're here", "i care about you", "sweet", "you're important to me",
        "affectionate", "love", "আহা", "ভালোবাসি", "প্রেম", "তোমাকে ভালোবাসি", "তুমি গুরুত্বপূর্ণ",
        "أوه", "أحبك", "أهتم بك", "رقيق",
    ],
    "relieved": [
        "relieved", "phew", "finally", "safe now", "calmed down", "comforted", "breath easy",
        "স্বস্তি", "শেষে", "শান্তি", "মুক্তি", "راحة", "آمان",
    ],
    "determined": [
        "determined", "resolve", "commit", "will do it", "get it done", "decisive",
        "focused on fixing", "নির্ধারিত", "তৈরি", "চূড়ান্ত", "কাজ করতে", "عزم", "مصر",
    ],
    "calm": [
        "calm", "steady", "peaceful", "relax", "easy", "comfortable", "centered", "শান্ত",
        "নির্মল", "ধীরে", "راحة", "هادئ", "سهل",
    ],
    "alert": [
        "alert", "on guard", "listening", "sharp", "aware", "ready", "attentive", "সজাগ",
        "মনোযোগ", "দৃষ্টি", "মستيقظ", "جاهز", "منتبه",
    ],
    "concerned": [
        "concerned", "worried", "careful", "unsafe", "watch out", "cautious", "hmm maybe",
        "ফিকর", "চিন্তিত", "সতর্ক", "মقلق", "انتباه", "حذر",
    ],
}

ROLEPLAY_PATTERNS = [
    "act shy", "be shy", "blush", "blushing",
    "act happy", "be happy", "smile", "laugh",
    "act excited", "be excited", "get excited",
    "act proud", "be proud", "stand proud",
    "act angry", "be angry", "get angry",
    "act jealous", "be jealous", "get jealous",
    "act confused", "be confused",
    "act curious", "be curious",
    "act calm", "be calm",
    "act surprised", "be surprised",
    "act playful", "be playful", "wink",
    "act worried", "be worried",
    "act sad", "be sad",
    "act loving", "be loving",
    "act determined", "be determined",
    "act focused", "be focused",
    "roleplay", "pretend to be",
]


def _split_emotion_segments(text: str) -> list[str]:
    cleaned = re.sub(r"\s+", " ", text.strip())
    if not cleaned:
        return []
    segments = re.split(r"(?:then|and|but|while|so|as|because)|[;,.!?]+", cleaned)
    segments = [segment.strip() for segment in segments if segment and segment.strip()]
    return segments or [cleaned]


def _emotion_score(text: str, emotion: str) -> int:
    lower = text.lower()
    keywords = EMOTION_HINTS.get(emotion, [])
    score = 0
    for keyword in keywords:
        if keyword in lower:
            score += 3 if len(keyword) <= 4 else 2
    for pattern in (f"act {emotion}", f"be {emotion}", f"get {emotion}", f"look {emotion}", f"feel {emotion}"):
        if pattern in lower:
            score += 5
    return score


def extract_emotion_sequence(text: str, source: str = "llm") -> list[dict[str, object]]:
    if not text:
        return []

    events: list[dict[str, object]] = []
    for segment in _split_emotion_segments(text):
        scored: list[tuple[str, int]] = []
        for emotion in EMOTION_HINTS:
            score = _emotion_score(segment, emotion)
            if score:
                scored.append((emotion, score))
        if not scored:
            continue
        scored.sort(key=lambda item: item[1], reverse=True)
        primary = scored[0][0]
        secondary = scored[1][0] if len(scored) > 1 and scored[1][1] >= 2 else None
        intensity = min(1.0, max(0.0, 0.35 + (scored[0][1] * 0.08)))
        events.append(
            build_emotion_event({
                "primary": primary,
                "secondary": secondary,
                "intensity": round(intensity, 2),
                "source": "user" if source == "user" else "heuristic",
                "priority": 5,
                "durationMs": max(900, min(2200, 500 + len(segment) * 18)),
            })
        )

    if not events:
        return [
            build_emotion_event({
                "primary": "neutral",
                "secondary": None,
                "intensity": 0.0,
                "source": "user" if source == "user" else "heuristic",
                "durationMs": 700,
            })
        ]
    return events


def infer_emotion_from_text(text: str, source: str = "llm") -> dict[str, object] | None:
    sequence = extract_emotion_sequence(text, source=source)
    if not sequence:
        return None
    return sequence[0]


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
