import { useEffect, useRef } from "react";
import type { PipecatBaseChildProps } from "@pipecat-ai/voice-ui-kit";
import { UIAgentProvider, useA11ySnapshot } from "@pipecat-ai/client-react";
import { useServerMessages } from "./hooks/useServerMessages";
import { useClickSender } from "./hooks/useClickSender";
import { Header } from "./components/Header";
import { Toast } from "./components/Toast";
import { Welcome } from "./screens/Welcome";
import { Home } from "./screens/Home";
import { Artist } from "./screens/Artist";
import { Detail } from "./screens/Detail";
import { Trending } from "./screens/Trending";
import type { Screen } from "./types";

function screenIdentity(s: Screen): string {
  switch (s.kind) {
    case "home":
      return "home";
    case "artist":
      return `artist:${s.artist.id}`;
    case "detail":
      return `detail:${s.item.id}`;
    case "trending":
      return `trending:${s.genre ?? ""}`;
  }
}

export function App(props: PipecatBaseChildProps) {
  return (
    <UIAgentProvider client={props.client ?? undefined}>
      <AppInner {...props} />
    </UIAgentProvider>
  );
}

function AppInner({ handleConnect, handleDisconnect }: PipecatBaseChildProps) {
  const { screen, toast, nowPlaying, closeToast } = useServerMessages();
  const sendClick = useClickSender();
  const mainRef = useRef<HTMLElement>(null);
  const lastScreenId = useRef<string | null>(null);

  // Spike: stream an accessibility snapshot of the app to the server
  // so the UI agent has structural awareness of what's on screen
  // without hand-written prose descriptions.
  useA11ySnapshot();

  // Reset the scroll position to the top whenever we land on a
  // different page. Same-page data updates (e.g. the Related Artists
  // row being populated on the current artist) keep the same identity
  // string and don't trigger, so the server-directed scroll_to="related"
  // can do its job without being undone.
  const currentScreenId = screenIdentity(screen);
  useEffect(() => {
    if (
      lastScreenId.current !== null &&
      lastScreenId.current !== currentScreenId
    ) {
      mainRef.current?.scrollTo({ top: 0, behavior: "auto" });
    }
    lastScreenId.current = currentScreenId;
  }, [currentScreenId]);

  // Pre-connect welcome: no header, card centered in the viewport.
  const isWelcome = screen.kind === "home" && screen.artists.length === 0;
  if (isWelcome) {
    return (
      <div className="app app--welcome">
        <Welcome onConnect={handleConnect} onDisconnect={handleDisconnect} />
      </div>
    );
  }

  const backEnabled =
    (screen.kind === "artist" ||
      screen.kind === "detail" ||
      screen.kind === "trending") &&
    screen.backEnabled;

  return (
    <div className="app">
      <Header
        onConnect={handleConnect}
        onDisconnect={handleDisconnect}
        onBack={() => sendClick({ kind: "nav", view: "back" })}
        onHome={() => sendClick({ kind: "nav", view: "home" })}
        backEnabled={backEnabled}
        nowPlaying={nowPlaying?.title ?? null}
      />
      <main className="main" ref={mainRef}>
        {screen.kind === "home" && (
          <Home
            artists={screen.artists}
            newReleases={screen.new_releases}
            favorites={screen.favorites}
            onSelectArtist={(artist) =>
              sendClick({
                kind: "nav",
                view: "artist",
                artist_id: artist.id,
              })
            }
            onSelectNewRelease={(release) =>
              sendClick({
                kind: "nav",
                view: "detail",
                detail_kind: "album",
                item_id: release.id,
                artist_id: release.artist_id,
              })
            }
            onSelectFavorite={(fav) =>
              sendClick({
                kind: "nav",
                view: "detail",
                detail_kind: fav.kind,
                item_id: fav.item_id,
                artist_id: fav.artist_id,
              })
            }
          />
        )}
        {screen.kind === "artist" && (
          <Artist
            artist={screen.artist}
            activeTab={screen.activeTab}
            onSelectItem={(kind, item) =>
              sendClick({
                kind: "nav",
                view: "detail",
                detail_kind: kind,
                item_id: item.id,
                artist_id: screen.artist.id,
              })
            }
            onSelectRelated={(r) =>
              sendClick({
                kind: "nav",
                view: "artist",
                artist_id: r.id,
              })
            }
            onSelectTab={(tab) =>
              sendClick({
                kind: "set_tab",
                artist_id: screen.artist.id,
                tab,
              })
            }
          />
        )}
        {screen.kind === "trending" && (
          <Trending
            label={screen.label}
            artists={screen.artists}
            onSelectArtist={(a) =>
              sendClick({
                kind: "nav",
                view: "artist",
                artist_id: a.id,
              })
            }
          />
        )}
        {screen.kind === "detail" && (
          <Detail
            kind={screen.detailKind}
            item={screen.item}
            artist={screen.artist}
            isFavorite={screen.isFavorite}
            isPlaying={screen.isPlaying}
            playingTrackId={screen.playingTrackId}
            onPlay={() =>
              sendClick({
                kind: "action",
                action: "play",
                item_id: screen.item.id,
                artist_id: screen.artist.id,
              })
            }
            onShowInfo={() =>
              sendClick({
                kind: "action",
                action: "show_info",
                item_id: screen.item.id,
                artist_id: screen.artist.id,
              })
            }
            onAddToFavorites={() =>
              sendClick({
                kind: "action",
                action: "add_to_favorites",
                item_id: screen.item.id,
                artist_id: screen.artist.id,
              })
            }
            onPlayTrack={(trackId) =>
              sendClick({
                kind: "play_track",
                artist_id: screen.artist.id,
                album_id: screen.item.id,
                track_id: trackId,
              })
            }
          />
        )}
      </main>
      <Toast toast={toast} onClose={closeToast} />
    </div>
  );
}
