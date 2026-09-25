"""Text helpers shared by Lumine's tools.

Every tool returns a short human-readable string instead of a raw API payload,
so results stay small in the model's context window.
"""

import unicodedata

ELLIPSIS = "..."

# Symbols and emoji: useless to a voice agent, expensive in context, and not
# printable on a cp1252 console. Letters, digits, and punctuation are kept in
# every script, so Bangla, Arabic, and other supported text is untouched.
_DROP_CATEGORIES = frozenset({"So", "Sk", "Cs"})
_DROP_CHARACTERS = frozenset("\u200b\u200d\u2060\ufeff\ufe0e\ufe0f")

# Symbols worth keeping because they carry meaning in a spoken summary.
_KEEP_CHARACTERS = frozenset("\u00b0")


def strip_symbols(text: str) -> str:
    """Remove emoji, symbol, and invisible characters while keeping all text."""
    return "".join(
        character
        for character in text
        if character in _KEEP_CHARACTERS
        or (
            unicodedata.category(character) not in _DROP_CATEGORIES
            and character not in _DROP_CHARACTERS
        )
    )


def clip(text: object, limit: int = 160) -> str:
    """Strip symbols, collapse whitespace, and cap length for the model."""
    cleaned = strip_symbols("" if text is None else str(text))
    cleaned = " ".join(cleaned.split())
    if len(cleaned) <= limit:
        return cleaned
    return cleaned[: max(limit - len(ELLIPSIS), 1)].rstrip() + ELLIPSIS
