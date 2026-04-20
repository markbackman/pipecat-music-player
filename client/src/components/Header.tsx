import { ConnectButton, VoiceVisualizer } from "@pipecat-ai/voice-ui-kit";

interface HeaderProps {
  onConnect?: () => void | Promise<void>;
  onDisconnect?: () => void | Promise<void>;
  onBack?: () => void;
  onHome?: () => void;
  backEnabled: boolean;
  nowPlaying: string | null;
}

export function Header({
  onConnect,
  onDisconnect,
  onBack,
  onHome,
  backEnabled,
  nowPlaying,
}: HeaderProps) {
  return (
    <header className="header">
      <div className="header-nav">
        <button
          type="button"
          className="nav-button"
          onClick={onBack}
          disabled={!backEnabled}
          aria-label="Back"
        >
          ← Back
        </button>
        <button
          type="button"
          className="nav-button"
          onClick={onHome}
          aria-label="Home"
        >
          Home
        </button>
      </div>
      <div className="header-now-playing">
        {nowPlaying ? `Now playing: ${nowPlaying}` : "Voice Music Player"}
      </div>
      <div className="header-controls">
        <div
          className="header-visualizer"
          style={{ width: 96, height: 28 }}
        >
          <VoiceVisualizer
            participantType="bot"
            barCount={5}
            barMaxHeight={20}
            barWidth={4}
            barGap={6}
            barOrigin="center"
            backgroundColor="transparent"
            barColor="#7a5aff"
          />
        </div>
        <ConnectButton onConnect={onConnect} onDisconnect={onDisconnect} />
      </div>
    </header>
  );
}
