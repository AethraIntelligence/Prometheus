import { useRuntime } from "../../../shared/api";
import { useEmergencyStop } from "../model/useEmergencyStop";

/**
 * The brake.
 *
 * One press stops everything - there is no "are you sure", because the moment
 * somebody reaches for this is the moment a question costs the most. Resuming
 * is the deliberate direction, and it says what it will not do: nothing that
 * was stopped starts again on its own.
 *
 * What was already done is not undone, and the banner says so rather than
 * letting "stopped" read as "reversed".
 *
 * Two placements, and the split is deliberate. `banner` is on every screen and
 * shows only that work *is* stopped: a machine somebody stopped from the
 * terminal or from another window has to say so wherever the person is
 * looking. `section` is the press itself, in settings beside the rest of what
 * is done to this installation - the button used to float over every page,
 * where it sat in the corner of a conversation nobody was stopping.
 */
export function EmergencyStop({
  pollMs,
  placement = "banner",
}: {
  pollMs?: number;
  placement?: "banner" | "section";
}) {
  const client = useRuntime();
  const brake = useEmergencyStop(client, pollMs);

  if (brake.state?.available === false) return null;

  if (placement === "banner") {
    if (!brake.state?.engaged) return null;
    return (
      <div className="estop-banner" role="alert" data-tauri-drag-region>
        <div className="estop-text">
          <b>All work is stopped.</b> <span>{brake.state.reason}</span>
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

  if (brake.state?.engaged) {
    return (
      <div className="estop-section stopped" role="alert">
        <div className="estop-text">
          <b>All work is stopped.</b> <span>{brake.state.reason}</span>
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
    <div className="estop-section">
      <div className="estop-text">
        <b>Nothing is stopped.</b>
        <small>
          One press answers every parked approval with no, signals every running task and refuses
          new work until somebody resumes it.
        </small>
        {brake.problem && <span className="problem-inline">{brake.problem}</span>}
      </div>
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
    </div>
  );
}
