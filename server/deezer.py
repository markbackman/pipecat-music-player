"""Async Deezer API HTTP helpers.

Deezer's public read endpoints are keyless and require no OAuth. This
module wraps ``urllib`` in ``asyncio.to_thread`` so the agents can use
it without introducing an ``aiohttp`` dependency.
"""

import asyncio
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request

BASE_URL = "https://api.deezer.com"
USER_AGENT = "music-player-demo/1.0 (+pipecat-subagents)"
RETRY_BACKOFF_S = 3.0
REQUEST_TIMEOUT_S = 15.0


def normalize_name(s: str) -> str:
    """Lowercase, collapse non-alphanumeric runs to single spaces, strip ends."""
    return re.sub(r"[^a-z0-9]+", " ", s.lower()).strip()


def _sync_get(url: str) -> dict | list:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    for attempt in range(2):
        try:
            with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT_S) as resp:
                return json.loads(resp.read())
        except urllib.error.HTTPError as e:
            if e.code == 429 and attempt == 0:
                time.sleep(RETRY_BACKOFF_S)
                continue
            raise


async def get_json(url: str) -> dict | list:
    """Issue a GET and return decoded JSON.

    Retries once on HTTP 429. Deezer signals quota exhaustion and other
    application-level failures with **HTTP 200** and a body like
    ``{"error": {"code": 4, "message": "Quota limit exceeded"}}``, so we
    also retry once on that shape and surface the payload as an
    exception on the second miss. Callers all catch and return an
    empty result, so a warmup flood that trips the limit just yields
    empty caches rather than breaking later lookups.
    """
    for attempt in range(2):
        data = await asyncio.to_thread(_sync_get, url)
        if isinstance(data, dict):
            err = data.get("error")
            if isinstance(err, dict) and err.get("code") is not None:
                if attempt == 0:
                    await asyncio.sleep(RETRY_BACKOFF_S)
                    continue
                raise RuntimeError(f"Deezer API error: {err}")
        return data
    return data  # unreachable; loop always returns or raises.


async def search_artist(name: str) -> dict | None:
    """Find an artist by name. Prefers an exact case-insensitive match."""
    url = f"{BASE_URL}/search/artist?" + urllib.parse.urlencode({"q": name, "limit": 5})
    data = await get_json(url)
    results = data.get("data") if isinstance(data, dict) else []
    target = normalize_name(name)
    for r in results or []:
        if normalize_name(r.get("name", "")) == target:
            return r
    return (results or [None])[0]


async def get_artist(artist_id: int | str) -> dict | None:
    """Fetch a Deezer artist object by id (includes picture_xl, nb_fan, etc.)."""
    try:
        data = await get_json(f"{BASE_URL}/artist/{artist_id}")
    except Exception:
        return None
    return data if isinstance(data, dict) else None


async def get_artist_releases(artist_id: int | str) -> list[dict]:
    """Return the artist's full album/ep/single/compile release list."""
    url = f"{BASE_URL}/artist/{artist_id}/albums?limit=500"
    data = await get_json(url)
    return (data.get("data") if isinstance(data, dict) else None) or []


async def get_artist_top_tracks(artist_id: int | str, limit: int = 50) -> list[dict]:
    """Return the artist's top tracks (ordered by popularity on Deezer)."""
    url = f"{BASE_URL}/artist/{artist_id}/top?limit={limit}"
    data = await get_json(url)
    return (data.get("data") if isinstance(data, dict) else None) or []


async def get_album(album_id: int | str) -> dict | None:
    """Fetch an album (includes a nested ``tracks.data`` array)."""
    try:
        data = await get_json(f"{BASE_URL}/album/{album_id}")
    except Exception:
        return None
    return data if isinstance(data, dict) else None


async def get_album_first_track(album_id: int | str) -> dict | None:
    """Return the first track of an album, or None on failure.

    Used to derive a preview URL and canonical track id/duration for a
    single-kind release.
    """
    album = await get_album(album_id)
    if not album:
        return None
    tracks = ((album.get("tracks") or {}).get("data")) or []
    return tracks[0] if tracks else None


async def get_related_artists(artist_id: int | str, limit: int = 12) -> list[dict]:
    """Return artists Deezer lists as similar to ``artist_id``.

    Each item is a full Deezer artist object (includes ``picture_xl``,
    ``nb_fan``, etc.), so no extra lookups are required to render a
    minimal grid cell.
    """
    url = f"{BASE_URL}/artist/{artist_id}/related?limit={limit}"
    try:
        data = await get_json(url)
    except Exception:
        return []
    return (data.get("data") if isinstance(data, dict) else None) or []


async def get_chart_artists(genre_id: int | str = 0, limit: int = 12) -> list[dict]:
    """Return the top N artists for a genre (0 = global).

    Note: Deezer's ``/chart/{id}/artists`` endpoint ignores the genre
    and always returns the global leaderboard. Callers that need
    per-genre artists should use ``get_chart`` + derive from tracks.
    """
    url = f"{BASE_URL}/chart/{genre_id}/artists?limit={limit}"
    try:
        data = await get_json(url)
    except Exception:
        return []
    return (data.get("data") if isinstance(data, dict) else None) or []


async def get_chart(genre_id: int | str = 0) -> dict:
    """Return the full chart object (tracks, albums, artists, playlists)."""
    try:
        data = await get_json(f"{BASE_URL}/chart/{genre_id}")
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


async def get_genres() -> list[dict]:
    """Return Deezer's genre list (id + name + picture)."""
    try:
        data = await get_json(f"{BASE_URL}/genre")
    except Exception:
        return []
    return (data.get("data") if isinstance(data, dict) else None) or []


async def get_editorial_releases(editorial_id: int | str = 0, limit: int = 24) -> list[dict]:
    """Return Deezer's editorial new-release feed for a genre (0 = All)."""
    url = f"{BASE_URL}/editorial/{editorial_id}/releases?limit={limit}"
    try:
        data = await get_json(url)
    except Exception:
        return []
    return (data.get("data") if isinstance(data, dict) else None) or []


async def search_track(query: str, limit: int = 5) -> list[dict]:
    """Global track search. Each hit includes album + artist metadata."""
    if not query.strip():
        return []
    url = f"{BASE_URL}/search/track?" + urllib.parse.urlencode({"q": query, "limit": limit})
    try:
        data = await get_json(url)
    except Exception:
        return []
    return (data.get("data") if isinstance(data, dict) else None) or []


async def search_album(query: str, limit: int = 5) -> list[dict]:
    """Global album search. Each hit includes artist metadata."""
    if not query.strip():
        return []
    url = f"{BASE_URL}/search/album?" + urllib.parse.urlencode({"q": query, "limit": limit})
    try:
        data = await get_json(url)
    except Exception:
        return []
    return (data.get("data") if isinstance(data, dict) else None) or []
