import { StrictMode } from "react";
import { createRoot } from "react-dom/client";

import type { PipecatBaseChildProps } from "@pipecat-ai/voice-ui-kit";
import {
  ErrorCard,
  PipecatAppBase,
  SpinLoader,
} from "@pipecat-ai/voice-ui-kit";

import { App } from "./App";

import "./index.css";

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <PipecatAppBase
      connectParams={{ webrtcUrl: "/api/offer" }}
      initDevicesOnMount
      transportType="smallwebrtc"
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
  </StrictMode>,
);
