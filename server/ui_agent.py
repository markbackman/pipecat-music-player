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
from pipecat.processors.aggregators.llm_context_summarizer import SummaryAppliedEvent
from pipecat.processors.aggregators.llm_response_universal import (
    LLMAssistantAggregatorParams,
)
from pipecat.services.llm_service import FunctionCallParams, LLMService
from pipecat.services.openai.base_llm import OpenAILLMSettings
from pipecat.services.openai.llm import OpenAILLMService
from pipecat.utils.context.llm_context_summarization import (
    LLMAutoContextSummarizationConfig,
    LLMContextSummaryConfig,
)
from pipecat.processors.frameworks.rtvi.models import ScrollTo, Toast
from pipecat_subagents.agents import (
    UI_STATE_PROMPT_GUIDE,
    TaskStatus,
    on_ui_event,
    tool,
)
from pipecat_subagents.agents import UIAgent as BaseUIAgent
from pipecat_subagents.bus import (
    AgentBus,
    BusTaskRequestMessage,
    BusTaskUpdateMessage,
    BusUIEventMessage,
)

Screen = Literal["home", "artist", "detail", "trending", "discovery"]
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
- ``answer(text, about=None)``: Answer a question about the current \
artist or screen. Write the spoken reply in ``text`` directly — \
one or two short sentences, no markdown or lists. Ground factual \
claims in what you see in ``<ui_state>`` (album titles, release \
years on tiles, track titles, durations, track counts). Use your \
general music knowledge for trivia: awards, Grammys, chart \
performance, influences, biography, critical reception, \
collaborations, cultural context. Pass ``about`` only when the \
answer centers on a specific album or song the user should see a \
toast for.
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
screen, and only when the user is asking about the artist already \
on screen with no other artist named ("who's similar", "more like \
them", "show related"). If the user names a specific seed artist, \
use ``start_discovery`` instead.
- ``show_trending(genre)``: Push a Trending screen. ``genre`` is an \
optional string like "rock", "pop", "hip-hop"; omit for the global \
chart. Use for "what's trending", "what's popular in rock", or \
anything chart-adjacent.
- ``go_back()``: Pop one screen off the navigation stack.
- ``go_home()``: Reset to the home grid.
- ``describe_screen(text)``: Describe the current screen in a single \
short sentence. Read-only.
- ``start_discovery(seed_artist)``: Open a Discoveries screen and \
fan out to three parallel recommenders, all scoped to similarity \
with the seed. Tracks stream into the screen as workers find them; \
the user clicks a card to play. Use whenever the user names a seed \
artist and wants similar music, recommendations, or new artists to \
explore. Triggers include "find me music like X", "discover artists \
like X", "show me artists similar to X", "play me something like \
X", "recommend music like X", "more like X". ``seed_artist`` must \
be the artist the user named.
- ``scroll_to(ref)``: Scroll an element into view by its \
``<ui_state>`` ref. Use when the user wants to act on an element \
tagged ``[offscreen]`` (e.g. "play the last song" when track 18 is \
below the fold). Pair with the follow-up action on the next turn \
once the snapshot refreshes.
- ``highlight(ref)``: Briefly flash an element by its ref. Use when \
the user asks you to point at, identify, or call attention to a \
visible element ("which one is Radiohead?", "show me OK Computer"). \
Purely visual; no navigation side effect.

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
6. Disambiguate "similar / like / discover" requests by whether the \
user named a seed artist. Named seed ("similar to Radiohead", \
"discover artists like Nirvana", "music like X") → \
``start_discovery``. No named seed, asking about the current artist \
("who's similar", "more like them") → ``show_similar_artists`` \
(only valid on an Artist screen). Never call ``navigate_to_artist`` \
as a stepping stone toward similar artists; ``start_discovery`` \
resolves the seed itself.
6. Use ``describe_screen`` only for meta questions about the \
current screen ("where am I", "what is this page"). Use \
``show_info`` for "tell me about X" on a specific named item. Use \
``answer`` for conversational questions about the current artist \
(catalog facts, opinions, trivia, awards) — write the spoken reply \
in ``text`` directly. Ground visible facts in ``<ui_state>``; use \
training knowledge for trivia.

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
    # Only populated when screen == "discovery". The seed artist
    # the discovery task group fanned out from; the client renders
    # it in the screen header.
    discovery_seed_id: str | None = None
    discovery_seed_name: str | None = None


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
        # Music browsing is naturally multi-turn ("show me Nirvana"
        # → "play their best album" → "skip that one"). With
        # ``keep_history=True`` the LLM accumulates conversation
        # history across turns so deictic references like "that" /
        # "it" / "the first one" resolve against prior exchanges.
        #
        # ``enable_auto_context_summarization=True`` on the assistant
        # aggregator keeps the context bounded over long sessions:
        # when the configured thresholds (default 8000 tokens / 20
        # unsummarized messages) are reached, the LLM service
        # generates a summary that replaces older messages with a
        # system summary while preserving the most recent turns
        # verbatim. The summary preserves what the agent and user
        # discussed without keeping every stale ``<ui_state>``
        # snapshot in context.
        auto_context_summarization_config = LLMAutoContextSummarizationConfig(
            max_context_tokens=8000,
            max_unsummarized_messages=20,
            summary_config=LLMContextSummaryConfig(
                target_context_tokens=6000,
                min_messages_after_summary=4,
            ),
        )

        super().__init__(
            name,
            bus=bus,
            active=True,
            keep_history=True,
            assistant_params=LLMAssistantAggregatorParams(
                enable_auto_context_summarization=True,
                auto_context_summarization_config=auto_context_summarization_config,
            ),
        )
        self._state = UIState()
        self._seed_demo_favorites()

    def build_llm(self) -> LLMService:
        return OpenAILLMService(
            api_key=os.getenv("OPENAI_API_KEY"),
            settings=OpenAILLMSettings(
                system_instruction=SYSTEM_PROMPT,
                model=os.getenv("OPENAI_MODEL"),
            ),
        )

    async def on_ready(self) -> None:
        await super().on_ready()
        # Log when summarization fires so we have visibility into
        # how often it's compressing the running session.

        @self.assistant_aggregator.event_handler("on_summary_applied")
        async def _on_summary_applied(_aggregator, _summarizer, event: SummaryAppliedEvent):
            logger.info(
                f"{self}: context summarized "
                f"({event.original_message_count} → {event.new_message_count} messages, "
                f"{event.summarized_message_count} compressed, "
                f"{event.preserved_message_count} preserved)"
            )

    async def on_activated(self, args: dict | None) -> None:
        # The root agent creates this UIAgent inside RTVI's
        # ``on_client_ready`` handler, so by the time ``on_activated``
        # fires the client is already subscribed to server messages and
        # we can emit the initial screen without a client round-trip.
        await super().on_activated(args)
        await self._emit_for_top()

    async def on_task_request(self, message: BusTaskRequestMessage) -> None:
        # UIAgent's base records the in-flight task on ``current_task``
        # and auto-injects ``<ui_state>`` before we append the query,
        # so the LLM always reasons over the current screen.
        await super().on_task_request(message)
        query = (message.payload or {}).get("query", "")
        logger.info(f"{self}: task query '{query}'")
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

    @on_ui_event("track_click")
    async def _on_track_click(self, message: BusUIEventMessage) -> None:
        await self._handle_discovery_track_click(message.payload or {})

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
            msg = f"I could not find {artist_name} in the library."
            await self._respond(msg, speak=msg)
            await params.result_callback(None)
            return
        description = await self._do_navigate_to_artist(artist)
        await self._respond(description, speak=description)
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
            msg = f"I could not find {item_title} in the library."
            await self._respond(msg, speak=msg)
            await params.result_callback(None)
            return
        artist = resolved["artist"]
        kind: Kind = resolved["kind"]
        item = resolved["item"]
        description = await self._do_select_item(artist, kind, item)
        await self._respond(description, speak=description)
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
            msg = f"I could not find {item_title} in the library."
            await self._respond(msg, speak=msg)
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
            msg = f"I could not find {title} in the library."
            await self._respond(msg, speak=msg)
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
            msg = f"I could not find {item_title} in the library."
            await self._respond(msg, speak=msg)
            await params.result_callback(None)
            return
        artist = resolved["artist"]
        kind: Kind = resolved["kind"]
        item = resolved["item"]
        description = await self._do_add_favorite(artist, kind, item)
        await self._respond(description, speak="Added.")
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
            msg = f"Unknown playback action: {action}."
            await self._respond(msg, speak=msg)
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
            msg = "I can only show similar artists while you're on an artist page."
            await self._respond(msg, speak=msg)
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
            msg = "I can only switch tabs while you're on an artist page."
            await self._respond(msg, speak=msg)
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
            msg = "I can only switch tabs while you're on an artist page."
            await self._respond(msg, speak=msg)
            await params.result_callback(None)
            return
        await self._activate_tab(artist, "songs")
        await self._respond(
            f"Showing {artist['name']}'s songs.",
            speak=f"Here are {artist['name']}'s songs.",
        )
        await params.result_callback(None)

    @tool
    async def answer(
        self,
        params: FunctionCallParams,
        text: str,
        about: str | None = None,
    ):
        """Answer a question about the current artist or screen.

        Ground factual claims in what you see in ``<ui_state>``
        (album titles, release years on tiles, track titles,
        durations, track counts). For trivia — awards, Grammys,
        chart performance, influences, biography, critical
        reception, collaborations, cultural context — use your
        general music knowledge. Keep the reply to one or two
        short spoken sentences.

        Args:
            text: The spoken answer in plain language (TTS-ready).
            about: Optional album, song, or artist title the answer
                pivots on. When the title resolves to a catalog
                item, the server raises a toast for it alongside
                the spoken answer.
        """
        logger.info(f"{self}: answer('{text[:60]}...', about={about!r})")
        artist = self._current_context_artist()
        toast_emitted = False
        if artist is not None and about:
            toast_emitted = await self._emit_answer_toast(artist, about, text)
        description = (
            f"Answer on {artist['name']}: {text}" if artist and toast_emitted else f"Answer: {text}"
        )
        await self._respond(description, speak=text)
        await params.result_callback(None)

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
        await self._respond(description, speak=description)
        await params.result_callback(None)

    @tool
    async def go_home(self, params: FunctionCallParams):
        """Reset the navigation stack to the home grid."""
        logger.info(f"{self}: go_home")
        description = await self._do_go_home()
        await self._respond(description, speak="Home.")
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

    @tool
    async def start_discovery(self, params: FunctionCallParams, seed_artist: str):
        """Find new music similar to the named seed artist.

        Opens a Discovery screen and fans out to three parallel
        recommenders, all scoped to similarity with the seed: direct
        similar artists, peers in the seed's genre, and the broader
        neighborhood (artists similar to the seed's similar). Tracks
        stream into the screen as workers find them; the user can
        click any card to play, or cancel the search.

        Use when the user asks for music recommendations or "play me
        something like X". The seed must be an artist the catalog
        can resolve.

        Args:
            seed_artist: Name of the artist to seed discovery on.
                The server resolves it via Deezer.
        """
        logger.info(f"{self}: start_discovery('{seed_artist}')")

        # Cold-catalog seed lookups can take several seconds when
        # home warm-up is competing for Deezer slots. Push a
        # placeholder Discovery screen and speak the ack first so
        # the user gets visual + audio feedback right away; resolve
        # the seed and fire the workers afterward.
        placeholder = {"id": "", "name": seed_artist, "image_url": ""}
        await self._do_navigate_to_discovery(placeholder)
        msg = f"Looking for music like {seed_artist}."
        await self._respond(msg, speak=msg)
        await params.result_callback(None)

        artist = await self._catalog_find_artist(seed_artist)
        if not artist:
            await self.send_command(
                "toast",
                Toast(
                    title="Couldn't find that artist",
                    subtitle="Discovery",
                    image_url="",
                    description=f"No catalog match for {seed_artist}.",
                ),
            )
            # Pop the placeholder screen so the user isn't stuck on
            # an empty Discovery view.
            await self._do_go_back()
            return

        # Update the nav frame to the canonical record and re-emit
        # so the header picks up the real image + spelling.
        top = self._top()
        if top.screen == "discovery":
            top.discovery_seed_id = str(artist["id"])
            top.discovery_seed_name = artist["name"]
        await self._emit_discovery(artist)

        # Fire-and-forget the task group. SDK forwards group_started,
        # task_update, task_completed, group_completed envelopes to
        # the client; on_task_update (below) intercepts streamed
        # tracks and emits add_track UI commands.
        await self.start_user_task_group(
            "similar_artist",
            "genre",
            "two_hop",
            payload={
                "seed": artist["name"],
                "seed_artist_id": artist["id"],
            },
            label=f"Discoveries: {artist['name']}",
            cancellable=True,
        )

    async def on_task_update(self, message: BusTaskUpdateMessage) -> None:
        """Translate per-track stream updates into ``add_track`` UI commands.

        Discovery workers stream each found track as a
        ``send_task_update`` with ``data["kind"] == "track"``. The
        UIAgent base class auto-forwards these as ``task_update``
        envelopes to the client (for the in-flight panel), and we
        ALSO emit a custom ``add_track`` command carrying the track
        payload so the client can render it as a card on the
        Discovery screen.

        Other update kinds (free-form progress text) flow through
        unchanged.
        """
        await super().on_task_update(message)
        update = message.update or {}
        if update.get("kind") != "track":
            return
        track = update.get("track") or {}
        if not track:
            return
        await self.send_command(
            "add_track",
            {"track": track, "source": message.source},
        )

    # ``scroll_to`` and ``highlight`` are silent fire-and-forget: the
    # tool dispatches the UI command, completes the in-flight task
    # with an empty response, and exits. The visual change on the
    # client is the user-facing feedback, and the voice agent's task
    # unblocks immediately so it can move on without speaking.
    #
    # The SDK ships a bundled ``ReplyToolMixin`` whose ``reply(answer,
    # scroll_to, highlight)`` tool requires a spoken answer. That
    # shape doesn't match this app, where each tool call IS the whole
    # turn (some speak via ``_respond``, some are silent like the two
    # below). We override the helper methods on ``UIAgent`` to expose
    # them as @tool-decorated, silent-terminating LLM tools.

    @tool
    async def scroll_to(self, params: FunctionCallParams, ref: str):
        """Scroll an element into view by its snapshot ref.

        Args:
            ref: Ref string from the most recent ``<ui_state>``,
                e.g. ``"e42"``.
        """
        logger.info(f"{self}: scroll_to(ref={ref!r})")
        await super().scroll_to(ref)
        await self.respond_to_task()
        await params.result_callback(None)

    @tool
    async def highlight(self, params: FunctionCallParams, ref: str):
        """Briefly flash an element on screen by its snapshot ref.

        Args:
            ref: Ref string from the most recent ``<ui_state>``,
                e.g. ``"e42"``.
        """
        logger.info(f"{self}: highlight(ref={ref!r})")
        await super().highlight(ref)
        await self.respond_to_task()
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

    async def _handle_discovery_track_click(self, data: dict) -> None:
        """User clicked a card in the Discoveries panel.

        Discovery tracks carry (artist_id, song id) — we resolve the
        artist's catalog record, find the matching song, and play it.
        Re-clicking the active track stops playback (parity with the
        regular ``play_track`` handler).
        """
        artist_id = str(data.get("artist_id") or "")
        track_id = str(data.get("track_id") or "")
        if not artist_id or not track_id:
            return
        artist = await self._catalog_get_artist(artist_id)
        if not artist:
            return
        song = self._find_item_in_artist(artist, "song", track_id)
        if not song:
            return
        # Toggle: re-clicking the active track stops playback.
        if (
            self._state.playing is not None
            and self._state.playing_artist_id == artist["id"]
            and self._state.playing.get("id") == track_id
        ):
            await self.send_command("playback_control", {"action": "stop"})
            await self._do_stop_playback()
            return
        await self._do_play(artist, "song", song)

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

    async def _do_navigate_to_discovery(self, seed_artist: dict) -> str:
        """Push a Discovery screen seeded on the given artist.

        The screen starts empty; the in-flight task group spawned by
        ``start_discovery`` will stream tracks in via ``add_track``
        commands as workers find them.
        """
        self._enter(
            NavFrame(
                screen="discovery",
                discovery_seed_id=str(seed_artist["id"]),
                discovery_seed_name=seed_artist["name"],
            )
        )
        await self._emit_discovery(seed_artist)
        return f"Looking for music like {seed_artist['name']}."

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

    def _seed_demo_favorites(self) -> None:
        """Seed the Favorites grid with a couple of well-known items.

        Demo only. Lets "scroll to my favorites" and "point at X" land
        on something concrete instead of the empty-state placeholder.
        Navigation into seeded items may not resolve if Deezer's IDs
        drift; that's acceptable for a review demo.
        """
        seeds: list[dict] = [
            {
                "artist_id": "399",
                "artist_name": "Radiohead",
                "kind": "album",
                "item_id": "7521880",
                "item_title": "In Rainbows",
                "cover_url": None,
            },
            {
                "artist_id": "1194053",
                "artist_name": "Bad Bunny",
                "kind": "album",
                "item_id": "656407741",
                "item_title": "DeBÍ TiRAR MáS FOToS",
                "cover_url": None,
            },
        ]
        for fav in seeds:
            key = self._favorite_key(fav["artist_id"], fav["kind"], fav["item_id"])
            if key in self._state.favorite_keys:
                continue
            self._state.favorite_keys.add(key)
            self._state.favorites.append(fav)

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

    async def _emit_discovery(self, seed_artist: dict) -> None:
        """Push the Discovery screen for the given seed artist.

        The screen starts empty. Tracks stream in afterwards via
        ``add_track`` commands emitted from ``on_task_update`` as
        workers find them.
        """
        await self.send_command(
            "screen",
            {
                "screen": "discovery",
                "seed_artist": {
                    "id": str(seed_artist.get("id") or ""),
                    "name": seed_artist.get("name") or "",
                    "image_url": seed_artist.get("image_url") or "",
                },
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
        elif top.screen == "discovery":
            seed = (
                await self._catalog_get_artist(top.discovery_seed_id or "")
                if top.discovery_seed_id
                else None
            )
            if seed is None:
                seed = {
                    "id": top.discovery_seed_id or "",
                    "name": top.discovery_seed_name or "",
                    "image_url": "",
                }
            await self._emit_discovery(seed)
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
        # Music-player convention: every tool result carries a
        # ``description`` (for the voice agent's LLM-paraphrase
        # fallback) plus an optional ``speak`` (for verbatim TTS).
        # ``respond_to_task`` handles the task-id lookup + bookkeeping.
        await self.respond_to_task({"description": description}, speak=speak, status=status)
