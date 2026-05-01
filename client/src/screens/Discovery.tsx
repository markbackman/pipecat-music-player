import { useUITasks } from "@pipecat-ai/client-react";
import type { DiscoveryTrack, MinimalArtist } from "../types";
import type { Task, TaskGroup } from "@pipecat-ai/client-react";

interface DiscoveryProps {
  seedArtist: MinimalArtist;
  tracks: DiscoveryTrack[];
  onPlayTrack: (track: DiscoveryTrack) => void;
}

const COLUMNS = 4;

const SOURCE_LABELS: Record<string, string> = {
  similar_artist: "Similar artists",
  genre: "Genre chart",
  chart: "Global chart",
};

function sourceLabel(source: string): string {
  return SOURCE_LABELS[source] ?? source;
}

/**
 * The Discovery screen renders a parallel-recommender session. The
 * seed artist is shown at the top. While workers are running an
 * in-flight panel summarizes per-worker status and exposes a Cancel
 * button. As workers stream tracks back, cards fill into the grid
 * below; clicking a card plays the track.
 *
 * In-flight state comes from ``useUITasks`` (the React UITasksProvider
 * subscribes to ``ui.task`` envelopes from the SDK). Track state
 * comes from ``add_track`` custom commands handled in
 * ``useServerMessages`` and threaded down as a prop.
 */
export function Discovery({
  seedArtist,
  tracks,
  onPlayTrack,
}: DiscoveryProps) {
  const { groups, cancelTask } = useUITasks();
  const inflight = pickActiveDiscoveryGroup(groups, seedArtist.id);

  return (
    <div className="screen discovery-screen">
      <header className="discovery-header">
        {seedArtist.image_url && (
          <img
            src={seedArtist.image_url}
            alt=""
            className="discovery-seed-image"
          />
        )}
        <div>
          <div className="discovery-eyebrow">Discoveries based on</div>
          <h1 className="screen-title">{seedArtist.name}</h1>
        </div>
      </header>

      {inflight && (
        <section className="discovery-inflight">
          <div className="discovery-inflight-header">
            <div className="discovery-inflight-label">
              {inflight.label ?? "Searching"}
            </div>
            {inflight.cancellable && inflight.status === "running" && (
              <button
                className="discovery-cancel"
                type="button"
                onClick={() => cancelTask(inflight.taskId, "user requested")}
              >
                Cancel
              </button>
            )}
          </div>
          <ul className="discovery-workers">
            {inflight.tasks.map((t) => (
              <WorkerRow key={t.agentName} task={t} />
            ))}
          </ul>
        </section>
      )}

      {tracks.length > 0 ? (
        <section className="discovery-tracks-section">
          <h2 className="grid-label">Tracks</h2>
          <div
            className="discovery-grid"
            role="grid"
            aria-colcount={COLUMNS}
            style={{
              gridTemplateColumns: `repeat(${COLUMNS}, minmax(0, 1fr))`,
            }}
          >
            {tracks.map((t, index) => (
              <button
                key={t.id}
                className="discovery-card"
                data-row={Math.floor(index / COLUMNS) + 1}
                data-col={(index % COLUMNS) + 1}
                onClick={() => onPlayTrack(t)}
              >
                {t.cover_url && (
                  <img src={t.cover_url} alt="" className="discovery-cover" />
                )}
                <div className="discovery-card-body">
                  <div className="discovery-card-title">{t.title}</div>
                  <div className="discovery-card-artist">{t.artist_name}</div>
                  <div
                    className="discovery-card-source"
                    data-source={t.source}
                  >
                    {sourceLabel(t.source)}
                  </div>
                </div>
              </button>
            ))}
          </div>
        </section>
      ) : (
        <div className="discovery-empty">
          {inflight
            ? "Searching for tracks…"
            : "No tracks yet. Ask the assistant to find something."}
        </div>
      )}
    </div>
  );
}

function WorkerRow({ task }: { task: Task }) {
  const lastUpdate = task.updates[task.updates.length - 1];
  const updateText =
    lastUpdate &&
    typeof lastUpdate.data === "object" &&
    lastUpdate.data !== null &&
    "text" in lastUpdate.data
      ? String((lastUpdate.data as { text?: unknown }).text ?? "")
      : "";

  return (
    <li className="discovery-worker">
      <span className="discovery-worker-name">
        {sourceLabel(task.agentName)}
      </span>
      <span className="discovery-worker-update">
        {task.status === "running"
          ? updateText || "starting…"
          : task.status === "completed"
            ? "done"
            : task.status}
      </span>
      <span
        className="discovery-worker-status"
        data-status={task.status}
      >
        {task.status}
      </span>
    </li>
  );
}

/**
 * Pick the in-flight task group that matches the current seed
 * artist. We look at the label (set server-side to ``"Discoveries:
 * <artist name>"``) to disambiguate from any unrelated user task
 * groups the app might dispatch in the future.
 */
function pickActiveDiscoveryGroup(
  groups: TaskGroup[],
  seedArtistId: string,
): TaskGroup | null {
  // Most recent first; pick the freshest discovery group that's
  // either still running or hasn't been replaced.
  for (let i = groups.length - 1; i >= 0; i--) {
    const g = groups[i];
    if (!g.label?.startsWith("Discoveries:")) continue;
    // We don't currently thread the seed_artist_id through the
    // label; matching by "is this a Discoveries group at all" is
    // sufficient for the single-discovery-at-a-time UX.
    void seedArtistId;
    return g;
  }
  return null;
}
