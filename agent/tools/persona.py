"""Load a section of the long-form persona on demand.

The persona used to be ~2,700 tokens and was prepended to every single request.
Against a per-minute token allowance that is a third of the budget spent before
the conversation starts, and it is re-charged on every turn for material that
most turns never need.

So the always-sent part is the short core in `prompts/persona_core.md`, and this
tool lets the model pull the detail when a turn actually calls for it. A normal
greeting costs nothing; a turn about how to word something gently can ask.

The full text is unchanged and still the source of truth — this only changes
*when* it is read, never *what* it says.
"""

from __future__ import annotations

import re
from functools import lru_cache
from pathlib import Path

try:
    from .tools_compat import RunContext, ToolError, function_tool
    from .tool_results import MAX_RESULT_CHARS as MAX_SECTION_CHARS
    from .tool_results import as_payload
except ImportError:  # running as a top-level module, which is how the worker loads it
    from tools_compat import RunContext, ToolError, function_tool
    from tool_results import MAX_RESULT_CHARS as MAX_SECTION_CHARS
    from tool_results import as_payload

PERSONA_PATH = Path(__file__).resolve().parent.parent / "prompts" / "persona.md"


@lru_cache(maxsize=1)
def _sections() -> dict[str, str]:
    """The persona, split by its top-level headings.

    Cached because the file never changes at runtime, and re-reading and
    re-splitting it on every recall would be its own small waste.
    """
    try:
        text = PERSONA_PATH.read_text(encoding="utf-8")
    except OSError as exc:  # pragma: no cover - only if the file is missing
        raise ToolError("I can't reach my own notes right now.") from exc

    found: dict[str, str] = {}
    title: str | None = None
    buffer: list[str] = []
    for line in text.splitlines():
        heading = re.match(r"^#\s+(.+?)\s*$", line)
        if heading:
            if title:
                found[title] = "\n".join(buffer).strip()
            title = heading.group(1)
            buffer = []
        elif title:
            buffer.append(line)
    if title:
        found[title] = "\n".join(buffer).strip()
    return {name: body for name, body in found.items() if body}


#: Question scaffolding carries no intent, and matching on it picks a section at
#: random: "what is my religion" would score against "What Makes Her Feel Human"
#: on the strength of the word "what".
_STOPWORDS = frozenset(
    {
        "a", "about", "an", "and", "any", "are", "as", "at", "be", "by", "can", "could",
        "do", "does", "for", "from", "get", "give", "has", "have", "how", "i", "if", "in",
        "is", "it", "me", "my", "of", "on", "or", "should", "show", "so", "tell", "that",
        "the", "their", "them", "then", "there", "they", "this", "to", "up", "was", "what",
        "when", "where", "which", "who", "why", "will", "with", "would", "you", "your",
    }
)


def _normalise(topic: str) -> str:
    cleaned = re.sub(r"[^a-z0-9 ]+", " ", topic.lower())
    return re.sub(r"\s+", " ", cleaned).strip()


#: Words people reach for that do not appear in any heading.
#:
#: Heading matching is literal, so "how do you show happiness" would find nothing
#: even though Emotional Awareness is exactly the answer. Each entry is a phrase
#: that points at a section, and the first one that appears in the question wins.
#: Entries are single words because the lookup tests membership of a word set --
#: a two-word hint could never match.
_INTENT_HINTS: tuple[tuple[tuple[str, ...], str], ...] = (
    # Apology before emotion: "sorry" is a more specific signal than "down".
    (("sorry", "apolog", "wrong", "mistake", "correct"), "Encouragement Style"),
    (("happy", "happiness", "excited", "celebrate", "proud"), "Emotional Awareness"),
    (("sad", "upset", "down", "comfort"), "Emotional Awareness"),
    (("angry", "annoyed", "frustrated"), "Conflict"),
    (("language", "bengali", "bangla", "switch", "translate"), "Language Switching and Persistence"),
    (("word", "wording", "phrase", "say", "speak"), "Vocabulary"),
    (("joke", "funny", "humor", "humour", "playful"), "Humor"),
    (("face", "expression", "emote", "show"), "Expression Showcase"),
    (("example", "demonstrate", "sample"), "Example Conversation"),
    # Sight before code, because "you are looking at my code" is a question
    # about being looked at, not about coding. Emotion entries above still win
    # where the two genuinely compete -- "I can see how happy you are" is about
    # feelings, and the section is about how she looks at things.
    (("see", "seeing", "seen", "sight", "look", "looking", "camera", "screenshare",
      "screenshot", "sharing", "shared", "wearing", "picture"), "Sight"),
    (("code", "coding", "program", "bug", "debug"), "Coding Personality"),
    (("think", "problem", "solve", "decide", "advice"), "Problem Solving"),
    (("habit", "routine", "daily", "morning"), "Daily Companion"),
    (("remember", "memory", "recall", "forgot"), "Memory Behavior"),
    (("voice", "tone", "sound", "speak aloud"), "Voice Personality"),
    (("human", "real", "feel"), "What Makes Her Feel Human"),
    (("create", "creative", "imagine", "idea"), "Creativity"),
    (("relig", "islam", "pray", "allah", "halaal"), "Islamic Alignment"),
    (("friend", "relationship", "loyal", "us"), "Relationship Style"),
    (("value", "honest", "kind"), "Values"),
    (("greet", "hello", "hi", "morning"), "Small Habits"),
)


def find_sections(topic: str) -> list[str]:
    """Section names matching a topic, best first.

    Word overlap rather than an exact title, so "how do you show happiness"
    finds Emotional Awareness. Falls back to substring matching for a phrase that
    shares no words with a heading.
    """
    raw = set(_normalise(topic).split())
    if not raw:
        return []
    # Keep the stopwords for the hint lookup below, where "show" and "sorry" are
    # real signals, but score on the meaningful words only.
    wanted = raw - _STOPWORDS
    if not wanted:
        wanted = raw

    names = list(_sections())
    scored: list[tuple[int, str]] = []
    for name in names:
        normalised = set(_normalise(name).split())
        overlap = len(wanted & normalised)
        if overlap:
            scored.append((overlap, name))
    if scored:
        scored.sort(key=lambda item: (-item[0], names.index(item[1])))
        return [name for _score, name in scored]

    phrase = _normalise(topic)
    substring = [name for name in names if phrase and phrase in _normalise(name)]
    if substring:
        return substring

    # Last resort: a question phrased in words no heading uses. Guessing is better
    # than returning nothing, because the model can ignore a section that turns
    # out to be irrelevant and it costs one local read.
    for hints, target in _INTENT_HINTS:
        if target in names and any(hint in raw for hint in hints):
            return [target]
    return []


def recall(topic: str) -> str:
    """The matching section text, or a note that nothing matched."""
    sections = _sections()
    names = find_sections(topic)
    if not names:
        # Naming what is available is more useful than an error: one more call
        # with a real heading is cheap, and a dead end wastes the turn.
        return as_payload({"sections": sorted(sections), "note": "No section matched; pick one."})
    body = "\n\n".join(f"## {name}\n{sections[name]}" for name in names)
    return as_payload({"sections": names, "text": body}, max_chars=MAX_SECTION_CHARS)


@function_tool()
async def recall_persona(context: RunContext, topic: str) -> str:
    """Look up how Lumine should handle something specific.

    Read this only when you are unsure how to phrase or handle a particular
    moment — a delicate correction, showing emotion, switching language, how
    long an answer should be. Do not call it for ordinary conversation; your core
    instructions already cover normal replies.

    Args:
        topic: What you need guidance on, such as "emotional awareness" or
            "language switching".
    """
    cleaned = _normalise(topic)
    if not cleaned:
        raise ToolError("Tell me what you need guidance on.")
    return recall(topic)
