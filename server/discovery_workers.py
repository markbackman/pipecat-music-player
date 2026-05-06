"""Discovery workers: parallel music recommenders that stream tracks.

The UI agent kicks off ``user_task_group("similar_artist", "genre",
"two_hop")`` when the user asks for music recommendations. All three
workers answer the same question, "what is similar to the seed?",
from different angles:

- ``similar_artist``: Deezer's direct related-artists for the seed.
- ``genre``: top artists in the seed's genre (or modal genre across
  the seed's relateds when the seed record has no genre populated).
- ``two_hop``: the broader neighborhood, by expanding each direct
  related artist's own related set and ranking grandchildren by how
  often they recur across the first hop.

Each worker fetches its candidates' top tracks via the long-lived
``CatalogAgent`` and streams them back as ``send_task_update`` calls
of kind ``"track"``. The UI agent's ``on_task_update`` interception
turns each one into an ``add_track`` UI command, so tracks appear in
the Discoveries screen as workers find them.

Workers don't talk to Deezer directly. Every catalog lookup goes
through ``CatalogAgent``, the same data layer the rest of the app
uses, so caching, rate-limiting, and the description generator stay
centralized.
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

# How many tracks per candidate artist to stream back. Three balances
# variety per artist against keeping the screen scannable.
TRACKS_PER_ARTIST = 3

# Per-catalog-call timeout. Catalog calls hit Deezer and warm caches
# on first touch; give them room. Tuned to absorb the in-flight
# semaphore queueing in ``deezer.get_json`` plus the once-per-call
# 3s 429 retry sleep without timing out on a legitimate cold cascade.
CATALOG_TIMEOUT = 15.0


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
        "cover_url": (track.get("cover_url") or artist.get("image_url") or ""),
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
            await self.send_task_update(task_id, {"text": f"finding candidates for {self.source}"})

            candidates = await self.find_candidate_artists(seed, seed_artist_id)

            if not candidates:
                await self.send_task_update(task_id, {"text": "no candidates found"})
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
            await self.send_task_response(task_id, response={"error": str(exc), "count": 0})

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
    """Pull tracks from top artists in the seed artist's genre.

    Resolves the genre from the seed's own record first, then falls
    back to the modal genre across the seed's related-artist set
    when the seed record doesn't carry one. If neither yields a
    genre, the worker emits no candidates and completes cleanly.
    """

    source = "genre"

    async def find_candidate_artists(
        self, seed: str, seed_artist_id: str | None
    ) -> list[tuple[str, str]]:
        if not seed_artist_id:
            return []

        seed_artist = await self._fetch_artist(seed_artist_id)
        genre = (seed_artist or {}).get("genre") or ""
        if not genre:
            genre = await self._derive_genre_from_related(seed_artist_id)
        if not genre:
            return []

        result = await self._catalog("get_trending", genre=genre, limit=CANDIDATES_PER_WORKER + 2)
        artists = result.get("artists") or []
        candidates: list[tuple[str, str]] = []
        for a in artists:
            aid = str(a.get("id") or "")
            if not aid or aid == seed_artist_id:
                continue
            candidates.append((aid, genre))
            if len(candidates) >= CANDIDATES_PER_WORKER:
                break
        return candidates

    async def _derive_genre_from_related(self, seed_artist_id: str) -> str:
        """Modal genre across the seed's related-artist set.

        Used when the seed's own record has no genre populated. Pulls
        a small related set, fetches each as a full artist record (so
        ``genre`` is filled), and returns the most common genre. Ties
        break by encounter order, which matches Deezer's relevance
        ranking of the related list.
        """
        related = await self._catalog("related_artists", artist_id=seed_artist_id, limit=5)
        related_artists = related.get("artists") or []
        if not related_artists:
            return ""
        results = await asyncio.gather(
            *[self._fetch_artist(str(a.get("id") or "")) for a in related_artists if a.get("id")],
            return_exceptions=True,
        )
        counts: dict[str, int] = {}
        for r in results:
            if isinstance(r, asyncio.CancelledError):
                raise r
            if isinstance(r, Exception) or not r:
                continue
            g = (r.get("genre") or "").strip()
            if not g:
                continue
            counts[g] = counts.get(g, 0) + 1
        if not counts:
            return ""
        return max(counts.items(), key=lambda x: x[1])[0]


class TwoHopRecommender(_DiscoveryWorker):
    """Pull tracks from the seed's broader neighborhood.

    Expands each of the seed's top direct-related artists to their
    own related-artist set, then ranks the union of those grand-
    children by how often they recur across the first hop. Surfaces
    artists that sit in the same musical space but don't show up in
    Deezer's direct related list for the seed.
    """

    source = "two_hop"

    # First-hop fan-out: how many direct relateds we expand. Each
    # expansion is a catalog round trip, so balance breadth against
    # latency.
    FIRST_HOP_LIMIT = 3

    # Second-hop limit per first-hop artist. Keep modest; the union
    # gets large fast.
    SECOND_HOP_LIMIT = 5

    async def find_candidate_artists(
        self, seed: str, seed_artist_id: str | None
    ) -> list[tuple[str, str]]:
        if not seed_artist_id:
            return []

        first_hop = await self._catalog(
            "related_artists",
            artist_id=seed_artist_id,
            limit=self.FIRST_HOP_LIMIT,
        )
        first_hop_artists = first_hop.get("artists") or []
        if not first_hop_artists:
            return []

        first_hop_ids = {str(a.get("id") or "") for a in first_hop_artists if a.get("id")}
        first_hop_ids.discard("")

        results = await asyncio.gather(
            *[
                self._catalog(
                    "related_artists",
                    artist_id=str(a.get("id") or ""),
                    limit=self.SECOND_HOP_LIMIT,
                )
                for a in first_hop_artists
                if a.get("id")
            ],
            return_exceptions=True,
        )

        # Score by recurrence: an artist showing up across multiple
        # first-hops sits closer to the center of the neighborhood.
        # Track which first-hop artist surfaced each grandchild so
        # the streaming hint can credit the path.
        scores: dict[str, int] = {}
        via: dict[str, str] = {}
        for first_artist, result in zip(first_hop_artists, results):
            if isinstance(result, asyncio.CancelledError):
                raise result
            if isinstance(result, Exception):
                continue
            for a in result.get("artists") or []:
                aid = str(a.get("id") or "")
                if not aid:
                    continue
                if aid == seed_artist_id or aid in first_hop_ids:
                    continue
                scores[aid] = scores.get(aid, 0) + 1
                via.setdefault(aid, first_artist.get("name") or "")

        if not scores:
            return []

        ranked = sorted(scores.items(), key=lambda x: x[1], reverse=True)
        return [(aid, f"via {via[aid]}") for aid, _ in ranked[:CANDIDATES_PER_WORKER]]
