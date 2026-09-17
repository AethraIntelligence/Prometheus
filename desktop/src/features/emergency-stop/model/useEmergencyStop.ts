/**
 * Whether all work is stopped, kept current, and the two things a person can do about it.
 *
 * Polled, not pushed: a stop may be set from a terminal or the shell's menu, by
 * a process this window never talks to, and the record on disk is the only
 * thing all of them share. The runtime reads it; the window asks the runtime.
 */

import { useCallback, useEffect, useState } from "react";

import { report, type RuntimeClient } from "../../../shared/api";
import { describe } from "../../../shared/lib";
import { emergencyStop, resumeWork, stopState, type StopReport, type StopState } from "../api/stop";

export interface EmergencyStopState {
  state: StopState | null;
  busy: boolean;
  problem: string;
  last: StopReport | null;
  stop: () => Promise<void>;
  resume: () => Promise<void>;
}

export function useEmergencyStop(client: RuntimeClient, pollMs = 2000): EmergencyStopState {
  const [state, setState] = useState<StopState | null>(null);
  const [busy, setBusy] = useState(false);
  const [problem, setProblem] = useState("");
  const [last, setLast] = useState<StopReport | null>(null);

  const refresh = useCallback(async () => {
    try {
      setState(await stopState(client));
    } catch {
      // A runtime that is not answering has no stop state to show; the rest of
      // the window already says it is unreachable.
    }
  }, [client]);

  useEffect(() => {
    void refresh();
    const timer = setInterval(() => void refresh(), pollMs);
    return () => clearInterval(timer);
  }, [refresh, pollMs]);

  const act = useCallback(
    async (action: () => Promise<StopReport>) => {
      setBusy(true);
      setProblem("");
      try {
        const done = await action();
        setLast(done);
        setState(done.state);
      } catch (error) {
        const said = describe(error);
        setProblem(said);
        report(said);
      } finally {
        setBusy(false);
      }
    },
    [],
  );

  const stop = useCallback(() => act(() => emergencyStop(client)), [act, client]);
  const resume = useCallback(() => act(() => resumeWork(client)), [act, client]);

  return { state, busy, problem, last, stop, resume };
}
