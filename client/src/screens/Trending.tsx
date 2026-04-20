import type { MinimalArtist } from "../types";
import { Grid, GridCell } from "../components/Grid";

interface TrendingProps {
  label: string;
  artists: MinimalArtist[];
  onSelectArtist: (artist: MinimalArtist) => void;
}

const COLUMNS = 8;

export function Trending({ label, artists, onSelectArtist }: TrendingProps) {
  return (
    <div className="screen trending-screen">
      <h1 className="screen-title">{label}</h1>
      {artists.length > 0 ? (
        <Grid columns={COLUMNS}>
          {artists.map((a, index) => (
            <GridCell
              key={a.id}
              title={a.name}
              subtitle="Artist"
              imageUrl={a.image_url}
              row={Math.floor(index / COLUMNS) + 1}
              col={(index % COLUMNS) + 1}
              onClick={() => onSelectArtist(a)}
            />
          ))}
        </Grid>
      ) : (
        <div className="trending-empty">
          No chart data right now. Try again in a moment.
        </div>
      )}
    </div>
  );
}
