import type {
  Album,
  Artist as ArtistType,
  ArtistTab,
  MinimalArtist,
  Song,
} from "../types";
import { Grid, GridCell } from "../components/Grid";

interface ArtistProps {
  artist: ArtistType;
  activeTab: ArtistTab;
  onSelectItem: (kind: "album" | "song", item: Album | Song) => void;
  onSelectRelated: (artist: MinimalArtist) => void;
  onSelectTab: (tab: ArtistTab) => void;
}

function formatDuration(seconds: number): string {
  const m = Math.floor(seconds / 60);
  const s = seconds % 60;
  return `${m}:${String(s).padStart(2, "0")}`;
}

const TABS: { key: ArtistTab; label: string }[] = [
  { key: "albums", label: "Albums" },
  { key: "songs", label: "Songs" },
  { key: "related", label: "Related artists" },
];

export function Artist({
  artist,
  activeTab,
  onSelectItem,
  onSelectRelated,
  onSelectTab,
}: ArtistProps) {
  const columns = 8;
  const albumCoverById = new Map(artist.albums.map((a) => [a.id, a.cover_url]));
  const related = artist.related_artists ?? null;
  return (
    <div className="screen artist-screen">
      <div className="artist-header">
        <img src={artist.image_url} alt="" className="artist-header-image" />
        <div className="artist-header-text">
          <h1 className="screen-title">{artist.name}</h1>
          {artist.genre && <div className="artist-genre">{artist.genre}</div>}
          {artist.short_description && (
            <p className="artist-short-description">
              {artist.short_description}
            </p>
          )}
        </div>
      </div>
      <div className="tab-bar" role="tablist">
        {TABS.map((tab) => (
          <button
            key={tab.key}
            type="button"
            role="tab"
            aria-selected={activeTab === tab.key}
            className={`tab-button ${activeTab === tab.key ? "active" : ""}`}
            onClick={() => onSelectTab(tab.key)}
          >
            {tab.label}
          </button>
        ))}
      </div>
      {activeTab === "albums" && (
        <Grid columns={columns}>
          {artist.albums.map((album, index) => (
            <GridCell
              key={album.id}
              title={album.title}
              subtitle={String(album.year)}
              imageUrl={album.cover_url}
              row={Math.floor(index / columns) + 1}
              col={(index % columns) + 1}
              onClick={() => onSelectItem("album", album)}
            />
          ))}
        </Grid>
      )}
      {activeTab === "songs" && (
        <Grid columns={columns}>
          {artist.songs.map((song, index) => (
            <GridCell
              key={song.id}
              title={song.title}
              subtitle={formatDuration(song.duration_seconds)}
              imageUrl={
                song.cover_url ?? albumCoverById.get(song.album_id) ?? artist.image_url
              }
              row={Math.floor(index / columns) + 1}
              col={(index % columns) + 1}
              onClick={() => onSelectItem("song", song)}
            />
          ))}
        </Grid>
      )}
      {activeTab === "related" && (
        <RelatedTab
          related={related}
          columns={columns}
          onSelect={onSelectRelated}
        />
      )}
    </div>
  );
}

interface RelatedTabProps {
  related: MinimalArtist[] | null;
  columns: number;
  onSelect: (artist: MinimalArtist) => void;
}

function RelatedTab({ related, columns, onSelect }: RelatedTabProps) {
  if (related === null) {
    return <div className="tab-loading">Loading similar artists…</div>;
  }
  if (related.length === 0) {
    return (
      <div className="tab-empty">
        No similar artists found for this artist.
      </div>
    );
  }
  return (
    <Grid columns={columns}>
      {related.map((r, index) => (
        <GridCell
          key={r.id}
          title={r.name}
          subtitle="Artist"
          imageUrl={r.image_url}
          row={Math.floor(index / columns) + 1}
          col={(index % columns) + 1}
          onClick={() => onSelect(r)}
        />
      ))}
    </Grid>
  );
}
