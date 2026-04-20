import type { Favorite, MinimalArtist, NewRelease } from "../types";
import { Grid, GridCell } from "../components/Grid";

interface HomeProps {
  artists: MinimalArtist[];
  newReleases: NewRelease[];
  favorites: Favorite[];
  onSelectArtist: (artist: MinimalArtist) => void;
  onSelectNewRelease: (release: NewRelease) => void;
  onSelectFavorite: (favorite: Favorite) => void;
}

const COLUMNS = 8;

export function Home({
  artists,
  newReleases,
  favorites,
  onSelectArtist,
  onSelectNewRelease,
  onSelectFavorite,
}: HomeProps) {
  return (
    <div className="screen home-screen">
      <Grid columns={COLUMNS} label="Trending artists">
        {artists.map((artist, index) => (
          <GridCell
            key={artist.id}
            title={artist.name}
            subtitle={`#${index + 1}`}
            imageUrl={artist.image_url}
            row={Math.floor(index / COLUMNS) + 1}
            col={(index % COLUMNS) + 1}
            onClick={() => onSelectArtist(artist)}
          />
        ))}
      </Grid>
      {newReleases.length > 0 && (
        <Grid columns={COLUMNS} label="New releases">
          {newReleases.map((release, index) => (
            <GridCell
              key={release.id}
              title={release.title}
              subtitle={release.artist_name}
              imageUrl={release.cover_url}
              row={Math.floor(index / COLUMNS) + 1}
              col={(index % COLUMNS) + 1}
              onClick={() => onSelectNewRelease(release)}
            />
          ))}
        </Grid>
      )}
      <FavoritesSection favorites={favorites} onSelect={onSelectFavorite} />
    </div>
  );
}

interface FavoritesSectionProps {
  favorites: Favorite[];
  onSelect: (favorite: Favorite) => void;
}

function FavoritesSection({ favorites, onSelect }: FavoritesSectionProps) {
  if (favorites.length === 0) {
    return (
      <section className="grid-section">
        <h2 className="grid-label">Favorites</h2>
        <div className="favorites-empty">
          <span className="favorites-empty-icon">♡</span>
          <span>
            Nothing here yet. Say "add this to favorites" on any album or
            song to save it.
          </span>
        </div>
      </section>
    );
  }
  return (
    <Grid columns={COLUMNS} label="Favorites">
      {favorites.map((fav, index) => (
        <GridCell
          key={`${fav.artist_id}:${fav.kind}:${fav.item_id}`}
          title={fav.item_title}
          subtitle={`${fav.kind === "album" ? "Album" : "Song"} · ${fav.artist_name}`}
          imageUrl={fav.cover_url ?? undefined}
          row={Math.floor(index / COLUMNS) + 1}
          col={(index % COLUMNS) + 1}
          onClick={() => onSelect(fav)}
        />
      ))}
    </Grid>
  );
}
