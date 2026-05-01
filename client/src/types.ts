export interface AlbumTrack {
  id: string;
  title: string;
  duration_seconds: number;
  preview_url?: string;
}

export interface Album {
  id: string;
  title: string;
  year: number;
  cover_url: string;
  preview_url?: string;
  tracks?: AlbumTrack[];
  short_description?: string | null;
  long_description?: string | null;
}

export interface Song {
  id: string;
  title: string;
  album_id: string;
  duration_seconds: number;
  cover_url?: string;
  preview_url?: string;
  short_description?: string | null;
  long_description?: string | null;
}

export interface MinimalArtist {
  id: string;
  name: string;
  image_url: string;
}

export interface Artist {
  id: string;
  name: string;
  genre: string;
  image_url: string;
  albums: Album[];
  songs: Song[];
  short_description?: string | null;
  long_description?: string | null;
  related_artists?: MinimalArtist[];
}

export interface Favorite {
  artist_id: string;
  artist_name: string;
  kind: "album" | "song";
  item_id: string;
  item_title: string;
  cover_url?: string | null;
}

export interface NewRelease {
  id: string;
  title: string;
  year: number;
  release_date: string;
  cover_url: string;
  artist_id: string;
  artist_name: string;
}

export type ArtistTab = "albums" | "songs" | "related";

export interface DiscoveryTrack {
  id: string;
  title: string;
  artist_id: string;
  artist_name: string;
  album_id: string;
  album_title: string;
  preview_url?: string;
  cover_url?: string;
  duration_seconds?: number;
  /**
   * Worker that surfaced this track: ``"similar_artist"``, ``"genre"``,
   * or ``"chart"``. The Discovery screen uses this to label cards.
   */
  source: string;
}

export type Screen =
  | {
      kind: "home";
      artists: MinimalArtist[];
      new_releases: NewRelease[];
      favorites: Favorite[];
    }
  | {
      kind: "artist";
      artist: Artist;
      activeTab: ArtistTab;
      backEnabled: boolean;
    }
  | {
      kind: "detail";
      detailKind: "album" | "song";
      item: Album | Song;
      artist: Artist;
      isFavorite: boolean;
      isPlaying: boolean;
      playingTrackId: string | null;
      backEnabled: boolean;
    }
  | {
      kind: "trending";
      label: string;
      genre: string | null;
      artists: MinimalArtist[];
      backEnabled: boolean;
    }
  | {
      kind: "discovery";
      seedArtist: MinimalArtist;
      backEnabled: boolean;
    };

export interface Toast {
  title: string;
  description: string;
  subtitle?: string;
  image_url?: string;
}

export type ClickEvent =
  | { kind: "nav"; view: "home" }
  | { kind: "nav"; view: "back" }
  | { kind: "nav"; view: "artist"; artist_id: string }
  | {
      kind: "nav";
      view: "detail";
      detail_kind: "album" | "song";
      item_id: string;
      artist_id: string;
    }
  | {
      kind: "action";
      action: "play" | "show_info" | "add_to_favorites";
      item_id: string;
      artist_id: string;
    }
  | {
      kind: "set_tab";
      artist_id: string;
      tab: ArtistTab;
    }
  | {
      kind: "play_track";
      artist_id: string;
      album_id: string;
      track_id: string;
    }
  | {
      kind: "track_click";
      artist_id: string;
      track_id: string;
    };
