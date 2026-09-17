import type { ReadinessState } from "../model/types";

const LABEL: Record<ReadinessState, string> = {
  READY: "Ready",
  DEGRADED: "Degraded",
  UNAVAILABLE: "Unavailable",
};

/** The runtime's readiness verdict as a chip. The state arrives decided. */
export function ReadinessBadge({ state }: { state: ReadinessState }) {
  return <span className={`readiness ${state.toLowerCase()}`}>{LABEL[state] ?? state}</span>;
}
