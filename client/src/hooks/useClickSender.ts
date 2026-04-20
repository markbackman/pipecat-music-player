import { useCallback } from "react";
import { usePipecatClient } from "@pipecat-ai/client-react";
import type { ClickEvent } from "../types";

type OutboundMessage = ClickEvent | { kind: "hello" };

export function useClickSender() {
  const client = usePipecatClient();
  return useCallback(
    (event: OutboundMessage) => {
      if (!client) return;
      client.sendClientMessage("ui_context", event);
    },
    [client],
  );
}
