# Pipecat Music Player

A voice-driven music browsing app built on [Pipecat](https://github.com/pipecat-ai/pipecat) and [pipecat-subagents](https://github.com/markbackman/pipecat-subagents). Demonstrates the **voice / UI separation-of-concerns pattern**: one agent handles the spoken conversation, a second agent owns the UI state, and a third agent owns the music catalog. Talk to browse trending artists, play 30-second previews, favorite songs, ask conversational questions about a catalog, and more.

Catalog data is live from [Deezer](https://developers.deezer.com/api). Descriptions and answers are generated on demand via OpenAI `gpt-4o-mini`. No database. No auth. No seeded catalog.

## Architecture

```
MusicAgent (transport + BusBridge + RTVI client-message listener)
  ├── VoiceAgent (LLM, bridged)
  │     └── @tool handle_request(query)
  │           └── request_task("ui")
  └── UIAgent (LLM, not bridged)
        ├── tools: navigate_to_artist, select_item, play, control_playback,
        │          show_info, answer_about_catalog, answer_about_music,
        │          add_to_favorites, show_albums, show_songs,
        │          show_similar_artists, show_trending, go_back, go_home,
        │          describe_screen
        └── on_bus_message: dispatches ui_context click events

CatalogAgent (runner peer, long-lived)
  ├── Deezer-backed artist + album + track cache
  ├── LLM-generated descriptions + conversational answers
  └── Task API: list_home, list_new_releases, find_artist, get_artist,
      get_artist_tracks, resolve_item, get_description, related_artists,
      get_trending, get_album_preview, ...
```

- **MusicAgent** (`server/music_agent.py`): owns the Pipecat transport, bridges frames to the voice agent, and forwards `ui_context` RTVI client messages onto the bus.
- **VoiceAgent** (`server/voice_agent.py`): bridged LLM agent. Its only tool is `handle_request`, which forwards the user's utterance verbatim to the UI agent and speaks the reply.
- **UIAgent** (`server/ui_agent.py`): owns a navigation stack (home → artist → detail → trending) and emits `RTVIServerMessageFrame` screen updates. Routes voice requests through its own LLM, but handles client clicks directly without an LLM call for low latency.
- **CatalogAgent** (`server/catalog_agent.py`): process-lifetime singleton that owns the Deezer-backed catalog, description cache, and Q&A inference. Everything that needs music data goes through its task API.

## Features

- **Home**: Trending artists (live Deezer chart), New releases (Deezer editorial feed), Favorites — three 8-column grids.
- **Artist pages**: Albums / Songs / Related tabs. Full discography (not capped), top 16 songs, lazy-loaded related artists.
- **Album detail with tracklist**: 30-second Deezer previews per track. Click a track to play; click again to stop.
- **Global search**: say "play Bohemian Rhapsody" from anywhere, and the server resolves via Deezer's track search before falling back to album search.
- **Conversational Q&A**: "what's their latest album?", "what's their most iconic album?", "are they still active?" route through dedicated `answer_about_catalog` / `answer_about_music` tools.
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
uv run music_agent.py
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

- **Voice/UI split via task dispatch**: `voice_agent.py` uses `async with self.task("ui", payload={"query": query})` to hand the user's utterance to `ui_agent.py`.
- **Custom bus message for client clicks**: `messages.py` defines `BusUIContextMessage`; `music_agent.py` publishes it on the bus from the RTVI listener; `ui_agent.on_bus_message` spawns a separate asyncio task to handle each event so the dispatcher doesn't deadlock on cross-agent task responses.
- **Long-lived singleton agent**: `CatalogAgent` is spawned as a runner peer (not per-connect) so its Deezer cache survives across clients and its expensive warm-up runs once per process.
- **Tool-invoked inference**: `answer_about_catalog` and `answer_about_music` make their own OpenAI calls inside the tool body, grounded by the structured artist data, so the UI LLM stays focused on routing.
