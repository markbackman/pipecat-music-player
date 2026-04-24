"""UI agent: owns navigation stack and UI state.

Two entry points:

- ``on_task_request``: the voice agent delegates a natural-language
  request. The UI agent's LLM picks one tool based on the ``<ui_event>``
  context injected by ``UIAgent`` on every client click.
- ``@on_ui_event(...)``: the client sends a ``ui.event`` (grid click or
  Detail-screen button). Dispatched directly to the decorated handler
  without an LLM call; ``UIAgent`` auto-appends a ``<ui_event>``
  developer message so the LLM sees the user action on the next turn.

All UI changes fan out through ``self.send_command(name, payload)``,
which publishes a ``BusUICommandMessage``. The bridge installed by
``attach_ui_bridge`` turns that into an ``RTVIServerMessageFrame`` on
the root agent's pipeline so RTVI delivers it to the client. Every
catalog lookup (seed listing, artist fetch, title resolution,
description generation) goes through the long-lived ``CatalogAgent``
via the bus.
"""

import asyncio
import os
from dataclasses import dataclass, field
from typing import Literal

from loguru import logger
from pipecat.frames.frames import LLMMessagesAppendFrame
from pipecat.pipeline.pipeline import Pipeline
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.processors.aggregators.llm_response_universal import LLMContextAggregatorPair
from pipecat.services.llm_service import FunctionCallParams, LLMService
from pipecat.services.openai.base_llm import OpenAILLMSettings
from pipecat.services.openai.llm import OpenAILLMService
from pipecat_subagents.agents import (
    UI_STATE_PROMPT_GUIDE,
    ScrollTo,
    TaskStatus,
    Toast,
    on_ui_event,
    tool,
)
from pipecat_subagents.agents import UIAgent as BaseUIAgent
from pipecat_subagents.bus import AgentBus, BusTaskRequestMessage, BusUIEventMessage

import descriptions

Screen = Literal["home", "artist", "detail", "trending"]
Kind = Literal["album", "song"]


_APP_PROMPT = """\
You control a voice-driven music player backed by a live music catalog. \
You never speak to the user directly. You always call exactly one tool \
per turn.

## UI layers
- **Home**: three 8-column grids stacked top to bottom — Trending \
artists, New releases (recent albums with their artist names), and \
Favorites. Position references on Home resolve against the section \
the user names ("the first new release", "bottom left favorite").
- **Artist**: an artist page with three tabs — Albums, Songs, and \
Related artists. Only one tab's grid is visible at a time (8 columns \
wide). The ``<ui_state>`` context describes the currently active \
tab; position references like "top right" resolve against that \
tab's grid.
- **Detail**: an album or song page with Play, More Info, and Add to \
Favorites action buttons.
- **Trending**: a grid of currently-popular artists, optionally \
scoped to a genre.

## Tools
- ``navigate_to_artist(artist_name)``: Push the artist screen. Use \
when the user names an artist ("show me Nirvana", "show me Daft \
Punk") or refers to an artist on the home or trending grid by \
position. Any artist in the catalog is fair game, not just the \
seeded lineup.
- ``select_item(item_title)``: Push the detail screen for an album or \
song. Works from any screen. If the item lives under a different \
artist, the server navigates through that artist's page first so \
"go back" lands on it.
- ``play(item_title)``: Play the named album or song. Works from any \
screen; navigates to its detail page and starts playback.
- ``control_playback(action)``: Control the currently-playing preview. \
``action`` is ``"pause"``, ``"resume"``, or ``"stop"``. Use for "pause", \
"resume", "continue", "stop", "mute this".
- ``show_info(title)``: Show a description toast for a named item. \
``title`` may name an album, a song, or an artist; the server resolves \
all three. Works from any screen. Use when the user asks "tell me \
about X" and X is a specific album/song/artist they named.
- ``answer_about_catalog(question, about=None)``: Answer a factual \
question about the artist in focus using their catalog (latest album, \
first album, release year, track count, duration). Speaks a short \
answer. Pass ``about`` with the item title only if the answer pivots \
on one specific album or song the user should see a toast for.
- ``answer_about_music(question, about=None)``: Answer an opinion or \
trivia question about the current artist (most popular album, best \
entry point, who influenced them, are they still active). Uses the \
model's general music knowledge, grounded by the catalog. Speaks a \
short answer. Pass ``about`` only when the answer centers on a \
specific album or song.
- ``add_to_favorites(item_title)``: Mark an album or song as a \
favorite. Works from any screen.
- ``show_albums()``: Switch the current Artist page to its Albums \
tab. Only valid on an Artist screen. Use for "show albums", "see the \
albums", "go to albums".
- ``show_songs()``: Switch the current Artist page to its Songs tab. \
Only valid on an Artist screen. Use for "show songs", "see the \
tracks", "go to songs".
- ``show_similar_artists()``: Switch the current Artist page to its \
Related Artists tab (fetches on demand). Only valid on an Artist \
screen. Use for "who's similar", "show me artists like them", "more \
like this", "show related".
- ``show_trending(genre)``: Push a Trending screen. ``genre`` is an \
optional string like "rock", "pop", "hip-hop"; omit for the global \
chart. Use for "what's trending", "what's popular in rock", or \
anything chart-adjacent.
- ``go_back()``: Pop one screen off the navigation stack.
- ``go_home()``: Reset to the home grid.
- ``describe_screen(text)``: Describe the current screen in a single \
short sentence. Read-only.

## Decision rules
1. Every turn picks exactly one tool. Never reply with plain text.
2. If the user refers to an item by position ("top right", "the first \
one", "second album"), resolve the position from the most recent \
``<ui_state>`` grid layout in your context, then pass the resolved \
title to the tool.
3. If the user names a specific artist, album, or song, pass that \
name verbatim to the tool; the server resolves it case-insensitively \
against the live catalog.
4. When the user names a specific album or song title, call \
``select_item``, ``play``, ``show_info``, or ``add_to_favorites`` \
directly. Prefer ``navigate_to_artist`` only when the user names an \
artist without a specific title.
5. Use ``show_albums``, ``show_songs``, or ``show_similar_artists`` \
to switch the Artist page tab when the user asks for one of those \
categories in the abstract ("show me the albums", "who's similar"). \
Use ``show_trending`` for popularity / chart questions.
6. Use ``describe_screen`` only for questions about the screen as a \
whole ("where am I", "what is this page"). For questions about a \
specific named item, use ``show_info`` or ``select_item``. For \
conversational questions about the current artist (catalog facts, \
opinions, trivia), use ``answer_about_catalog`` or \
``answer_about_music``.

"""

SYSTEM_PROMPT = f"{_APP_PROMPT}\n\n{UI_STATE_PROMPT_GUIDE}"


@dataclass
class NavFrame:
    """One entry on the UI agent's navigation stack."""

    screen: Screen
    artist_id: str | None = None
    kind: Kind | None = None
    item_id: str | None = None
    # Only populated when screen == "trending".
    trending_genre: str | None = None


ArtistTab = Literal["albums", "songs", "related"]


@dataclass
class UIState:
    """Internal UI state mirroring what the client is rendering."""

    stack: list[NavFrame] = field(default_factory=lambda: [NavFrame(screen="home")])
    favorite_keys: set[str] = field(default_factory=set)
    favorites: list[dict] = field(default_factory=list)
    playing: dict | None = None
    playing_artist_id: str | None = None
    # Session-scoped artist cache sourced from CatalogAgent. Populated
    # whenever we receive an artist dict, drained only on agent restart.
    artist_cache: dict[str, dict] = field(default_factory=dict)
    # Ordered list of seed artist ids for the Home grid. Populated on first
    # ``list_home`` call.
    home_artist_ids: list[str] = field(default_factory=list)
    # Per-artist active tab on the Artist screen. Missing entries default
    # to "albums". Persists across nav-stack pushes so returning to an
    # artist keeps the tab the user picked.
    active_tab_by_artist: dict[str, ArtistTab] = field(default_factory=dict)


class UIAgent(BaseUIAgent):
    """Owns UI state and routes voice requests / client clicks to UI actions."""

    def __init__(self, name: str, *, bus: AgentBus):
        super().__init__(name, bus=bus, active=True)
        self._state = UIState()
        self._current_message: BusTaskRequestMessage | None = None

    def build_llm(self) -> LLMService:
        return OpenAILLMService(
            api_key=os.getenv("OPENAI_API_KEY"),
            settings=OpenAILLMSettings(
                system_instruction=SYSTEM_PROMPT,
                model=os.getenv("OPENAI_MODEL"),
            ),
        )

    async def build_pipeline(self) -> Pipeline:
        self._llm = self.create_llm()
        context = LLMContext()
        aggregator = LLMContextAggregatorPair(context)
        return Pipeline(
            [
                aggregator.user(),
                self._llm,
                aggregator.assistant(),
            ]
        )

    async def on_activated(self, args: dict | None) -> None:
        # The root agent creates this UIAgent inside RTVI's
        # ``on_client_ready`` handler, so by the time ``on_activated``
        # fires the client is already subscribed to server messages and
        # we can emit the initial screen without a client round-trip.
        await super().on_activated(args)
        await self._emit_for_top()

    async def on_task_request(self, message: BusTaskRequestMessage) -> None:
        # UIAgent's base handles <ui_state> auto-injection before we
        # append the query, so the LLM always reasons over the current
        # screen.
        await super().on_task_request(message)
        query = (message.payload or {}).get("query", "")
        logger.info(f"{self}: task query '{query}'")
        self._current_message = message
        await self.queue_frame(
            LLMMessagesAppendFrame(
                messages=[{"role": "developer", "content": query}],
                run_llm=True,
            )
        )

    # ------------------------------------------------------------------
    # Client UI events
    # ------------------------------------------------------------------

    @on_ui_event("nav")
    async def _on_nav(self, message: BusUIEventMessage) -> None:
        await self._handle_nav_click(message.payload or {})

    @on_ui_event("action")
    async def _on_action(self, message: BusUIEventMessage) -> None:
        await self._handle_action_click(message.payload or {})

    @on_ui_event("set_tab")
    async def _on_set_tab(self, message: BusUIEventMessage) -> None:
        await self._handle_set_tab_click(message.payload or {})

    @on_ui_event("play_track")
    async def _on_play_track(self, message: BusUIEventMessage) -> None:
        await self._handle_play_track_click(message.payload or {})

    # ------------------------------------------------------------------
    # Tools
    # ------------------------------------------------------------------

    @tool
    async def navigate_to_artist(self, params: FunctionCallParams, artist_name: str):
        """Push the artist screen for the named artist.

        Args:
            artist_name: The artist's display name (e.g. "Nirvana").
        """
        logger.info(f"{self}: navigate_to_artist('{artist_name}')")
        artist = await self._catalog_find_artist(artist_name)
        if not artist:
            await self._respond(f"I could not find {artist_name} in the library.")
            await params.result_callback(None)
            return
        description = await self._do_navigate_to_artist(artist)
        await self._respond(description)
        await params.result_callback(None)

    @tool
    async def select_item(self, params: FunctionCallParams, item_title: str):
        """Push the detail screen for an album or song.

        Args:
            item_title: The album or song title.
        """
        logger.info(f"{self}: select_item('{item_title}')")
        resolved = await self._catalog_resolve_item(item_title)
        if not resolved:
            await self._respond(f"I could not find {item_title} in the library.")
            await params.result_callback(None)
            return
        artist = resolved["artist"]
        kind: Kind = resolved["kind"]
        item = resolved["item"]
        description = await self._do_select_item(artist, kind, item)
        await self._respond(description)
        await params.result_callback(None)

    @tool
    async def play(self, params: FunctionCallParams, item_title: str):
        """Play an album or song, navigating to its detail first.

        Args:
            item_title: The album or song title to play.
        """
        logger.info(f"{self}: play('{item_title}')")
        resolved = await self._catalog_resolve_item(item_title)
        if not resolved:
            await self._respond(f"I could not find {item_title} in the library.")
            await params.result_callback(None)
            return
        artist = resolved["artist"]
        kind: Kind = resolved["kind"]
        item = resolved["item"]
        # If the user is looking at an album detail, treat "play X" as
        # "play this track from the album" so the album stays in focus
        # and the track row flips to Stop.
        top = self._top()
        if kind == "song" and top.screen == "detail" and top.kind == "album" and top.item_id:
            cached_artist = self._get_cached_artist(top.artist_id or "")
            album = (
                self._find_item_in_artist(cached_artist, "album", top.item_id)
                if cached_artist
                else None
            )
            track = self._find_track_in_album(album, item) if album else None
            if cached_artist and album and track:
                description = await self._do_play_track(cached_artist, album, track)
                await self._respond(description, speak=f"Playing {track['title']}.")
                await params.result_callback(None)
                return
        description = await self._do_play(artist, kind, item)
        await self._respond(description, speak=f"Playing {item['title']}.")
        await params.result_callback(None)

    @tool
    async def show_info(self, params: FunctionCallParams, title: str):
        """Show a description toast for any album, song, or artist.

        Args:
            title: An album title, a song title, or an artist name.
        """
        logger.info(f"{self}: show_info('{title}')")

        # Artists first (cached only, to keep latency low — Phase 2
        # expands this to the full catalog by prompt alone).
        artist = self._find_cached_artist(title)
        if artist:
            long_desc = await self._catalog_get_description("artist", artist["id"], "long")
            await self._emit_artist_toast(artist, long_desc)
            await self._respond(
                f"Info toast: {artist['name']}.",
                speak=long_desc or artist.get("short_description") or "",
            )
            await params.result_callback(None)
            return

        resolved = await self._catalog_resolve_item(title)
        if not resolved:
            await self._respond(f"I could not find {title} in the library.")
            await params.result_callback(None)
            return
        artist = resolved["artist"]
        kind: Kind = resolved["kind"]
        item = resolved["item"]
        long_desc = await self._catalog_get_description(kind, item["id"], "long")
        await self._emit_item_toast(artist, kind, item, long_desc)
        await self._respond(
            f"Info toast: {item['title']}.",
            speak=long_desc or item.get("short_description") or "",
        )
        await params.result_callback(None)

    @tool
    async def add_to_favorites(self, params: FunctionCallParams, item_title: str):
        """Add an album or song to favorites.

        Args:
            item_title: The album or song title.
        """
        logger.info(f"{self}: add_to_favorites('{item_title}')")
        resolved = await self._catalog_resolve_item(item_title)
        if not resolved:
            await self._respond(f"I could not find {item_title} in the library.")
            await params.result_callback(None)
            return
        artist = resolved["artist"]
        kind: Kind = resolved["kind"]
        item = resolved["item"]
        description = await self._do_add_favorite(artist, kind, item)
        await self._respond(description)
        await params.result_callback(None)

    @tool
    async def control_playback(self, params: FunctionCallParams, action: str):
        """Pause, resume, or stop the current preview playback.

        Args:
            action: One of ``"pause"``, ``"resume"``, ``"stop"``.
        """
        logger.info(f"{self}: control_playback('{action}')")
        normalized = action.strip().lower()
        if normalized not in ("pause", "resume", "stop"):
            await self._respond(f"Unknown playback action: {action}.")
            await params.result_callback(None)
            return
        if self._state.playing is None:
            await self._respond("Nothing is playing right now.", speak="Nothing is playing.")
            await params.result_callback(None)
            return
        title = self._state.playing["title"]
        await self.send_command("playback_control", {"action": normalized})
        if normalized == "stop":
            await self._do_stop_playback()
            await self._respond(f"Stopped {title}.", speak="Stopped.")
        elif normalized == "pause":
            await self._respond(f"Paused {title}.", speak="Paused.")
        else:
            await self._respond(f"Resumed {title}.", speak="Resuming.")
        await params.result_callback(None)

    @tool
    async def show_similar_artists(self, params: FunctionCallParams):
        """Switch to the Related Artists tab on the current Artist page."""
        logger.info(f"{self}: show_similar_artists")
        artist = await self._current_artist_for_tab_switch()
        if artist is None:
            await self._respond("I can only show similar artists while you're on an artist page.")
            await params.result_callback(None)
            return
        await self._activate_tab(artist, "related")
        related = artist.get("related_artists") or []
        if not related:
            description = f"No similar artists found for {artist['name']}."
            await self._respond(description, speak=description)
        else:
            names = ", ".join(r["name"] for r in related[:4])
            description = (
                f"Similar to {artist['name']}: " + ", ".join(r["name"] for r in related) + "."
            )
            speak = f"Here are artists like {artist['name']}: {names}."
            await self._respond(description, speak=speak)
        await params.result_callback(None)

    @tool
    async def show_albums(self, params: FunctionCallParams):
        """Switch to the Albums tab on the current Artist page."""
        logger.info(f"{self}: show_albums")
        artist = await self._current_artist_for_tab_switch()
        if artist is None:
            await self._respond("I can only switch tabs while you're on an artist page.")
            await params.result_callback(None)
            return
        await self._activate_tab(artist, "albums")
        await self._respond(
            f"Showing {artist['name']}'s albums.",
            speak=f"Here are {artist['name']}'s albums.",
        )
        await params.result_callback(None)

    @tool
    async def show_songs(self, params: FunctionCallParams):
        """Switch to the Songs tab on the current Artist page."""
        logger.info(f"{self}: show_songs")
        artist = await self._current_artist_for_tab_switch()
        if artist is None:
            await self._respond("I can only switch tabs while you're on an artist page.")
            await params.result_callback(None)
            return
        await self._activate_tab(artist, "songs")
        await self._respond(
            f"Showing {artist['name']}'s songs.",
            speak=f"Here are {artist['name']}'s songs.",
        )
        await params.result_callback(None)

    @tool
    async def answer_about_catalog(
        self,
        params: FunctionCallParams,
        question: str,
        about: str | None = None,
    ):
        """Answer a factual question about the current artist's catalog.

        Args:
            question: The user's question, passed verbatim.
            about: Optional album, song, or artist title the answer \
                pivots on. When provided, the server raises a toast \
                for that item alongside the spoken answer.
        """
        await self._answer_question("catalog", question, about, params)

    @tool
    async def answer_about_music(
        self,
        params: FunctionCallParams,
        question: str,
        about: str | None = None,
    ):
        """Answer an opinion or trivia question about the current artist.

        Args:
            question: The user's question, passed verbatim.
            about: Optional album, song, or artist title the answer \
                pivots on. When provided, the server raises a toast \
                for that item alongside the spoken answer.
        """
        await self._answer_question("music", question, about, params)

    @tool
    async def show_trending(self, params: FunctionCallParams, genre: str | None = None):
        """Push a Trending screen. Optional ``genre`` like "rock" or "pop"."""
        logger.info(f"{self}: show_trending(genre={genre!r})")
        result = await self._catalog_get_trending(genre)
        artists = result.get("artists") or []
        label = result.get("label") or "Trending"
        genre_label = result.get("genre")
        self._enter(NavFrame(screen="trending", trending_genre=genre_label))
        await self._emit_trending(label, artists, genre_label)
        if artists:
            top_names = ", ".join(a["name"] for a in artists[:3])
            speak = f"Trending: {top_names}."
            description = f"Trending screen ({label}). Top: {top_names}."
        else:
            speak = "I could not find a trending chart right now."
            description = speak
        await self._respond(description, speak=speak)
        await params.result_callback(None)

    @tool
    async def go_back(self, params: FunctionCallParams):
        """Pop one screen off the navigation stack."""
        logger.info(f"{self}: go_back")
        description = await self._do_go_back()
        await self._respond(description)
        await params.result_callback(None)

    @tool
    async def go_home(self, params: FunctionCallParams):
        """Reset the navigation stack to the home grid."""
        logger.info(f"{self}: go_home")
        description = await self._do_go_home()
        await self._respond(description)
        await params.result_callback(None)

    @tool
    async def describe_screen(self, params: FunctionCallParams, text: str):
        """Describe the current screen. Read-only.

        Args:
            text: A short, conversational description in plain spoken language.
        """
        logger.info(f"{self}: describe_screen('{text[:60]}...')")
        await self._respond(f"Described screen: {text}", speak=text)
        await params.result_callback(None)

    async def _answer_question(
        self,
        mode: str,
        question: str,
        about: str | None,
        params: FunctionCallParams,
    ) -> None:
        logger.info(f"{self}: answer_{mode}('{question}', about={about!r})")
        artist = self._current_context_artist()
        if artist is None:
            await self._respond(
                "I can only answer questions about an artist you're currently viewing.",
                speak="Pick an artist first and ask again.",
            )
            await params.result_callback(None)
            return

        answer = await descriptions.answer_question(
            mode=mode,
            question=question,
            artist_name=artist["name"],
            albums=artist.get("albums") or [],
            songs=artist.get("songs") or [],
        )
        if not answer:
            fallback = "I'm not sure about that one."
            await self._respond(fallback, speak=fallback)
            await params.result_callback(None)
            return

        toast_emitted = False
        if about:
            toast_emitted = await self._emit_answer_toast(artist, about, answer)

        description = (
            f"Answer ({mode}) about {artist['name']}: {answer}"
            if not toast_emitted
            else f"Answer toast ({mode}) on {artist['name']}: {answer}"
        )
        await self._respond(description, speak=answer)
        await params.result_callback(None)

    def _current_context_artist(self) -> dict | None:
        """Best-effort: return the artist whose page the user is on."""
        for frame in reversed(self._state.stack):
            if frame.artist_id:
                cached = self._get_cached_artist(frame.artist_id)
                if cached:
                    return cached
        return None

    async def _emit_answer_toast(self, artist: dict, about: str, answer: str) -> bool:
        """Resolve ``about`` and raise a toast for the matching item.

        Returns True if a toast was emitted. Falls back to no toast
        (speech only) when the title can't be resolved.
        """
        target = (about or "").strip().lower()
        if not target:
            return False
        if target == (artist.get("name") or "").strip().lower():
            await self.send_command(
                "toast",
                Toast(
                    title=artist["name"],
                    subtitle=artist.get("genre") or "Artist",
                    image_url=artist.get("image_url") or "",
                    description=answer,
                ),
            )
            return True
        resolved = await self._catalog_resolve_item(about)
        if not resolved:
            return False
        resolved_artist = resolved["artist"]
        kind: Kind = resolved["kind"]
        item = resolved["item"]
        label = "Album" if kind == "album" else "Song"
        year = item.get("year")
        subtitle = f"{resolved_artist['name']} · {label}"
        if kind == "album" and year:
            subtitle = f"{subtitle} · {year}"
        await self.send_command(
            "toast",
            Toast(
                title=item["title"],
                subtitle=subtitle,
                image_url=item.get("cover_url") or resolved_artist.get("image_url") or "",
                description=answer,
            ),
        )
        return True

    # ------------------------------------------------------------------
    # Click handlers (invoked from @on_ui_event dispatch)
    # ------------------------------------------------------------------
    #
    # The ``UIAgent`` base injects a ``<ui_event>`` developer message for
    # every client event before the handler runs, so these no longer
    # append ``[click] ...`` prose themselves.

    async def _handle_play_track_click(self, data: dict) -> None:
        artist = await self._catalog_get_artist(data.get("artist_id", ""))
        if not artist:
            return
        album = self._find_item_in_artist(artist, "album", data.get("album_id", ""))
        if not album:
            return
        track_id = data.get("track_id", "")
        # Toggle: re-clicking the active track stops playback.
        if (
            self._state.playing is not None
            and self._state.playing_artist_id == artist["id"]
            and self._state.playing.get("id") == track_id
        ):
            await self.send_command("playback_control", {"action": "stop"})
            await self._do_stop_playback()
            return
        tracks = album.get("tracks") or []
        if not tracks:
            tracks = await self._catalog_get_album_tracks(album["id"])
            album["tracks"] = tracks
        track = next((t for t in tracks if t["id"] == track_id), None)
        if not track:
            return
        await self._do_play_track(artist, album, track)

    async def _handle_set_tab_click(self, data: dict) -> None:
        tab = data.get("tab")
        if tab not in ("albums", "songs", "related"):
            return
        artist_id = data.get("artist_id") or ""
        artist = await self._catalog_get_artist(artist_id)
        if not artist:
            return
        await self._activate_tab(artist, tab)

    async def _handle_nav_click(self, data: dict) -> None:
        view = data.get("view")
        if view == "home":
            await self._do_go_home()
        elif view == "back":
            await self._do_go_back()
        elif view == "artist":
            artist = await self._catalog_get_artist(data.get("artist_id", ""))
            if not artist:
                return
            await self._do_navigate_to_artist(artist)
        elif view == "detail":
            artist = await self._catalog_get_artist(data.get("artist_id", ""))
            kind = data.get("detail_kind")
            item_id = data.get("item_id", "")
            if not artist or kind not in ("album", "song"):
                return
            item = self._find_item_in_artist(artist, kind, item_id)
            if not item:
                return
            await self._do_select_item(artist, kind, item)

    async def _handle_action_click(self, data: dict) -> None:
        action = data.get("action")
        artist = await self._catalog_get_artist(data.get("artist_id", ""))
        if not artist:
            return
        item_id = data.get("item_id", "")
        kind: Kind | None = None
        item: dict | None = None
        for k in ("album", "song"):
            found = self._find_item_in_artist(artist, k, item_id)
            if found:
                kind = k  # type: ignore[assignment]
                item = found
                break
        if not kind or item is None:
            return
        if action == "play":
            await self._do_play(artist, kind, item)
        elif action == "show_info":
            long_desc = await self._catalog_get_description(kind, item["id"], "long")
            await self._emit_item_toast(artist, kind, item, long_desc)
        elif action == "add_to_favorites":
            await self._do_add_favorite(artist, kind, item)

    # ------------------------------------------------------------------
    # Action helpers (shared by tools and click dispatcher)
    # ------------------------------------------------------------------

    async def _do_navigate_to_artist(self, artist: dict) -> str:
        self._enter(NavFrame(screen="artist", artist_id=artist["id"]))
        await self._emit_artist(artist)
        return f"Showing {artist['name']}."

    async def _do_select_item(self, artist: dict, kind: Kind, item: dict) -> str:
        top = self._top()
        if top.artist_id != artist["id"]:
            self._enter(NavFrame(screen="artist", artist_id=artist["id"]))
        self._enter(
            NavFrame(screen="detail", artist_id=artist["id"], kind=kind, item_id=item["id"])
        )
        await self._emit_detail(artist, kind, item)
        return f"{item.get('title', 'Item')} by {artist['name']}."

    async def _do_play(self, artist: dict, kind: Kind, item: dict) -> str:
        top = self._top()
        already_on_detail = (
            top.screen == "detail" and top.artist_id == artist["id"] and top.item_id == item["id"]
        )
        if not already_on_detail:
            if top.artist_id != artist["id"]:
                self._enter(NavFrame(screen="artist", artist_id=artist["id"]))
            self._enter(
                NavFrame(screen="detail", artist_id=artist["id"], kind=kind, item_id=item["id"])
            )
            await self._emit_detail(artist, kind, item)
        preview_url = item.get("preview_url") or ""
        if kind == "album" and not preview_url:
            preview_url = await self._catalog_get_album_preview(item["id"])
            if preview_url:
                item["preview_url"] = preview_url
        self._state.playing = item
        self._state.playing_artist_id = artist["id"]
        await self.send_command(
            "playback",
            {
                "state": "playing",
                "item_title": item["title"],
                "item_id": item["id"],
                "preview_url": preview_url,
            },
        )
        await self._emit_detail(artist, kind, item)
        return f"Now playing {item['title']} by {artist['name']}."

    async def _do_play_track(self, artist: dict, album: dict, track: dict) -> str:
        """Play a single track from an album's tracklist, staying on the album page."""
        synthetic = {
            "id": track["id"],
            "title": track["title"],
            "album_id": album["id"],
            "duration_seconds": track.get("duration_seconds") or 0,
            "cover_url": album.get("cover_url") or "",
            "preview_url": track.get("preview_url") or "",
        }
        self._state.playing = synthetic
        self._state.playing_artist_id = artist["id"]
        await self.send_command(
            "playback",
            {
                "state": "playing",
                "item_title": track["title"],
                "item_id": track["id"],
                "preview_url": synthetic["preview_url"],
            },
        )
        await self._emit_detail(artist, "album", album)
        return f"Now playing {track['title']} from {album['title']}."

    async def _do_stop_playback(self) -> None:
        self._state.playing = None
        self._state.playing_artist_id = None
        top = self._top()
        if top.screen == "detail" and top.artist_id and top.kind and top.item_id:
            artist = self._get_cached_artist(top.artist_id)
            if artist:
                item = self._find_item_in_artist(artist, top.kind, top.item_id)
                if item:
                    await self._emit_detail(artist, top.kind, item)

    async def _do_add_favorite(self, artist: dict, kind: Kind, item: dict) -> str:
        key = self._favorite_key(artist["id"], kind, item["id"])
        is_new = key not in self._state.favorite_keys
        if is_new:
            self._state.favorite_keys.add(key)
            self._state.favorites.append(self._favorite_record(artist, kind, item))
        await self.send_command(
            "favorite_added",
            {
                "favorite": self._favorite_record(artist, kind, item),
                "favorites": list(self._state.favorites),
            },
        )
        top = self._top()
        if top.screen == "detail" and top.artist_id == artist["id"] and top.item_id == item["id"]:
            await self._emit_detail(artist, kind, item)
        if not is_new:
            return f"{item['title']} is already in favorites."
        return f"Added {item['title']} to favorites."

    async def _do_go_back(self) -> str:
        if len(self._state.stack) > 1:
            self._state.stack.pop()
        top = self._top()
        await self._emit_for_top()
        if top.screen == "home":
            return "Back at the home grid."
        if top.screen == "artist":
            artist = self._get_cached_artist(top.artist_id or "")
            return f"Back on the {artist['name'] if artist else 'artist'} page."
        if top.screen == "trending":
            label = f"Trending · {top.trending_genre}" if top.trending_genre else "Trending"
            return f"Back on {label}."
        return "Back one screen."

    async def _do_go_home(self) -> str:
        self._state.stack = [NavFrame(screen="home")]
        await self._emit_home()
        return "Home grid is showing."

    # ------------------------------------------------------------------
    # Nav stack + caches
    # ------------------------------------------------------------------

    def _enter(self, frame: NavFrame) -> None:
        top = self._top()
        if top == frame:
            return
        self._state.stack.append(frame)

    def _top(self) -> NavFrame:
        return self._state.stack[-1]

    def _get_cached_artist(self, artist_id: str) -> dict | None:
        return self._state.artist_cache.get(artist_id)

    def _find_cached_artist(self, name: str) -> dict | None:
        target = name.strip().lower()
        for artist in self._state.artist_cache.values():
            if artist["name"].lower() == target or artist["id"] == target:
                return artist
        return None

    @staticmethod
    def _find_item_in_artist(artist: dict, kind: str, item_id: str) -> dict | None:
        coll = artist.get("albums", []) if kind == "album" else artist.get("songs", [])
        return next((i for i in coll if i["id"] == item_id), None)

    @staticmethod
    def _find_track_in_album(album: dict | None, song: dict) -> dict | None:
        """Match a resolved song dict against an album's loaded tracklist."""
        if not album:
            return None
        tracks = album.get("tracks") or []
        song_id = song.get("id")
        for t in tracks:
            if t["id"] == song_id:
                return t
        target = (song.get("title") or "").strip().lower()
        if not target:
            return None
        for t in tracks:
            if (t.get("title") or "").strip().lower() == target:
                return t
        return None

    def _cache_artist(self, artist: dict) -> None:
        self._state.artist_cache[artist["id"]] = artist

    @staticmethod
    def _favorite_key(artist_id: str, kind: Kind, item_id: str) -> str:
        return f"{artist_id}:{kind}:{item_id}"

    @staticmethod
    def _favorite_record(artist: dict, kind: Kind, item: dict) -> dict:
        return {
            "artist_id": artist["id"],
            "artist_name": artist["name"],
            "kind": kind,
            "item_id": item["id"],
            "item_title": item["title"],
            "cover_url": item.get("cover_url"),
        }

    # ------------------------------------------------------------------
    # CatalogAgent task calls
    # ------------------------------------------------------------------

    async def _catalog_list_home(self) -> list[dict]:
        async with self.task("catalog", payload={"action": "list_home"}, timeout=30) as t:
            pass
        response = t.response or {}
        # Home records are minimal (id + name + image_url). Don't cache
        # them in ``_state.artist_cache`` — that cache is for full artist
        # dicts with albums/songs. Clicking a home cell goes through
        # ``_catalog_get_artist`` which triggers a full fetch on miss.
        artists = response.get("artists") or []
        self._state.home_artist_ids = [a["id"] for a in artists]
        return artists

    async def _catalog_list_new_releases(self, limit: int = 12) -> list[dict]:
        async with self.task(
            "catalog",
            payload={"action": "list_new_releases", "limit": limit},
            timeout=15,
        ) as t:
            pass
        response = t.response or {}
        return response.get("releases") or []

    async def _catalog_find_artist(self, name: str) -> dict | None:
        async with self.task(
            "catalog", payload={"action": "find_artist", "name": name}, timeout=30
        ) as t:
            pass
        response = t.response or {}
        artist = response.get("artist")
        if artist:
            self._cache_artist(artist)
        return artist

    async def _catalog_get_artist(self, artist_id: str) -> dict | None:
        if not artist_id:
            return None
        cached = self._get_cached_artist(artist_id)
        if cached:
            return cached
        async with self.task(
            "catalog", payload={"action": "get_artist", "artist_id": artist_id}, timeout=15
        ) as t:
            pass
        response = t.response or {}
        artist = response.get("artist")
        if artist:
            self._cache_artist(artist)
            return artist
        # Not in the catalog's cache either — this happens for related /
        # trending artists the user just clicked. Fall back to a live
        # Deezer fetch keyed by id.
        async with self.task(
            "catalog",
            payload={"action": "fetch_artist_by_id", "artist_id": artist_id},
            timeout=30,
        ) as t:
            pass
        response = t.response or {}
        artist = response.get("artist")
        if artist:
            self._cache_artist(artist)
        return artist

    async def _catalog_related_artists(self, artist_id: str, limit: int = 6) -> list[dict]:
        async with self.task(
            "catalog",
            payload={"action": "related_artists", "artist_id": artist_id, "limit": limit},
            timeout=15,
        ) as t:
            pass
        response = t.response or {}
        return response.get("artists") or []

    async def _catalog_get_trending(self, genre: str | None) -> dict:
        async with self.task(
            "catalog",
            payload={"action": "get_trending", "genre": genre, "limit": 16},
            timeout=15,
        ) as t:
            pass
        return t.response or {}

    async def _catalog_resolve_item(self, title: str) -> dict | None:
        prefer = self._top().artist_id
        async with self.task(
            "catalog",
            payload={"action": "resolve_item", "title": title, "prefer_artist_id": prefer},
            timeout=15,
        ) as t:
            pass
        response = t.response or {}
        resolved = response.get("resolved")
        if resolved and resolved.get("artist"):
            self._cache_artist(resolved["artist"])
        return resolved

    async def _catalog_get_album_preview(self, album_id: str) -> str:
        async with self.task(
            "catalog",
            payload={"action": "get_album_preview", "album_id": album_id},
            timeout=15,
        ) as t:
            pass
        response = t.response or {}
        return response.get("preview_url", "") or ""

    async def _catalog_get_album_tracks(self, album_id: str) -> list[dict]:
        async with self.task(
            "catalog",
            payload={"action": "get_album_tracks", "album_id": album_id},
            timeout=20,
        ) as t:
            pass
        response = t.response or {}
        return response.get("tracks") or []

    async def _catalog_get_description(self, kind: str, id_: str, depth: str) -> str:
        async with self.task(
            "catalog",
            payload={"action": "get_description", "kind": kind, "id": id_, "depth": depth},
            timeout=30,
        ) as t:
            pass
        response = t.response or {}
        return response.get("description", "") or ""

    # ------------------------------------------------------------------
    # Frame emission
    # ------------------------------------------------------------------

    async def _emit_home(self) -> None:
        artists, new_releases = await asyncio.gather(
            self._catalog_list_home(),
            self._catalog_list_new_releases(limit=16),
        )
        await self.send_command(
            "screen",
            {
                "screen": "home",
                "artists": artists,
                "new_releases": new_releases,
                "favorites": list(self._state.favorites),
            },
        )

    async def _emit_artist(self, artist: dict) -> None:
        tab = self._get_artist_tab(artist["id"])
        await self.send_command(
            "screen",
            {
                "screen": "artist",
                "artist": artist,
                "active_tab": tab,
                "back_enabled": len(self._state.stack) > 1,
            },
        )

    def _get_artist_tab(self, artist_id: str) -> ArtistTab:
        return self._state.active_tab_by_artist.get(artist_id, "albums")

    def _set_artist_tab(self, artist_id: str, tab: ArtistTab) -> None:
        self._state.active_tab_by_artist[artist_id] = tab

    async def _current_artist_for_tab_switch(self) -> dict | None:
        """Return the Artist currently shown on top of the nav stack."""
        top = self._top()
        if top.screen != "artist" or not top.artist_id:
            return None
        artist = self._get_cached_artist(top.artist_id)
        if not artist:
            artist = await self._catalog_get_artist(top.artist_id)
        return artist

    async def _activate_tab(self, artist: dict, tab: ArtistTab) -> None:
        """Flip the active tab and (for ``related``) fetch on demand."""
        self._set_artist_tab(artist["id"], tab)
        if tab == "related" and not artist.get("related_artists"):
            related = await self._catalog_related_artists(artist["id"], limit=6)
            artist["related_artists"] = related
            self._cache_artist(artist)
        await self._emit_artist(artist)

    async def _emit_detail(self, artist: dict, kind: Kind, item: dict) -> None:
        if kind == "album" and not item.get("tracks"):
            tracks = await self._catalog_get_album_tracks(item["id"])
            if tracks:
                item["tracks"] = tracks
                if not item.get("preview_url"):
                    item["preview_url"] = tracks[0].get("preview_url", "")
        is_playing = (
            self._state.playing is not None
            and self._state.playing_artist_id == artist["id"]
            and self._state.playing.get("id") == item["id"]
        )
        await self.send_command(
            "screen",
            {
                "screen": "detail",
                "kind": kind,
                "item": item,
                "artist": artist,
                "is_favorite": self._favorite_key(artist["id"], kind, item["id"])
                in self._state.favorite_keys,
                "is_playing": is_playing,
                "playing_track_id": (
                    self._state.playing.get("id")
                    if self._state.playing and self._state.playing_artist_id == artist["id"]
                    else None
                ),
                "back_enabled": len(self._state.stack) > 1,
            },
        )

    async def _emit_trending(self, label: str, artists: list[dict], genre: str | None) -> None:
        await self.send_command(
            "screen",
            {
                "screen": "trending",
                "label": label,
                "genre": genre,
                "artists": artists,
                "back_enabled": len(self._state.stack) > 1,
            },
        )

    async def _emit_for_top(self) -> None:
        top = self._top()
        if top.screen == "home":
            await self._emit_home()
        elif top.screen == "artist":
            artist = await self._catalog_get_artist(top.artist_id or "")
            if artist:
                await self._emit_artist(artist)
        elif top.screen == "detail":
            artist = await self._catalog_get_artist(top.artist_id or "")
            if artist and top.kind and top.item_id:
                item = self._find_item_in_artist(artist, top.kind, top.item_id)
                if item:
                    await self._emit_detail(artist, top.kind, item)
        elif top.screen == "trending":
            # Re-fetch trending on reconnect; charts change fast enough
            # that the previous list is stale.
            result = await self._catalog_get_trending(top.trending_genre)
            await self._emit_trending(
                result.get("label") or "Trending",
                result.get("artists") or [],
                result.get("genre"),
            )

    async def _emit_artist_toast(self, artist: dict, long_description: str) -> None:
        text = (
            long_description
            or artist.get("long_description")
            or artist.get("short_description")
            or ""
        )
        genre = artist.get("genre") or "Artist"
        await self.send_command(
            "toast",
            Toast(
                title=artist["name"],
                subtitle=genre,
                image_url=artist.get("image_url") or "",
                description=text,
            ),
        )

    async def _emit_item_toast(
        self, artist: dict, kind: Kind, item: dict, long_description: str
    ) -> None:
        text = (
            long_description or item.get("long_description") or item.get("short_description") or ""
        )
        label = "Album" if kind == "album" else "Song"
        year = item.get("year")
        subtitle = f"{artist['name']} · {label}"
        if kind == "album" and year:
            subtitle = f"{subtitle} · {year}"
        await self.send_command(
            "toast",
            Toast(
                title=item["title"],
                subtitle=subtitle,
                image_url=item.get("cover_url") or artist.get("image_url") or "",
                description=text,
            ),
        )

    async def _send_scroll(self, target: str) -> None:
        """Ask the client to scroll a ``data-scroll-target`` section into view."""
        await self.send_command("scroll_to", ScrollTo(target_id=target))

    # ------------------------------------------------------------------
    # Response + LLM context
    # ------------------------------------------------------------------

    async def _respond(
        self,
        description: str,
        *,
        speak: str | None = None,
        status: TaskStatus = TaskStatus.COMPLETED,
    ) -> None:
        if self._current_message is None:
            return
        task_id = self._current_message.task_id
        self._current_message = None
        response: dict = {"description": description}
        if speak:
            response["speak"] = speak
        await self.send_task_response(task_id, response=response, status=status)

