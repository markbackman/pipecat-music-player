import { useCallback, useRef, useState } from "react";
import { RTVIEvent } from "@pipecat-ai/client-js";
import { useRTVIClientEvent } from "@pipecat-ai/client-react";
import { usePreviewPlayer } from "./usePreviewPlayer";
import type { Favorite, Screen, ServerMessage, Toast } from "../types";

const TOAST_MAX_DURATION_MS = 20_000;

const INITIAL_SCREEN: Screen = {
  kind: "home",
  artists: [],
  new_releases: [],
  favorites: [],
};

export function useServerMessages() {
  const [screen, setScreen] = useState<Screen>(INITIAL_SCREEN);
  const [favorites, setFavorites] = useState<Favorite[]>([]);
  const [toast, setToast] = useState<Toast | null>(null);
  const [nowPlaying, setNowPlaying] = useState<{
    id: string;
    title: string;
  } | null>(null);
  const toastTimer = useRef<ReturnType<typeof setTimeout> | undefined>(
    undefined,
  );
  // Tracks whether the active toast was raised by a server message that the
  // bot is narrating. Used so BotStoppedSpeaking can auto-dismiss only the
  // narrated toast, not manual ones.
  const toastFollowsBot = useRef<boolean>(false);

  const player = usePreviewPlayer(() => setNowPlaying(null));

  const closeToast = useCallback(() => {
    clearTimeout(toastTimer.current);
    toastFollowsBot.current = false;
    setToast(null);
  }, []);

  const reset = useCallback(() => {
    clearTimeout(toastTimer.current);
    toastFollowsBot.current = false;
    player.stop();
    setScreen(INITIAL_SCREEN);
    setFavorites([]);
    setToast(null);
    setNowPlaying(null);
  }, [player]);

  const showToast = useCallback((t: Toast, followsBot: boolean) => {
    setToast(t);
    toastFollowsBot.current = followsBot;
    clearTimeout(toastTimer.current);
    toastTimer.current = setTimeout(() => {
      toastFollowsBot.current = false;
      setToast(null);
    }, TOAST_MAX_DURATION_MS);
  }, []);

  useRTVIClientEvent(RTVIEvent.ServerMessage, (data: unknown) => {
    const msg = data as ServerMessage;
    if (msg.type === "screen") {
      if (msg.screen === "home") {
        setScreen({
          kind: "home",
          artists: msg.artists,
          new_releases: msg.new_releases ?? [],
          favorites: msg.favorites,
        });
        setFavorites(msg.favorites);
      } else if (msg.screen === "artist") {
        setScreen({
          kind: "artist",
          artist: msg.artist,
          activeTab: msg.active_tab,
          backEnabled: msg.back_enabled,
        });
      } else if (msg.screen === "trending") {
        setScreen({
          kind: "trending",
          label: msg.label,
          genre: msg.genre,
          artists: msg.artists,
          backEnabled: msg.back_enabled,
        });
      } else {
        setScreen({
          kind: "detail",
          detailKind: msg.kind,
          item: msg.item,
          artist: msg.artist,
          isFavorite: msg.is_favorite,
          isPlaying: msg.is_playing,
          playingTrackId: msg.playing_track_id ?? null,
          backEnabled: msg.back_enabled,
        });
      }
    } else if (msg.type === "toast") {
      showToast(
        {
          title: msg.title,
          description: msg.description,
          subtitle: msg.subtitle,
          image_url: msg.image_url,
        },
        true,
      );
    } else if (msg.type === "playback") {
      if (msg.state === "playing") {
        setNowPlaying({ id: msg.item_id, title: msg.item_title });
        if (msg.preview_url) {
          player.play(msg.preview_url);
        } else {
          player.stop();
        }
      } else {
        setNowPlaying(null);
        player.stop();
      }
    } else if (msg.type === "playback_control") {
      if (msg.action === "pause") player.pause();
      else if (msg.action === "resume") player.resume();
      else if (msg.action === "stop") {
        player.stop();
        setNowPlaying(null);
      }
    } else if (msg.type === "favorite_added") {
      setFavorites(msg.favorites);
      setScreen((prev) =>
        prev.kind === "home"
          ? { ...prev, favorites: msg.favorites }
          : prev,
      );
      showToast(
        {
          title: "Added to favorites",
          description: msg.favorite.item_title,
          image_url: msg.favorite.cover_url ?? undefined,
        },
        false,
      );
    } else if (msg.type === "scroll_to") {
      // Defer so React has time to render the flagged section before we
      // try to scroll it into view.
      const target = msg.target;
      requestAnimationFrame(() => {
        const el = document.querySelector<HTMLElement>(
          `[data-scroll-target="${target}"]`,
        );
        el?.scrollIntoView({ behavior: "smooth", block: "start" });
      });
    }
  });

  // When the bot finishes narrating, dismiss a bot-linked toast so the
  // card disappears alongside the voice, matching what the user just heard.
  useRTVIClientEvent(RTVIEvent.BotStoppedSpeaking, () => {
    if (!toastFollowsBot.current) return;
    toastFollowsBot.current = false;
    clearTimeout(toastTimer.current);
    setToast(null);
  });

  return { screen, favorites, toast, nowPlaying, closeToast, reset };
}
