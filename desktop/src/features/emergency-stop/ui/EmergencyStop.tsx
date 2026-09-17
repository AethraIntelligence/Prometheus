import { useRuntime } from "../../../shared/api";
import { useEmergencyStop } from "../model/useEmergencyStop";

/**
 * The brake, on every screen.
 *
 * One press stops everything - there is no "are you sure", because the moment
 * somebody reaches for this is the moment a question costs the most. Resuming
 * is the deliberate direction, and it says what it will not do: nothing that
 * was stopped starts again on its own.
 *
 * What was already done is not undone, and the banner says so rather than
 * letting "stopped" read as "reversed".
 */
export function EmergencyStop({ pollMs }: { pollMs?: number }) {
  const client = useRuntime();
  const brake = useEmergencyStop(client, pollMs);

  if (brake.state?.available === false) return null;

  if (brake.state?.engaged) {
    return (
      <div className="estop-banner" role="alert" data-tauri-drag-region>
        <div className="estop-text">
          <b>All work is stopped.</b>{" "}
          <span>{brake.state.reason}</span>
          <small>
            Actions already carried out were not undone. Resuming starts nothing that was stopped.
          </small>
          {brake.problem && <span className="problem-inline">{brake.problem}</span>}
        </div>
        <button
          type="button"
          className="estop-resume"
          disabled={brake.busy}
          onClick={() => void brake.resume()}
        >
          Resume work
        </button>
      </div>
    );
  }

  return (
    <button
      type="button"
      className="estop"
      aria-label="Stop all work"
      title="Stop all work now"
      disabled={brake.busy || brake.state === null}
      onClick={() => void brake.stop()}
    >
      <span className="estop-mark" aria-hidden="true" />
      Stop
    </button>
  );
}
