"""LLM-powered description and Q&A generation, grounded by Deezer metadata.

One call to ``gpt-4o-mini`` per request. The prompts instruct the model
to output ``NONE`` when it isn't confident, which we translate to an
empty string. Callers cache as they see fit.
"""

import os

from loguru import logger
from openai import AsyncOpenAI

_MODEL = "gpt-4o-mini"

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


_QA_PROMPTS = {
    "catalog": """You're answering a question about a music artist's catalog inside a voice-driven music player. Answer from the structured data below. Do not speculate beyond it.

Artist: {artist_name}
{focus_section}Albums (ordered by Deezer):
{album_list}
Top songs (Deezer top tracks):
{song_list}

User question: {question}

Reply in one or two short spoken sentences (no markdown, lists, or symbols). If the question cannot be answered from the data above, say so plainly ("I don't have that information"). Never guess.""",
    "music": """You're a knowledgeable music concierge for a voice-driven music player. Answer conversationally, grounded by the artist's catalog below. Use your training knowledge for opinion or trivia questions, but only when you are confident.

Artist: {artist_name}
{focus_section}Albums (ordered by Deezer):
{album_list}
Top songs (Deezer top tracks):
{song_list}

User question: {question}

Reply in one to three short spoken sentences (no markdown, lists, or symbols). If you are not confident or the question is outside what you can reliably answer, say so plainly instead of guessing.""",
}


async def answer_question(
    *,
    mode: str,
    question: str,
    artist_name: str,
    albums: list[dict],
    songs: list[dict],
    about: str | None = None,
    about_tracks: list[dict] | None = None,
) -> str:
    """Generate a spoken answer to ``question`` grounded by the given catalog.

    ``mode`` is ``"catalog"`` for factual questions derivable from the
    structured data (latest, first, count, duration, release year) and
    ``"music"`` for trivia / opinion that should draw on training
    knowledge.

    When ``about`` is set, the prompt includes a "Focus item" line so
    the LLM can resolve deictic references like "this album" or "this
    song" without guessing. Pass ``about_tracks`` when ``about``
    names an album and you have its tracklist; the inner LLM uses it
    to reason about "the best song on this album" and similar.

    Returns an empty string if the model declines or the call fails.
    """
    template = _QA_PROMPTS.get(mode) or _QA_PROMPTS["catalog"]

    def fmt_album(a: dict) -> str:
        year = a.get("year") or "unknown"
        return f"- {a.get('title', '')} ({year})"

    def fmt_song(s: dict) -> str:
        return f"- {s.get('title', '')}"

    focus_lines: list[str] = []
    if about:
        focus_lines.append(f"Focus item (the user's 'this'/'that' refers to): {about}")
        if about_tracks:
            track_list = "\n".join(
                f"  - {i + 1}. {t.get('title', '')}" for i, t in enumerate(about_tracks)
            )
            focus_lines.append(f"Tracklist for {about}:\n{track_list}")
    focus_section = ("\n".join(focus_lines) + "\n\n") if focus_lines else ""

    prompt = template.format(
        artist_name=artist_name or "—",
        focus_section=focus_section,
        album_list="\n".join(fmt_album(a) for a in albums) or "—",
        song_list="\n".join(fmt_song(s) for s in songs) or "—",
        question=question,
    )

    try:
        completion = await _get_client().chat.completions.create(
            model=_MODEL,
            messages=[{"role": "user", "content": prompt}],
            temperature=0.3 if mode == "catalog" else 0.5,
            max_tokens=180,
        )
    except Exception as exc:
        logger.warning(f"Q&A generation failed ({mode}): {exc}")
        return ""

    text = (completion.choices[0].message.content or "").strip()
    if not text or text.upper() == "NONE":
        return ""
    return text
