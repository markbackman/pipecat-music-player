# Pipecat Music Player

A voice-driven music browsing app built on [Pipecat](https://github.com/pipecat-ai/pipecat) and [Pipecat Subagents](https://github.com/markbackman/pipecat-subagents). Demonstrates two patterns:

- **Voice / UI separation-of-concerns**: a voice agent handles the spoken conversation, a UI agent owns the screen state, a catalog agent owns the data.
- **Parallel fan-out with streaming results**: "show me artists like Radiohead" dispatches to three worker agents that pull candidates from different similarity angles in parallel and stream tracks back into a Discovery screen as they find them.

Talk to browse trending artists, play 30-second previews, favorite songs, ask conversational questions about the catalog, get recommendations, and more.

Catalog data is live from [Deezer](https://developers.deezer.com/api). Descriptions and answers are generated on demand via OpenAI `gpt-4.1`. No database. No auth. No seeded catalog.

## Architecture

```
MusicAgent (transport + BusBridge + UI bridge)
  ├── VoiceAgent (LLM, bridged)
  │     └── @tool handle_request(query)
  │           └── request_task("ui")
  ├── UIAgent (LLM, not bridged)
  │     ├── tools: navigate_to_artist, select_item, play, control_playback,
  │     │          show_info, answer, add_to_favorites, show_albums,
  │     │          show_songs, show_similar_artists, show_trending,
  │     │          start_discovery, go_back, go_home, describe_screen
  │     ├── silent fire-and-forget tools: scroll_to, highlight
  │     └── @on_ui_event handlers: nav, action, set_tab, play_track,
  │                                track_click
  └── Discovery workers (BaseAgent, session-scoped)
        ├── SimilarArtistRecommender ("similar_artist")
        ├── GenreRecommender ("genre")
        └── TwoHopRecommender ("two_hop")

CatalogAgent (runner peer, long-lived)
  ├── Deezer-backed artist + album + track cache
  ├── LLM-generated descriptions
  └── Task API: list_home, list_new_releases, find_artist, get_artist,
      get_artist_tracks, resolve_item, get_description, related_artists,
      get_trending, fetch_artist_by_id, get_album_preview, ...
```

- **MusicAgent** (`server/bot.py`): owns the Pipecat transport and bridges frames to the voice agent. Calls the SDK's `attach_ui_bridge(...)` from `on_ready` so client `ui.event` messages flow onto the bus as `BusUIEventMessage` and `UIAgent.send_command(...)` delivers `ui.command` messages back to the client.
- **VoiceAgent** (`server/voice_agent.py`): bridged LLM agent. Its only tool is `handle_request`, which forwards the user's utterance verbatim to the UI agent and speaks the reply.
- **UIAgent** (`server/ui_agent.py`): owns a navigation stack (home / artist / detail / trending / discovery) and emits screen pushes, toasts, scroll, and highlight commands via the SDK's UI command pipe. Voice requests go through its own LLM (with the latest `<ui_state>` auto-injected); client click events route through `@on_ui_event` handlers without running the LLM, for low latency.
- **Discovery workers** (`server/discovery_workers.py`): three short-named `BaseAgent` subclasses spawned alongside the UI agent. The UI agent's `start_discovery` tool fans out to them via `start_user_task_group(...)`. Each pulls candidate artists from a different similarity angle and streams tracks back as `send_task_update(data={"kind": "track", ...})`. The UI agent's `on_task_update` interception turns each one into an `add_track` UI command so the Discovery grid fills as workers find tracks.
- **CatalogAgent** (`server/catalog_agent.py`): process-lifetime singleton that owns the Deezer-backed catalog and description cache. Everything that needs music data goes through its task API. Module-level Deezer concurrency is capped (`deezer.py`) so fan-out paths can't trip the IP rate limit.

## Features

- **Home**: three 8-column grids stacked top to bottom: Trending artists (live Deezer chart), New releases (Deezer editorial feed), and Favorites.
- **Artist pages**: Albums / Songs / Related tabs. Full discography (not capped), top 16 songs, lazy-loaded related artists.
- **Album detail with tracklist**: 30-second Deezer previews per track. Click a track to play; click again to stop.
- **Discoveries**: "show me artists like Radiohead" opens a Discovery screen and fans out to three parallel recommenders, all scoped to similarity with the seed: direct similar artists, peers in the seed's genre, and the broader neighborhood (artists similar to the seed's similar). Tracks stream into the screen as workers find them; click any card to play, or cancel mid-search.
- **Global search**: say "play Bohemian Rhapsody" from anywhere, and the server resolves via Deezer's track search before falling back to album search.
- **Conversational Q&A**: "what's their latest album?", "what's their most iconic album?", "are they still active?" route through a single `answer(text, about=None)` tool. The UI agent's LLM writes the spoken reply inline as the `text` argument, grounded by the current `<ui_state>` for visible facts and training knowledge for trivia.
- **Descriptions**: LLM-generated, grounded by Deezer metadata, cached in-process. Short line under each detail cover; long form appears in toast cards that auto-dismiss when the bot stops speaking.
- **Trending by genre**: "what's trending in alternative?" works for any Deezer genre, derived from the per-genre track chart (since Deezer's artist chart endpoint ignores genre).
- **Favorites** stored in session memory.
- **Multi-turn context**: the UI agent runs with `keep_history=True` and Pipecat's auto context summarization, so deictic references ("play that one", "what about the other album?") resolve across turns and the running context stays bounded over long sessions.

## Running

### Prerequisites

- Python 3.11+, [`uv`](https://docs.astral.sh/uv/)
- Node 20+, `npm`
- API keys: OpenAI, Soniox, Cartesia, Daily

### Environment

Create `server/.env` with:

```
OPENAI_API_KEY=...
SONIOX_API_KEY=...
CARTESIA_API_KEY=...
DAILY_API_KEY=...
```

### Start the server

```bash
cd server
uv sync
uv run bot.py
```

Binds to `http://localhost:7860` (SmallWebRTC) by default. Pass `--transport daily` for a Daily room.

### Start the client

```bash
cd client
npm install
npm run dev
```

Open http://localhost:5173, click **Connect**, and start talking.

## Things to try

- **"Show me Taylor Swift"**: artist page, 8-col Albums grid.
- **"What's their latest album?"**: catalog Q&A, voice answer + toast for the album.
- **"Show me the songs"**: switches the Artist page tab.
- **"Play London Calling"**: resolves even when the catalog only has "London Calling (Remastered)".
- **"Play Bohemian Rhapsody"** from home: global Deezer search loads Queen and starts playback.
- **"What's trending in metal?"**: genre chart derived from the track feed.
- **"Show me similar artists"**: Related tab fetches on demand.
- **"Tell me about Nevermind"**: long-description toast, auto-dismisses when narration ends.
- **"Most iconic album?"**: music-trivia answer drawn from training knowledge, grounded by the artist's catalog.
- **"Show me artists like Radiohead"**: opens a Discovery screen with three parallel workers streaming tracks in. The in-flight panel shows per-worker status with a Cancel button; click any track card to play its preview.

## Reference patterns

- **Voice/UI split via task dispatch**: `voice_agent.py` uses `async with self.task("ui", payload={"query": query})` to hand the user's utterance to `ui_agent.py`. The UI agent completes the task with a `speak` field; the voice agent hands that verbatim to TTS without re-running its LLM.
- **SDK UI agent protocol**: `bot.py` calls `attach_ui_bridge(self, target="ui")` from `on_ready`. Client `UIAgentClient.sendEvent(name, payload)` calls land on the bus as `BusUIEventMessage` and dispatch to `@on_ui_event(name)` handlers on the UI agent. Server-side `send_command(name, payload)` flows the other way through the bridge as an `RTVIServerMessageFrame`.
- **Accessibility snapshots as `<ui_state>`**: the React client calls `useA11ySnapshot()` near the app root, which streams the document's accessibility tree to the server. The UI agent stores the latest snapshot and auto-injects it as `<ui_state>` at the start of every task, so the LLM always reasons over the current screen.
- **Silent fire-and-forget action tools**: `scroll_to(ref)` and `highlight(ref)` are defined locally on the UI agent. They send the UI command, complete the in-flight task with no `speak`, and exit. The visual change on the client is the user-facing feedback; the voice agent stays quiet for that turn. The SDK ships `ReplyToolMixin` as a single bundled `reply(answer, scroll_to=None, highlight=None, ...)` tool with a required `answer` argument. This app uses a different shape where each domain tool (`play`, `navigate_to_artist`, `answer`, etc.) IS the whole turn, with tool-specific spoken responses via `_respond`. The local `scroll_to` and `highlight` are silent-terminator `@tool` wrappers around the SDK's helper methods that fit that shape.
- **Long-lived singleton agent**: `CatalogAgent` is spawned as a runner peer (not per-connect) so its Deezer cache survives across clients and its expensive warm-up runs once per process.
- **Parallel fan-out with streaming results**: `start_discovery(seed_artist)` calls `start_user_task_group("similar_artist", "genre", "two_hop", payload=..., label=..., cancellable=True)`. The three worker agents process the same payload in parallel and stream tracks via `send_task_update(data={"kind": "track", "track": ...})`. On the UI agent side, `on_task_update` intercepts those and emits `add_track` UI commands so the grid fills incrementally. The SDK's task-lifecycle envelopes (`group_started`, `task_update`, `task_completed`, `group_completed`) flow to the client unchanged, so the React side gets the in-flight panel + per-worker progress + cancel button via `useUITasks()` without app-specific wiring.
- **Ack-first ordering for slow tools**: `start_discovery` pushes a placeholder Discovery screen and speaks the ack first, then resolves the seed via the catalog, then re-emits the screen with the canonical artist record before firing the workers. Cold catalog seeds can take several seconds; the user gets visible + audible feedback within 2-3s instead of staring at a stalled tool.
