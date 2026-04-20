import type { Album, Artist, Song } from "../types";

interface DetailProps {
  kind: "album" | "song";
  item: Album | Song;
  artist: Artist;
  isFavorite: boolean;
  isPlaying: boolean;
  playingTrackId: string | null;
  onPlay: () => void;
  onShowInfo: () => void;
  onAddToFavorites: () => void;
  onPlayTrack: (trackId: string) => void;
}

function formatDuration(seconds: number): string {
  const m = Math.floor(seconds / 60);
  const s = seconds % 60;
  return `${m}:${String(s).padStart(2, "0")}`;
}

export function Detail({
  kind,
  item,
  artist,
  isFavorite,
  isPlaying,
  playingTrackId,
  onPlay,
  onShowInfo,
  onAddToFavorites,
  onPlayTrack,
}: DetailProps) {
  const songAlbumCover =
    kind === "song"
      ? artist.albums.find((a) => a.id === (item as Song).album_id)?.cover_url
      : undefined;
  const coverUrl =
    kind === "album"
      ? (item as Album).cover_url
      : ((item as Song).cover_url ?? songAlbumCover ?? artist.image_url);
  const subtitle =
    kind === "album"
      ? `Album · ${(item as Album).year}`
      : `Song · ${formatDuration((item as Song).duration_seconds)}`;

  return (
    <div className="screen detail-screen">
      <div className="detail-card">
        <img src={coverUrl} alt="" className="detail-cover" />
        <div className="detail-body">
          <div className="detail-artist">{artist.name}</div>
          <h1 className="detail-title">{item.title}</h1>
          <div className="detail-subtitle">{subtitle}</div>
          {item.short_description && (
            <p className="detail-description">{item.short_description}</p>
          )}
          <div className="detail-actions">
            <button
              type="button"
              className={`action-button primary ${isPlaying ? "active" : ""}`}
              onClick={onPlay}
            >
              {isPlaying ? "Playing" : "Play"}
            </button>
            <button
              type="button"
              className="action-button"
              onClick={onShowInfo}
            >
              More Info
            </button>
            <button
              type="button"
              className={`action-button ${isFavorite ? "active" : ""}`}
              onClick={onAddToFavorites}
            >
              {isFavorite ? "Favorited" : "Add to Favorites"}
            </button>
          </div>
        </div>
      </div>
      {kind === "album" && (
        <AlbumTracklist
          tracks={(item as Album).tracks}
          playingTrackId={playingTrackId}
          onPlayTrack={onPlayTrack}
        />
      )}
    </div>
  );
}

interface AlbumTracklistProps {
  tracks: Album["tracks"];
  playingTrackId: string | null;
  onPlayTrack: (trackId: string) => void;
}

function AlbumTracklist({
  tracks,
  playingTrackId,
  onPlayTrack,
}: AlbumTracklistProps) {
  if (!tracks || tracks.length === 0) {
    return (
      <section className="tracklist">
        <h2 className="tracklist-label">Tracks</h2>
        <div className="tracklist-empty">Loading tracks…</div>
      </section>
    );
  }
  return (
    <section className="tracklist">
      <h2 className="tracklist-label">Tracks</h2>
      <ol className="tracklist-list">
        {tracks.map((track, i) => {
          const isActive = track.id === playingTrackId;
          const canPlay = Boolean(track.preview_url);
          return (
            <li
              key={track.id}
              className={`tracklist-row ${isActive ? "active" : ""}`}
            >
              <span className="tracklist-index">{i + 1}</span>
              <span className="tracklist-title">{track.title}</span>
              <span className="tracklist-duration">
                {formatDuration(track.duration_seconds)}
              </span>
              <button
                type="button"
                className="tracklist-play"
                onClick={() => onPlayTrack(track.id)}
                disabled={!canPlay}
                title={canPlay ? "Play preview" : "No preview available"}
              >
                {isActive ? "Stop" : "Play"}
              </button>
            </li>
          );
        })}
      </ol>
    </section>
  );
}
