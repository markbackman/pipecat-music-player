"""Discovery workers: parallel music recommenders that stream tracks.

The UI agent kicks off ``user_task_group("similar_artist", "genre",
"chart")`` when the user asks for music recommendations. Each worker
here pulls candidate artists from a different angle, then fetches
their top tracks via the long-lived ``CatalogAgent`` and streams
each track back as a ``send_task_update`` of kind ``"track"``. The UI
agent's ``on_task_update`` interception turns each one into an
``add_track`` UI command, so tracks appear in the Discoveries
screen as workers find them.

Workers don't talk to Deezer directly — every catalog lookup goes
through ``CatalogAgent``, the same data layer the rest of the app
uses. That keeps caching, rate-limiting, and the description
generator centralized.
"""

from __future__ import annotations

import asyncio

from loguru import logger
from pipecat.pipeline.pipeline import Pipeline
from pipecat.processors.filters.identity_filter import IdentityFilter
from pipecat_subagents.agents import BaseAgent, TaskError
from pipecat_subagents.bus import AgentBus, BusTaskRequestMessage


# How many candidate artists each worker pulls before fetching tracks.
CANDIDATES_PER_WORKER = 5

# How many tracks per candidate artist to stream back. Two keeps the
# panel readable while still surfacing variety per source.
TRACKS_PER_ARTIST = 2

# Per-catalog-call timeout. Catalog calls hit Deezer and warm caches
# on first touch; give them room.
CATALOG_TIMEOUT = 8.0


def _track_to_payload(artist: dict, track: dict) -> dict:
    """Shape a catalog track + its artist into the add_track payload.

    The client renders this directly into a track card. Fields kept
    minimal: title, artist, album, preview, cover.
    """
    return {
        "id": str(track.get("id") or ""),
        "title": track.get("title") or "",
        "artist_id": str(artist.get("id") or ""),
        "artist_name": artist.get("name") or "",
        "album_id": str(track.get("album_id") or ""),
        "album_title": track.get("album_title") or "",
        "preview_url": track.get("preview_url") or "",
        "cover_url": (
            track.get("cover_url")
            or artist.get("image_url")
            or ""
        ),
        "duration_seconds": int(track.get("duration_seconds") or 0),
    }


class _DiscoveryWorker(BaseAgent):
    """Base for the three discovery workers.

    Subclasses override ``find_candidate_artists(seed, seed_artist_id)``
    to return a list of ``(artist_id, hint)`` tuples. ``hint`` is an
    optional descriptor that gets emitted in progress text (e.g. the
    genre name). The base then fans out, fetches each artist's full
    record from the catalog, and streams tracks.
    """

    source: str = "discovery"

    async def build_pipeline(self) -> Pipeline:
        return Pipeline([IdentityFilter()])

    async def find_candidate_artists(
        self, seed: str, seed_artist_id: str | None
    ) -> list[tuple[str, str]]:
        """Return ``(artist_id, hint)`` candidates. Override per worker."""
        return []

    async def on_task_request(self, message: BusTaskRequestMessage) -> None:
        await super().on_task_request(message)
        task_id = message.task_id
        payload = message.payload or {}
        seed = str(payload.get("seed") or "")
        seed_artist_id = payload.get("seed_artist_id")
        if seed_artist_id is not None:
            seed_artist_id = str(seed_artist_id)

        try:
            await self.send_task_update(
                task_id, {"text": f"finding candidates for {self.source}"}
            )

            candidates = await self.find_candidate_artists(seed, seed_artist_id)

            if not candidates:
                await self.send_task_update(
                    task_id, {"text": "no candidates found"}
                )
                await self.send_task_response(task_id, response={"count": 0})
                return

            await self.send_task_update(
                task_id,
                {"text": f"fetching tracks for {len(candidates)} artists"},
            )

            count = 0
            for artist_id, hint in candidates:
                artist = await self._fetch_artist(artist_id)
                if not artist:
                    continue
                songs = (artist.get("songs") or [])[:TRACKS_PER_ARTIST]
                if not songs:
                    continue
                if hint:
                    await self.send_task_update(
                        task_id,
                        {"text": f"streaming from {artist.get('name', '?')} ({hint})"},
                    )
                else:
                    await self.send_task_update(
                        task_id,
                        {"text": f"streaming from {artist.get('name', '?')}"},
                    )
                for song in songs:
                    await self.send_task_update(
                        task_id,
                        {
                            "kind": "track",
                            "track": _track_to_payload(artist, song),
                        },
                    )
                    count += 1

            await self.send_task_response(task_id, response={"count": count})
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.exception(f"{self}: discovery failed")
            await self.send_task_response(
                task_id, response={"error": str(exc), "count": 0}
            )

    async def _catalog(self, action: str, **payload) -> dict:
        """Helper: dispatch a single catalog task and return its response."""
        try:
            async with self.task(
                "catalog",
                payload={"action": action, **payload},
                timeout=CATALOG_TIMEOUT,
            ) as t:
                pass
        except TaskError as e:
            logger.warning(f"{self}: catalog {action!r} failed: {e}")
            return {}
        return t.response or {}

    async def _fetch_artist(self, artist_id: str) -> dict | None:
        if not artist_id:
            return None
        # ``fetch_artist_by_id`` warms the cache + returns the full
        # artist record (with songs[]). ``get_artist`` is cache-only
        # and may return None for artists we haven't seen yet.
        result = await self._catalog("fetch_artist_by_id", artist_id=artist_id)
        return result.get("artist")


class SimilarArtistRecommender(_DiscoveryWorker):
    """Pull tracks from artists Deezer says are related to the seed."""

    source = "similar_artist"

    async def find_candidate_artists(
        self, seed: str, seed_artist_id: str | None
    ) -> list[tuple[str, str]]:
        if not seed_artist_id:
            return []
        result = await self._catalog(
            "related_artists",
            artist_id=seed_artist_id,
            limit=CANDIDATES_PER_WORKER,
        )
        related = result.get("artists") or []
        return [(str(a.get("id") or ""), "related") for a in related if a.get("id")]


class GenreRecommender(_DiscoveryWorker):
    """Pull tracks from top artists in the seed artist's genre."""

    source = "genre"

    async def find_candidate_artists(
        self, seed: str, seed_artist_id: str | None
    ) -> list[tuple[str, str]]:
        # The seed artist may carry a genre; otherwise fall back to
        # the seed string itself as a genre name.
        genre = ""
        if seed_artist_id:
            artist = await self._fetch_artist(seed_artist_id)
            genre = (artist or {}).get("genre") or ""
        if not genre and seed:
            # Heuristic: if the user said "show me workout music",
            # the seed itself might be the genre. Catalog accepts
            # arbitrary names and tries to resolve them.
            genre = seed

        if not genre:
            return []

        result = await self._catalog(
            "get_trending", genre=genre, limit=CANDIDATES_PER_WORKER
        )
        artists = result.get("artists") or []
        # Skip the seed artist itself if it shows up in the genre chart.
        candidates: list[tuple[str, str]] = []
        for a in artists:
            aid = str(a.get("id") or "")
            if not aid or aid == seed_artist_id:
                continue
            candidates.append((aid, genre))
            if len(candidates) >= CANDIDATES_PER_WORKER:
                break
        return candidates


class ChartRecommender(_DiscoveryWorker):
    """Pull tracks from the global Deezer chart."""

    source = "chart"

    async def find_candidate_artists(
        self, seed: str, seed_artist_id: str | None
    ) -> list[tuple[str, str]]:
        result = await self._catalog(
            "get_trending", genre=None, limit=CANDIDATES_PER_WORKER + 2
        )
        artists = result.get("artists") or []
        candidates: list[tuple[str, str]] = []
        for a in artists:
            aid = str(a.get("id") or "")
            if not aid or aid == seed_artist_id:
                continue
            candidates.append((aid, "chart"))
            if len(candidates) >= CANDIDATES_PER_WORKER:
                break
        return candidates
