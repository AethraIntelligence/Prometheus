import { StopIcon } from "../../../shared/ui";

/** Drawn where the send button is, because while work runs that is the one action. */
export function StopButton({ onStop }: { onStop: () => void | Promise<void> }) {
  return (
    <button type="button" className="send stop" aria-label="Stop" onClick={() => void onStop()}>
      <StopIcon />
    </button>
  );
}
