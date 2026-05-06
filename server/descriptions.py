"""LLM-powered description and Q&A generation, grounded by Deezer metadata.

One call to ``gpt-4.1`` per request. The prompts instruct the model
to output ``NONE`` when it isn't confident, which we translate to an
empty string. Callers cache as they see fit.
"""

import os

from loguru import logger
from openai import AsyncOpenAI

_MODEL = "gpt-4.1"

_PROMPT = """You're writing a description for a voice-driven music player app. The text will be both displayed on screen and spoken aloud by a text-to-speech engine.

Item name: {name}
Item kind: {kind}
Artist: {artist_name}
Year: {year}
Genre tags: {genres}
Release type: {record_type}
Deezer popularity: {fans} fans

Write {length_instruction} in plain spoken prose. Avoid markdown, bullet points, lists, emoji, or special characters. Use concrete, factual details when you are confident.

If you do not have confident, specific knowledge about this exact item, output the single word NONE and nothing else. Do not invent facts."""

_LENGTH_INSTRUCTIONS = {
    "short": "exactly one sentence, fifteen words or fewer",
    "long": "four to five sentences, under 120 words total",
}

_client: AsyncOpenAI | None = None


def _get_client() -> AsyncOpenAI:
    global _client
    if _client is None:
        _client = AsyncOpenAI(api_key=os.getenv("OPENAI_API_KEY"))
    return _client


async def generate_description(
    *,
    kind: str,
    depth: str,
    name: str,
    artist_name: str,
    year: int | None = None,
    genres: list[str] | None = None,
    record_type: str | None = None,
    fans: int | None = None,
) -> str:
    """Generate a ``short`` or ``long`` description for an item.

    Returns an empty string if the LLM refuses or the call fails.
    """
    length_instruction = _LENGTH_INSTRUCTIONS.get(depth, _LENGTH_INSTRUCTIONS["long"])
    prompt = _PROMPT.format(
        name=name,
        kind=kind,
        artist_name=artist_name or "—",
        year=year if year else "—",
        genres=", ".join(genres) if genres else "—",
        record_type=record_type or "—",
        fans=fans if fans is not None else "—",
        length_instruction=length_instruction,
    )

    try:
        completion = await _get_client().chat.completions.create(
            model=_MODEL,
            messages=[{"role": "user", "content": prompt}],
            temperature=0.4,
            max_tokens=260,
        )
    except Exception as exc:
        logger.warning(f"description generation failed for {kind} '{name}': {exc}")
        return ""

    text = (completion.choices[0].message.content or "").strip()
    if not text or text.upper() == "NONE":
        return ""
    return text
