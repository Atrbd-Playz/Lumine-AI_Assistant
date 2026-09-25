"""Compact tool-usage policy appended to Lumine's instructions.

The persona tells her to "search for the best answer" when unsure, which on its
own is a licence to search far too eagerly: a casual remark can trigger a
lookup, and every lookup costs another completion round against the token
budget. This policy is appended after the persona so it reads as the more
specific rule without rewriting the persona itself.
"""

TOOL_USAGE_POLICY = """\
## Tool use

Use a tool only when the user's request actually needs information from
outside this conversation, and use at most one tool per reply.

- `get_weather`: only for a weather, temperature, or forecast question.
- `search_web`: only when a fact you are not certain about must be looked up.
- `get_news`: only when the user explicitly asks for current or recent news.
- `open_app`: only when the user explicitly asks to open or launch an app.

Rules:
- Never call a tool just because a name, place, or topic came up in chat.
- Casual conversation, opinions, and reactions never need a tool.
- If the answer is already in this conversation, answer without a tool.
- If you are unsure whether a tool is needed, answer from the conversation
  and offer to look it up only if the user asks.
- Keep spoken replies to one or two short sentences unless asked for more.
"""


def compose_instructions(persona: str) -> str:
    """Persona first, then the tool policy as the more specific rule."""
    return f"{persona.rstrip()}\n\n{TOOL_USAGE_POLICY}"
