import type { DailyConnectionEndpoint } from "@pipecat-ai/daily-transport";
import type { APIRequest } from "@pipecat-ai/client-js";

export type TransportType = "daily" | "smallwebrtc";

export const AVAILABLE_TRANSPORTS: TransportType[] = ["daily", "smallwebrtc"];

export const TRANSPORT_LABELS: Record<TransportType, string> = {
  daily: "Daily",
  smallwebrtc: "SmallWebRTC",
};

export const DEFAULT_TRANSPORT: TransportType =
  (import.meta.env.VITE_TRANSPORT as TransportType) || "smallwebrtc";

const botStartUrl =
  import.meta.env.VITE_BOT_START_URL || "http://localhost:7860/start";
const botStartApiKey: string | undefined = import.meta.env
  .VITE_BOT_START_PUBLIC_API_KEY;

if (!import.meta.env.VITE_BOT_START_URL) {
  console.warn(
    "VITE_BOT_START_URL not configured, using default: http://localhost:7860/start",
  );
}

const authHeaders = (): Headers | undefined =>
  botStartApiKey
    ? new Headers({ Authorization: `Bearer ${botStartApiKey}` })
    : undefined;

const dailyConfig: DailyConnectionEndpoint = {
  endpoint: botStartUrl,
  requestData: {
    createDailyRoom: true,
    dailyRoomProperties: { start_video_off: true },
    transport: "daily",
  },
};
const dailyHeaders = authHeaders();
if (dailyHeaders) dailyConfig.headers = dailyHeaders;

const smallWebRTCConfig: APIRequest = {
  endpoint: botStartUrl,
  requestData: {
    createDailyRoom: false,
    enableDefaultIceServers: true,
    transport: "webrtc",
  },
};
const smallWebRTCHeaders = authHeaders();
if (smallWebRTCHeaders) smallWebRTCConfig.headers = smallWebRTCHeaders;

export const TRANSPORT_CONFIG: Record<
  TransportType,
  DailyConnectionEndpoint | APIRequest
> = {
  daily: dailyConfig,
  smallwebrtc: smallWebRTCConfig,
};
