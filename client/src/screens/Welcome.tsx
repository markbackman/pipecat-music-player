import { ConnectButton } from "@pipecat-ai/voice-ui-kit";

interface WelcomeProps {
  onConnect?: () => void | Promise<void>;
  onDisconnect?: () => void | Promise<void>;
}

export function Welcome({ onConnect, onDisconnect }: WelcomeProps) {
  return (
    <div className="welcome">
      <div className="welcome-card">
        <div className="welcome-eyebrow">Voice Music Player</div>
        <h1 className="welcome-title">Browse music with your voice</h1>
        <ConnectButton
          size="lg"
          onConnect={onConnect}
          onDisconnect={onDisconnect}
        />
      </div>
    </div>
  );
}
