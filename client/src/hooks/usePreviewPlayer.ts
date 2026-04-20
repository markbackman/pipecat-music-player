import { useCallback, useEffect, useRef } from "react";

interface PreviewPlayer {
  play: (url: string) => void;
  pause: () => void;
  resume: () => void;
  stop: () => void;
}

export function usePreviewPlayer(onEnded: () => void): PreviewPlayer {
  const audioRef = useRef<HTMLAudioElement | null>(null);
  const endedRef = useRef(onEnded);
  endedRef.current = onEnded;

  const ensureAudio = useCallback((): HTMLAudioElement => {
    if (audioRef.current) return audioRef.current;
    const audio = new Audio();
    audio.preload = "auto";
    audio.addEventListener("ended", () => endedRef.current());
    audioRef.current = audio;
    return audio;
  }, []);

  const play = useCallback(
    (url: string) => {
      if (!url) return;
      const audio = ensureAudio();
      if (audio.src !== url) {
        audio.src = url;
      }
      audio.currentTime = 0;
      void audio.play().catch(() => {
        // Autoplay policy or network error. Swallow so the UI keeps
        // the "Now Playing" banner even if audio can't start.
      });
    },
    [ensureAudio],
  );

  const pause = useCallback(() => {
    audioRef.current?.pause();
  }, []);

  const resume = useCallback(() => {
    const audio = audioRef.current;
    if (!audio || !audio.src) return;
    void audio.play().catch(() => {});
  }, []);

  const stop = useCallback(() => {
    const audio = audioRef.current;
    if (!audio) return;
    audio.pause();
    audio.currentTime = 0;
  }, []);

  useEffect(() => {
    return () => {
      const audio = audioRef.current;
      if (audio) {
        audio.pause();
        audio.src = "";
      }
    };
  }, []);

  return { play, pause, resume, stop };
}
