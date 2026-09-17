import { useRuntime } from "../../../shared/api";
import { useUpdate } from "../model/useUpdate";

/** Check for a signed update, and install it only when the runtime says it is safe. */
export function UpdatePanel(options: Parameters<typeof useUpdate>[1] = {}) {
  const client = useRuntime();
  const updates = useUpdate(client, options);
  const busy = ["checking", "waiting", "installing"].includes(updates.phase);

  return (
    <>
      <div className="panel-head">
        <h2>Updates</h2>
        {updates.phase === "available" ? (
          <button type="button" className="addbtn" onClick={() => void updates.install()}>
            Install and restart
          </button>
        ) : (
          <button
            type="button"
            className="addbtn"
            disabled={busy}
            onClick={() => void updates.check()}
          >
            Check for updates
          </button>
        )}
      </div>
      <div className="card">
        {updates.phase === "idle" && <p className="card-empty">Not checked in this session.</p>}
        {updates.phase === "checking" && <p className="card-empty">Checking…</p>}
        {updates.phase === "current" && <p className="card-empty">This is the latest version.</p>}
        {updates.phase === "unsupported" && <p className="card-empty">{updates.note}</p>}
        {updates.update && ["available", "waiting", "installing"].includes(updates.phase) && (
          <p className="card-empty">
            Version {updates.update.version} is available (this is{" "}
            {updates.update.currentVersion}). It is checked against this build's signing key
            before it replaces anything.
          </p>
        )}
        {updates.phase === "waiting" && (
          <p className="card-empty" role="status">
            Waiting for running actions to finish…
          </p>
        )}
        {updates.phase === "installing" && (
          <p className="card-empty" role="status">
            Installing{updates.progress !== null ? ` (${updates.progress}%)` : ""}… Prometheus
            restarts when it is done, and work in progress resumes.
          </p>
        )}
        {updates.inFlight.length > 0 && (
          <ul className="update-in-flight">
            {updates.inFlight.map((item) => (
              <li key={`${item.task_id}-${item.tool}-${item.since}`}>
                {item.tool} ({item.effect.toLowerCase()})
              </li>
            ))}
          </ul>
        )}
        {updates.note && updates.phase !== "unsupported" && (
          <p className="card-empty problem-inline" role="alert">
            {updates.note}
          </p>
        )}
      </div>
    </>
  );
}
