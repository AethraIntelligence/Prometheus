import { useState } from "react";

import { useRuntime } from "../../../shared/api";
import { useRestart, type RestartOptions } from "../model/useRestart";

/**
 * "Restart Prometheus", with the one thing worth knowing before pressing it.
 *
 * Asks first only when something is running, because only then does a restart
 * cost anything: those runs are stopped the cooperative way and can be resumed.
 * A runtime that cannot restart itself is said to be one, and no button is
 * offered that would only fail.
 */
export function RestartButton({
  label = "Restart Prometheus",
  primary = false,
  ...options
}: RestartOptions & { label?: string; primary?: boolean }) {
  const client = useRuntime();
  const runtime = useRestart(client, options);
  const [confirming, setConfirming] = useState(false);

  if (runtime.canRestart === false) {
    return (
      <p className="note restart-manual">
        This runtime cannot restart itself. Restart it where it runs.
      </p>
    );
  }

  if (runtime.phase === "restarting") {
    return (
      <p className="note restart-progress" role="status">
        Restarting… the window reloads when Prometheus is back.
      </p>
    );
  }

  return (
    <span className="actions restart-actions">
      {runtime.problem && (
        <span className="problem-inline" role="alert">
          {runtime.problem}
        </span>
      )}
      {confirming ? (
        <>
          <span className="note">
            {runtime.carrying === 1
              ? "1 run in progress will be stopped."
              : `${runtime.carrying} runs in progress will be stopped.`}
          </span>
          <button type="button" onClick={() => setConfirming(false)}>
            Cancel
          </button>
          <button type="button" className="danger" onClick={() => void runtime.restart()}>
            Stop them and restart
          </button>
        </>
      ) : (
        <button
          type="button"
          className={primary ? "primary" : undefined}
          disabled={runtime.canRestart === null}
          onClick={() =>
            runtime.carrying > 0 ? setConfirming(true) : void runtime.restart()
          }
        >
          {label}
        </button>
      )}
    </span>
  );
}
