import { StrictMode, useState } from "react";
import { createRoot } from "react-dom/client";

import type { PipecatBaseChildProps } from "@pipecat-ai/voice-ui-kit";
import {
  ErrorCard,
  PipecatAppBase,
  SpinLoader,
} from "@pipecat-ai/voice-ui-kit";

import { App } from "./App";
import { DEFAULT_TRANSPORT, TRANSPORT_CONFIG } from "./config";
import type { TransportType } from "./config";

import "./index.css";

function Main() {
  const [transportType] = useState<TransportType>(DEFAULT_TRANSPORT);
  const connectParams = TRANSPORT_CONFIG[transportType];

  return (
    <PipecatAppBase
      connectParams={connectParams}
      initDevicesOnMount
      transportType={transportType}
      noThemeProvider
    >
      {({ client, handleConnect, handleDisconnect, error }: PipecatBaseChildProps) =>
        !client ? (
          <SpinLoader />
        ) : error ? (
          <ErrorCard>{error}</ErrorCard>
        ) : (
          <App
            client={client}
            handleConnect={handleConnect}
            handleDisconnect={handleDisconnect}
          />
        )
      }
    </PipecatAppBase>
  );
}

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <Main />
  </StrictMode>,
);
