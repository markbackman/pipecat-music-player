# Pipecat Music Player

A voice-driven music browsing app built on [Pipecat](https://github.com/pipecat-ai/pipecat) and [pipecat-subagents](https://github.com/markbackman/pipecat-subagents). Demonstrates the **voice / UI separation-of-concerns pattern**: one agent handles the spoken conversation, a second agent owns the UI state, and a third agent owns the music catalog. Talk to browse trending artists, play 30-second previews, favorite songs, ask conversational questions about a catalog, and more.

Catalog data is live from [Deezer](https://developers.deezer.com/api). Descriptions and answers are generated on demand via OpenAI `gpt-4o-mini`. No database. No auth. No seeded catalog.

## Architecture

```
MusicAgent (transport + BusBridge + UI bridge)
  ├── VoiceAgent (LLM, bridged)
  │     └── @tool handle_request(query)
  │           └── request_task("ui")
  └── UIAgent (LLM, not bridged; ScrollToToolMixin + HighlightToolMixin)
        ├── tools: navigate_to_artist, select_item, play, control_playback,
        │          show_info, answer, add_to_favorites, show_albums,
        │          show_songs, show_similar_artists, show_trending,
        │          go_back, go_home, describe_screen
        ├── @on_ui_event handlers: nav, action, set_tab, play_track
        └── inherited mixin tools: scroll_to, highlight (silent
            fire-and-forget)

CatalogAgent (runner peer, long-lived)
  ├── Deezer-backed artist + album + track cache
  ├── LLM-generated descriptions
  └── Task API: list_home, list_new_releases, find_artist, get_artist,
      get_artist_tracks, resolve_item, get_description, related_artists,
      get_trending, get_album_preview, ...
```

- **MusicAgent** (`server/bot.py`): owns the Pipecat transport and bridges frames to the voice agent. Calls the SDK's `attach_ui_bridge(...)` from `on_ready` so client `ui.event` messages flow onto the bus as `BusUIEventMessage` and `UIAgent.send_command(...)` delivers `ui.command` messages back to the client.
- **VoiceAgent** (`server/voice_agent.py`): bridged LLM agent. Its only tool is `handle_request`, which forwards the user's utterance verbatim to the UI agent and speaks the reply.
- **UIAgent** (`server/ui_agent.py`): owns a navigation stack (home → artist → detail → trending) and emits `Navigate` / `ScrollTo` / `Toast` commands via the SDK's UI command pipe. Voice requests go through its own LLM (with the latest `<ui_state>` auto-injected); client click events route through `@on_ui_event` handlers without running the LLM, for low latency.
- **CatalogAgent** (`server/catalog_agent.py`): process-lifetime singleton that owns the Deezer-backed catalog and description cache. Everything that needs music data goes through its task API.

## Features

- **Home**: Trending artists (live Deezer chart), New releases (Deezer editorial feed), Favorites — three 8-column grids.
- **Artist pages**: Albums / Songs / Related tabs. Full discography (not capped), top 16 songs, lazy-loaded related artists.
- **Album detail with tracklist**: 30-second Deezer previews per track. Click a track to play; click again to stop.
- **Global search**: say "play Bohemian Rhapsody" from anywhere, and the server resolves via Deezer's track search before falling back to album search.
- **Conversational Q&A**: "what's their latest album?", "what's their most iconic album?", "are they still active?" route through a single `answer(text, about=None)` tool. The UI agent's LLM writes the spoken reply inline as the `text` argument, grounded by the current `<ui_state>` for visible facts and training knowledge for trivia.
- **Descriptions**: LLM-generated, grounded by Deezer metadata, cached in-process. Short line under each detail cover; long form appears in toast cards that auto-dismiss when the bot stops speaking.
- **Trending by genre**: "what's trending in alternative?" works for any Deezer genre, derived from the per-genre track chart (since Deezer's artist chart endpoint ignores genre).
- **Favorites** stored in session memory.

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

- **"Show me Taylor Swift"** — artist page, 8-col Albums grid.
- **"What's their latest album?"** — catalog Q&A, voice answer + toast for the album.
- **"Show me the songs"** — switches the Artist page tab.
- **"Play London Calling"** — resolves even when the catalog only has "London Calling (Remastered)".
- **"Play Bohemian Rhapsody"** from home — global Deezer search loads Queen and starts playback.
- **"What's trending in metal?"** — genre chart derived from the track feed.
- **"Show me similar artists"** — Related tab fetches on demand.
- **"Tell me about Nevermind"** — long-description toast, auto-dismisses when narration ends.
- **"Most iconic album?"** — music-trivia answer drawn from training knowledge, grounded by the artist's catalog.

## Reference patterns

- **Voice/UI split via task dispatch**: `voice_agent.py` uses `async with self.task("ui", payload={"query": query})` to hand the user's utterance to `ui_agent.py`. The UI agent completes the task with a `speak` field; the voice agent hands that verbatim to TTS without re-running its LLM.
- **SDK UI agent protocol**: `bot.py` calls `attach_ui_bridge(self, target="ui")` from `on_ready`. Client `UIAgentClient.sendEvent(name, payload)` calls land on the bus as `BusUIEventMessage` and dispatch to `@on_ui_event(name)` handlers on the UI agent. Server-side `send_command(name, payload)` flows the other way through the bridge as an `RTVIServerMessageFrame`.
- **Accessibility snapshots as `<ui_state>`**: the React client calls `useA11ySnapshot()` near the app root, which streams the document's accessibility tree to the server. The UI agent stores the latest snapshot and auto-injects it as `<ui_state>` at the start of every task, so the LLM always reasons over the current screen.
- **Silent fire-and-forget mixin tools**: `ScrollToToolMixin` and `HighlightToolMixin` are inherited from the SDK; the LLM picks `scroll_to(ref)` or `highlight(ref)` and the visual change on the client is the user-facing feedback (the voice agent stays quiet for that turn).
- **Long-lived singleton agent**: `CatalogAgent` is spawned as a runner peer (not per-connect) so its Deezer cache survives across clients and its expensive warm-up runs once per process.
