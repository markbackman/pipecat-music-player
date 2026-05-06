import { useCallback } from "react";
import { useUIEventSender } from "@pipecat-ai/client-react";
import type { ClickEvent } from "../types";

export function useClickSender() {
  const sendEvent = useUIEventSender();
  return useCallback(
    (event: ClickEvent) => {
      const { kind, ...payload } = event;
      sendEvent(kind, payload);
    },
    [sendEvent],
  );
}
