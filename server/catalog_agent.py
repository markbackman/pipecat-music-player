"""Long-lived CatalogAgent.

Owns the Deezer-backed artist catalog and the LLM-generated description
cache for the running process. Home is seeded from Deezer's global
trending-artists chart on startup; full artist dicts are synthesized
lazily on click and warmed in the background. Exposes a
payload-dispatched task API consumed by ``UIAgent``.
"""

import asyncio
import time
from typing import Any

from loguru import logger
from pipecat_subagents.agents import BaseAgent, TaskStatus
from pipecat_subagents.bus import AgentBus, BusTaskRequestMessage

import deezer
import descriptions

HOME_LIMIT = 16
TARGET_SONGS = 16
EAGER_DESCRIBE_ALBUMS = 16
WARM_CONCURRENCY = 6
NEW_RELEASES_LIMIT = 16
NEW_RELEASES_TTL_S = 3600.0  # Re-fetch editorial feed at most once per hour.


class CatalogAgent(BaseAgent):
    """Process-lifetime catalog store backed by live Deezer calls."""

    def __init__(self, name: str, *, bus: AgentBus):
        super().__init__(name, bus=bus, active=True)
        self._artists_by_id: dict[str, dict] = {}
        self._artists_by_name_norm: dict[str, str] = {}
        # Home is a live Deezer top-artists chart. Each entry is a
        # minimal artist record (id + name + image_url); the full
        # album/song dicts are synthesized lazily and cached in
        # ``_artists_by_id``. Chart order is preserved so row-1-col-1
        # is the current #1.
        self._home_artists: list[dict] = []
        # "kind:id" -> {"short": str, "long": str}
        self._description_cache: dict[str, dict[str, str]] = {}
        # Per-key locks to dedupe concurrent description generation.
        self._description_locks: dict[str, asyncio.Lock] = {}
        self._home_ready = asyncio.Event()
        # Genres lazy-loaded on first show_trending call; normalized
        # name → Deezer genre id. Canonical display labels live in a
        # parallel dict so we don't lose punctuation like "Rap/Hip Hop".
        self._genres_by_name_norm: dict[str, int] = {}
        self._genre_label_by_id: dict[int, str] = {}
        self._genres_lock = asyncio.Lock()
        # Editorial "new releases" feed, refreshed at most every
        # ``NEW_RELEASES_TTL_S`` seconds. Cache is shared across clients.
        self._new_releases: list[dict] = []
        self._new_releases_fetched_at: float = 0.0
        self._new_releases_lock = asyncio.Lock()

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def on_ready(self) -> None:
        await super().on_ready()
        self.create_asyncio_task(self._warm_home(), "catalog_warm_home")

    async def _warm_home(self) -> None:
        """Fetch Deezer's global trending-artists chart for the Home grid."""
        logger.info(f"{self}: warming home from global trending chart")
        try:
            hits = await deezer.get_chart_artists(0, limit=HOME_LIMIT)
        except Exception as exc:
            logger.error(f"{self}: home chart fetch failed: {exc}")
            hits = []
        self._home_artists = [self._minimal_artist(h) for h in hits]
        logger.info(f"{self}: {len(self._home_artists)} home artists ready")
        self._home_ready.set()
        # Pre-synthesize the full artist dicts (albums + songs) for every
        # home cell in the background so a click on Home is responsive.
        # ``_fetch_artist_by_id`` also kicks off per-artist short
        # description warming once the dict is built.
        if self._home_artists:
            self.create_asyncio_task(self._warm_home_full_artists(), "catalog_warm_home_full")

    async def _warm_home_full_artists(self) -> None:
        sem = asyncio.Semaphore(WARM_CONCURRENCY)

        async def bounded(artist_id: str) -> None:
            async with sem:
                await self._fetch_artist_by_id(artist_id)

        await asyncio.gather(
            *(bounded(a["id"]) for a in self._home_artists), return_exceptions=True
        )
        logger.info(f"{self}: home artist full dicts warmed")

    async def _warm_artist_short_descriptions(self, artist_id: str) -> None:
        """Backfill short descriptions for a newly-discovered artist.

        Artist + top songs get eagerly warmed. Albums are capped at
        ``EAGER_DESCRIBE_ALBUMS`` so a discography of 50+ doesn't trigger
        a burst of LLM calls; the rest generate lazily on detail open.
        """
        artist = self._artists_by_id.get(artist_id)
        if not artist:
            return
        coros = [self._ensure_description("artist", artist_id, "short")]
        for album in artist["albums"][:EAGER_DESCRIBE_ALBUMS]:
            coros.append(self._ensure_description("album", album["id"], "short"))
        for song in artist["songs"]:
            coros.append(self._ensure_description("song", song["id"], "short"))
        sem = asyncio.Semaphore(WARM_CONCURRENCY)

        async def bounded(c):
            async with sem:
                await c

        await asyncio.gather(*(bounded(c) for c in coros), return_exceptions=True)

    # ------------------------------------------------------------------
    # Task dispatch
    # ------------------------------------------------------------------

    async def on_task_request(self, message: BusTaskRequestMessage) -> None:
        await super().on_task_request(message)
        payload = message.payload or {}
        action = payload.get("action", "")
        task_id = message.task_id
        try:
            if action == "list_home":
                await self._home_ready.wait()
                await self.send_task_response(
                    task_id, response={"artists": list(self._home_artists)}
                )
            elif action == "find_artist":
                name = payload.get("name", "")
                artist = await self._find_artist(name)
                await self.send_task_response(
                    task_id,
                    response={"artist": self._strip_internal(artist) if artist else None},
                )
            elif action == "get_artist":
                artist_id = str(payload.get("artist_id", ""))
                artist = self._artists_by_id.get(artist_id)
                await self.send_task_response(
                    task_id,
                    response={"artist": self._strip_internal(artist) if artist else None},
                )
            elif action == "resolve_item":
                title = payload.get("title", "")
                prefer = payload.get("prefer_artist_id")
                resolved = await self._resolve_item(title, prefer)
                await self.send_task_response(task_id, response={"resolved": resolved})
            elif action == "get_description":
                kind = payload.get("kind", "")
                id_ = str(payload.get("id", ""))
                depth = payload.get("depth", "long")
                desc = await self._ensure_description(kind, id_, depth)
                await self.send_task_response(task_id, response={"description": desc})
            elif action == "related_artists":
                artist_id = str(payload.get("artist_id", ""))
                limit = int(payload.get("limit", 6))
                related = await self._related_artists(artist_id, limit)
                await self.send_task_response(task_id, response={"artists": related})
            elif action == "get_trending":
                genre = payload.get("genre")
                limit = int(payload.get("limit", 12))
                result = await self._trending(genre, limit)
                await self.send_task_response(task_id, response=result)
            elif action == "fetch_artist_by_id":
                artist_id = str(payload.get("artist_id", ""))
                artist = await self._fetch_artist_by_id(artist_id)
                await self.send_task_response(
                    task_id,
                    response={"artist": self._strip_internal(artist) if artist else None},
                )
            elif action == "get_album_preview":
                album_id = str(payload.get("album_id", ""))
                preview = await self._get_album_preview(album_id)
                await self.send_task_response(task_id, response={"preview_url": preview})
            elif action == "get_album_tracks":
                album_id = str(payload.get("album_id", ""))
                tracks = await self._get_album_tracks(album_id)
                await self.send_task_response(task_id, response={"tracks": tracks})
            elif action == "list_new_releases":
                limit = int(payload.get("limit", NEW_RELEASES_LIMIT))
                releases = await self._get_new_releases(limit=limit)
                await self.send_task_response(task_id, response={"releases": releases})
            else:
                await self.send_task_response(
                    task_id,
                    response={"error": f"unknown action: {action!r}"},
                    status=TaskStatus.FAILED,
                )
        except Exception as exc:
            logger.exception(f"{self}: task {action!r} failed")
            await self.send_task_response(
                task_id,
                response={"error": str(exc)},
                status=TaskStatus.FAILED,
            )

    # ------------------------------------------------------------------
    # Artist fetch + cache
    # ------------------------------------------------------------------

    async def _fetch_artist_by_name(self, name: str) -> dict:
        deezer_artist = await deezer.search_artist(name)
        if not deezer_artist:
            raise ValueError(f"no Deezer match for {name!r}")
        return await self._build_artist(deezer_artist)

    async def _build_artist(self, deezer_artist: dict) -> dict:
        """Synthesize our artist shape (all albums + 12 songs) from Deezer."""
        artist_id = str(deezer_artist["id"])
        releases = await deezer.get_artist_releases(artist_id)

        albums: list[dict] = []
        seen_album_ids: set[str] = set()
        for r in releases:
            if r.get("record_type") not in ("album", "ep", "compile"):
                continue
            album_id = str(r.get("id") or "")
            if not album_id or album_id in seen_album_ids:
                continue
            title = r.get("title") or ""
            if not title:
                continue
            year = int((r.get("release_date") or "0000-")[:4] or 0)
            albums.append(
                {
                    "id": album_id,
                    "title": title,
                    "year": year,
                    "cover_url": r.get("cover_xl") or r.get("cover_big") or "",
                    "short_description": None,
                    "long_description": None,
                    "_record_type": r.get("record_type", "album"),
                }
            )
            seen_album_ids.add(album_id)

        # Songs: prefer record_type=single, then top up with top-tracks.
        songs: list[dict] = []
        seen_titles: set[str] = set()

        for r in releases:
            if len(songs) >= TARGET_SONGS:
                break
            if r.get("record_type") != "single":
                continue
            title = r.get("title") or ""
            norm = deezer.normalize_name(title)
            if not title or norm in seen_titles:
                continue
            track = await deezer.get_album_first_track(r["id"])
            duration = int((track or {}).get("duration") or 0) or 240
            preview = (track or {}).get("preview") or ""
            track_id = str((track or {}).get("id") or r["id"])
            songs.append(
                {
                    "id": track_id,
                    "title": title,
                    "album_id": "",
                    "duration_seconds": duration,
                    "cover_url": r.get("cover_xl") or r.get("cover_big") or "",
                    "preview_url": preview,
                    "short_description": None,
                    "long_description": None,
                }
            )
            seen_titles.add(norm)

        if len(songs) < TARGET_SONGS:
            tops = await deezer.get_artist_top_tracks(artist_id, limit=100)
            for t in tops:
                if len(songs) >= TARGET_SONGS:
                    break
                title = t.get("title") or ""
                norm = deezer.normalize_name(title)
                if not title or norm in seen_titles:
                    continue
                album = t.get("album") or {}
                songs.append(
                    {
                        "id": str(t["id"]),
                        "title": title,
                        "album_id": "",
                        "duration_seconds": int(t.get("duration") or 240),
                        "cover_url": album.get("cover_xl") or album.get("cover_big") or "",
                        "preview_url": t.get("preview") or "",
                        "short_description": None,
                        "long_description": None,
                    }
                )
                seen_titles.add(norm)

        return {
            "id": artist_id,
            "name": deezer_artist.get("name") or "",
            "genre": "",
            "image_url": deezer_artist.get("picture_xl") or deezer_artist.get("picture_big") or "",
            "short_description": None,
            "long_description": None,
            "albums": albums,
            "songs": songs,
            "_fans": deezer_artist.get("nb_fan"),
        }

    def _cache_artist(self, artist: dict) -> None:
        self._artists_by_id[artist["id"]] = artist
        self._artists_by_name_norm[deezer.normalize_name(artist["name"])] = artist["id"]

    async def _find_artist(self, name: str) -> dict | None:
        norm = deezer.normalize_name(name)
        cached_id = self._artists_by_name_norm.get(norm)
        if cached_id:
            return self._artists_by_id[cached_id]
        try:
            artist = await self._fetch_artist_by_name(name)
        except Exception as exc:
            logger.warning(f"{self}: find_artist({name!r}) failed: {exc}")
            return None
        self._cache_artist(artist)
        self.create_asyncio_task(
            self._warm_artist_short_descriptions(artist["id"]),
            f"catalog_warm_artist_{artist['id']}",
        )
        return artist

    async def _fetch_artist_by_id(self, artist_id: str) -> dict | None:
        """Look up an artist by Deezer id, caching the result.

        Used when the client clicks a Related Artists / Trending cell
        whose id hasn't been seen before.
        """
        if not artist_id:
            return None
        cached = self._artists_by_id.get(artist_id)
        if cached:
            return cached
        try:
            deezer_artist = await deezer.get_artist(artist_id)
            if not deezer_artist:
                return None
            artist = await self._build_artist(deezer_artist)
        except Exception as exc:
            logger.warning(f"{self}: fetch_artist_by_id({artist_id!r}) failed: {exc}")
            return None
        self._cache_artist(artist)
        self.create_asyncio_task(
            self._warm_artist_short_descriptions(artist["id"]),
            f"catalog_warm_artist_{artist['id']}",
        )
        return artist

    async def _get_album_tracks(self, album_id: str) -> list[dict]:
        """Return the tracklist for an album. Cached on the album dict.

        Each track has ``{id, title, duration_seconds, preview_url}``. The
        album's own ``preview_url`` is populated from the first track as a
        side effect, so subsequent ``get_album_preview`` calls are free.
        """
        if not album_id:
            return []
        album_ref = self._find_album_ref(album_id)
        if album_ref is not None and album_ref.get("tracks"):
            return list(album_ref["tracks"])
        data = await deezer.get_album(album_id)
        raw_tracks = (((data or {}).get("tracks") or {}).get("data")) or []
        tracks = [
            {
                "id": str(t.get("id")),
                "title": t.get("title") or "",
                "duration_seconds": int(t.get("duration") or 0),
                "preview_url": t.get("preview") or "",
            }
            for t in raw_tracks
            if t.get("id") and t.get("title")
        ]
        if album_ref is not None:
            album_ref["tracks"] = tracks
            if tracks and not album_ref.get("preview_url"):
                album_ref["preview_url"] = tracks[0]["preview_url"]
        return tracks

    def _find_album_ref(self, album_id: str) -> dict | None:
        for artist in self._artists_by_id.values():
            for album in artist["albums"]:
                if album["id"] == album_id:
                    return album
        return None

    async def _get_album_preview(self, album_id: str) -> str:
        """Return a Deezer 30-second preview URL for the album's first track.

        Albums aren't eagerly populated with previews when ``_build_artist``
        runs because most Play events target songs. When the user hits Play
        on an album, we lazily fetch the first track, cache it back onto the
        album dict, and return the URL. Empty string on any failure; the
        client falls back to a silent "Now Playing" banner.
        """
        if not album_id:
            return ""
        for artist in self._artists_by_id.values():
            for album in artist["albums"]:
                if album["id"] != album_id:
                    continue
                cached = album.get("preview_url")
                if cached:
                    return cached
                track = await deezer.get_album_first_track(album_id)
                preview = (track or {}).get("preview") or ""
                album["preview_url"] = preview
                return preview
        # Album not in cache (rare): fetch first track directly.
        track = await deezer.get_album_first_track(album_id)
        return (track or {}).get("preview") or ""

    # ------------------------------------------------------------------
    # Discovery (related artists, trending)
    # ------------------------------------------------------------------

    @staticmethod
    def _minimal_artist(deezer_artist: dict) -> dict:
        """Client-renderable grid cell for a discovery hit."""
        return {
            "id": str(deezer_artist.get("id", "")),
            "name": deezer_artist.get("name") or "",
            "image_url": (
                deezer_artist.get("picture_xl") or deezer_artist.get("picture_big") or ""
            ),
        }

    async def _related_artists(self, artist_id: str, limit: int = 6) -> list[dict]:
        if not artist_id:
            return []
        hits = await deezer.get_related_artists(artist_id, limit=limit)
        return [self._minimal_artist(h) for h in hits]

    async def _ensure_genres(self) -> None:
        if self._genres_by_name_norm:
            return
        async with self._genres_lock:
            if self._genres_by_name_norm:
                return
            genres = await deezer.get_genres()
            mapping: dict[str, int] = {}
            labels: dict[int, str] = {}
            for g in genres:
                name = g.get("name") or ""
                try:
                    gid = int(g.get("id", 0))
                except (TypeError, ValueError):
                    continue
                if name:
                    mapping[deezer.normalize_name(name)] = gid
                    labels[gid] = name
            self._genres_by_name_norm = mapping
            self._genre_label_by_id = labels

    def _resolve_genre_id(self, genre: str | None) -> tuple[int, str | None]:
        """Return ``(genre_id, canonical_label)``. Falls back to global (0)."""
        if not genre:
            return 0, None
        norm = deezer.normalize_name(genre)
        if not norm:
            return 0, None
        direct = self._genres_by_name_norm.get(norm)
        if direct is not None:
            # Pull the canonical label by reversing the lookup.
            label = self._label_for_genre_id(direct)
            return direct, label
        # Loose match: any genre whose normalized name contains ours or
        # is contained in ours. Handles "hip hop" → "Rap/Hip Hop".
        for n, gid in self._genres_by_name_norm.items():
            if norm in n or n in norm:
                return gid, self._label_for_genre_id(gid)
        return 0, None

    def _label_for_genre_id(self, gid: int) -> str | None:
        return self._genre_label_by_id.get(gid)

    async def _trending(self, genre: str | None, limit: int = 12) -> dict:
        await self._ensure_genres()
        genre_id, label = self._resolve_genre_id(genre)
        if genre_id == 0:
            hits = await deezer.get_chart_artists(0, limit=limit)
            artists = [self._minimal_artist(h) for h in hits]
        else:
            artists = await self._derive_genre_artists(genre_id, limit)
        screen_label = f"Trending · {label}" if label else "Trending"
        return {
            "label": screen_label,
            "genre": label,
            "artists": artists,
        }

    async def _derive_genre_artists(self, genre_id: int, limit: int) -> list[dict]:
        """Pull a genre's leading artists from its track + album charts.

        Deezer's ``/chart/{id}/artists`` endpoint silently falls back to
        the global chart for non-zero genres, so we walk the genre's own
        track chart (where filtering does work), dedupe by artist id,
        and preserve Deezer's ranking.
        """
        chart = await deezer.get_chart(genre_id)
        seen: dict[str, dict] = {}
        for item in (chart.get("tracks") or {}).get("data") or []:
            self._absorb_chart_artist(item.get("artist") or {}, seen)
            if len(seen) >= limit:
                break
        if len(seen) < limit:
            for item in (chart.get("albums") or {}).get("data") or []:
                self._absorb_chart_artist(item.get("artist") or {}, seen)
                if len(seen) >= limit:
                    break
        return list(seen.values())[:limit]

    @staticmethod
    def _absorb_chart_artist(artist: dict, seen: dict[str, dict]) -> None:
        aid = str(artist.get("id") or "")
        if not aid or aid in seen:
            return
        seen[aid] = {
            "id": aid,
            "name": artist.get("name") or "",
            "image_url": (
                artist.get("picture_xl")
                or artist.get("picture_big")
                or artist.get("picture_medium")
                or ""
            ),
        }

    # ------------------------------------------------------------------
    # Resolution
    # ------------------------------------------------------------------

    async def _resolve_item(self, title: str, prefer_artist_id: str | None = None) -> dict | None:
        """Return ``{"artist", "kind", "item"}`` or None.

        First walks the in-process artist cache (preferred artist
        first, then any other cached artist). If that misses, falls
        back to Deezer's global track and album search so the user can
        say "play X" without first navigating to the right artist.
        """
        norm = deezer.normalize_name(title)
        if not norm:
            return None
        if prefer_artist_id and prefer_artist_id in self._artists_by_id:
            artist = self._artists_by_id[prefer_artist_id]
            match = self._find_in_artist(artist, norm)
            if match:
                return {
                    "artist": self._strip_internal(artist),
                    "kind": match[0],
                    "item": match[1],
                }
        for aid, artist in self._artists_by_id.items():
            if aid == prefer_artist_id:
                continue
            match = self._find_in_artist(artist, norm)
            if match:
                return {
                    "artist": self._strip_internal(artist),
                    "kind": match[0],
                    "item": match[1],
                }
        return await self._search_resolve(title, norm)

    async def _search_resolve(self, title: str, norm: str) -> dict | None:
        """Deezer-backed global fallback. Tries track search, then albums.

        When a hit is found, we load the associated artist via
        ``_find_artist`` so later navigation lands on a complete page.
        """
        track_hits = await deezer.search_track(title, limit=5)
        for hit in track_hits:
            hit_norm = deezer.normalize_name(hit.get("title") or "")
            if not hit_norm or (hit_norm != norm and not hit_norm.startswith(norm + " ")):
                continue
            artist_name = (hit.get("artist") or {}).get("name") or ""
            artist = await self._find_artist(artist_name)
            if not artist:
                continue
            album = hit.get("album") or {}
            song = {
                "id": str(hit.get("id")),
                "title": hit.get("title") or "",
                "album_id": str(album.get("id") or ""),
                "duration_seconds": int(hit.get("duration") or 0),
                "cover_url": album.get("cover_xl") or album.get("cover_big") or "",
                "preview_url": hit.get("preview") or "",
                "short_description": None,
                "long_description": None,
            }
            return {
                "artist": self._strip_internal(artist),
                "kind": "song",
                "item": song,
            }

        album_hits = await deezer.search_album(title, limit=5)
        for hit in album_hits:
            hit_norm = deezer.normalize_name(hit.get("title") or "")
            if not hit_norm or (hit_norm != norm and not hit_norm.startswith(norm + " ")):
                continue
            artist_name = (hit.get("artist") or {}).get("name") or ""
            artist = await self._find_artist(artist_name)
            if not artist:
                continue
            cached = self._find_album_ref(str(hit.get("id")))
            if cached:
                return {
                    "artist": self._strip_internal(artist),
                    "kind": "album",
                    "item": cached,
                }
            synth_album = {
                "id": str(hit.get("id")),
                "title": hit.get("title") or "",
                "year": int((hit.get("release_date") or "0000-")[:4] or 0),
                "cover_url": hit.get("cover_xl") or hit.get("cover_big") or "",
                "short_description": None,
                "long_description": None,
            }
            return {
                "artist": self._strip_internal(artist),
                "kind": "album",
                "item": synth_album,
            }
        return None

    async def _get_new_releases(self, limit: int = NEW_RELEASES_LIMIT) -> list[dict]:
        """Return Deezer's editorial new-releases feed, cached for an hour."""
        now = time.monotonic()
        if self._new_releases and now - self._new_releases_fetched_at < NEW_RELEASES_TTL_S:
            return list(self._new_releases[:limit])
        async with self._new_releases_lock:
            now = time.monotonic()
            if self._new_releases and now - self._new_releases_fetched_at < NEW_RELEASES_TTL_S:
                return list(self._new_releases[:limit])
            raw = await deezer.get_editorial_releases(0, limit=max(limit, 24))
            releases: list[dict] = []
            for r in raw:
                artist = r.get("artist") or {}
                aid = str(artist.get("id") or "")
                if not aid:
                    continue
                release_date = r.get("release_date") or ""
                releases.append(
                    {
                        "id": str(r.get("id")),
                        "title": r.get("title") or "",
                        "year": int((release_date or "0000-")[:4] or 0),
                        "release_date": release_date,
                        "cover_url": r.get("cover_xl") or r.get("cover_big") or "",
                        "artist_id": aid,
                        "artist_name": artist.get("name") or "",
                    }
                )
            self._new_releases = releases
            self._new_releases_fetched_at = now
            return list(releases[:limit])

    @staticmethod
    def _find_in_artist(artist: dict, norm_title: str) -> tuple[str, dict] | None:
        """Resolve a normalized title to (kind, item) within an artist.

        Matching runs in two passes. The first pass demands an exact
        normalized match and prefers a song, then album, then album
        track. The second pass relaxes to prefix matches (the candidate
        title starts with the query) and then to substring matches.
        Within a loose pass the shortest title wins, so "London Calling"
        beats "London Calling (Expanded Edition)" by picking the
        terser "London Calling (Remastered)".
        """
        albums = artist.get("albums") or []
        songs = artist.get("songs") or []

        def as_song_from_track(album: dict, track: dict) -> dict:
            return {
                "id": track["id"],
                "title": track["title"],
                "album_id": album["id"],
                "duration_seconds": track.get("duration_seconds") or 0,
                "cover_url": album.get("cover_url") or "",
                "preview_url": track.get("preview_url") or "",
                "short_description": None,
                "long_description": None,
            }

        # Pass 1: exact normalized match. Songs before albums so
        # "play <title>" prefers the track when the name collides.
        for song in songs:
            if deezer.normalize_name(song["title"]) == norm_title:
                return "song", song
        for album in albums:
            if deezer.normalize_name(album["title"]) == norm_title:
                return "album", album
        for album in albums:
            for track in album.get("tracks") or []:
                if deezer.normalize_name(track["title"]) == norm_title:
                    return "song", as_song_from_track(album, track)

        # Pass 2: loose match. A "prefix" hit treats the query as the
        # full plain title and the rest as decoration (Remastered,
        # Expanded Edition, etc.). "substring" is a last resort.
        query_prefix = norm_title + " "

        def rank(items: list[dict], title_key: str) -> list[tuple[int, int, dict]]:
            out: list[tuple[int, int, dict]] = []
            for item in items:
                norm = deezer.normalize_name(item.get(title_key) or "")
                if not norm or norm == norm_title:
                    continue
                if norm.startswith(query_prefix):
                    out.append((0, len(norm), item))
                elif norm_title in norm:
                    out.append((1, len(norm), item))
            out.sort(key=lambda x: (x[0], x[1]))
            return out

        song_hits = rank(songs, "title")
        if song_hits:
            return "song", song_hits[0][2]
        album_hits = rank(albums, "title")
        if album_hits:
            return "album", album_hits[0][2]
        for album in albums:
            track_hits = rank(album.get("tracks") or [], "title")
            if track_hits:
                return "song", as_song_from_track(album, track_hits[0][2])
        return None

    # ------------------------------------------------------------------
    # Description cache
    # ------------------------------------------------------------------

    async def _ensure_description(self, kind: str, id_: str, depth: str) -> str:
        key = f"{kind}:{id_}"
        cache = self._description_cache.setdefault(key, {})
        cached = cache.get(depth)
        if cached is not None:
            return cached

        lock = self._description_locks.setdefault(f"{key}:{depth}", asyncio.Lock())
        async with lock:
            cached = cache.get(depth)
            if cached is not None:
                return cached
            info = self._grounding_info(kind, id_)
            if info is None:
                cache[depth] = ""
                return ""
            desc = await descriptions.generate_description(kind=kind, depth=depth, **info)
            cache[depth] = desc
            self._write_back_description(kind, id_, depth, desc)
            return desc

    def _grounding_info(self, kind: str, id_: str) -> dict[str, Any] | None:
        if kind == "artist":
            artist = self._artists_by_id.get(id_)
            if not artist:
                return None
            return {
                "name": artist["name"],
                "artist_name": artist["name"],
                "year": None,
                "genres": [artist["genre"]] if artist.get("genre") else [],
                "record_type": None,
                "fans": artist.get("_fans"),
            }
        if kind == "album":
            for artist in self._artists_by_id.values():
                for album in artist["albums"]:
                    if album["id"] == id_:
                        return {
                            "name": album["title"],
                            "artist_name": artist["name"],
                            "year": album.get("year"),
                            "genres": [artist["genre"]] if artist.get("genre") else [],
                            "record_type": album.get("_record_type") or "album",
                            "fans": artist.get("_fans"),
                        }
        if kind == "song":
            for artist in self._artists_by_id.values():
                for song in artist["songs"]:
                    if song["id"] == id_:
                        return {
                            "name": song["title"],
                            "artist_name": artist["name"],
                            "year": None,
                            "genres": [artist["genre"]] if artist.get("genre") else [],
                            "record_type": "single",
                            "fans": artist.get("_fans"),
                        }
        return None

    def _write_back_description(self, kind: str, id_: str, depth: str, desc: str) -> None:
        field = "short_description" if depth == "short" else "long_description"
        if kind == "artist":
            a = self._artists_by_id.get(id_)
            if a is not None:
                a[field] = desc
            return
        for artist in self._artists_by_id.values():
            coll = artist["albums"] if kind == "album" else artist["songs"]
            for item in coll:
                if item["id"] == id_:
                    item[field] = desc
                    return

    # ------------------------------------------------------------------
    # Output shaping
    # ------------------------------------------------------------------

    @staticmethod
    def _strip_internal(artist: dict) -> dict:
        """Return a copy with leading-underscore cache fields removed."""
        out = {k: v for k, v in artist.items() if not k.startswith("_")}
        out["albums"] = [
            {k: v for k, v in a.items() if not k.startswith("_")} for a in artist["albums"]
        ]
        out["songs"] = [
            {k: v for k, v in s.items() if not k.startswith("_")} for s in artist["songs"]
        ]
        return out
