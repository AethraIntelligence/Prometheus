/**
 * Restarting the runtime, and waiting for the new one.
 *
 * The runtime stops itself and comes back as a new process on the same port;
 * the window knows the new one by `started_at` changing, not by a request
 * merely succeeding - the old process answers for a moment after it was asked.
 * Then the window reloads, because everything it holds was read from the
 * process that is gone.
 */

import { useCallback, useEffect, useState } from "react";

import { report, type RuntimeClient } from "../../../shared/api";
import { describe } from "../../../shared/lib";
import { requestRestart, runtimeHealth } from "../api/runtime";

export type RestartPhase = "idle" | "restarting" | "failed";

export interface RestartOptions {
  /** What to do once the new process answers. The window reloads by default. */
  onRestarted?: () => void;
  pollMs?: number;
  timeoutMs?: number;
}

export interface RestartState {
  /** Unknown until the runtime has said. */
  canRestart: boolean | null;
  carrying: number;
  phase: RestartPhase;
  problem: string;
  restart: () => Promise<void>;
}

const wait = (ms: number) => new Promise((resolve) => setTimeout(resolve, ms));

export function useRestart(
  client: RuntimeClient,
  { onRestarted, pollMs = 1000, timeoutMs = 90_000 }: RestartOptions = {},
): RestartState {
  const [canRestart, setCanRestart] = useState<boolean | null>(null);
  const [carrying, setCarrying] = useState(0);
  const [phase, setPhase] = useState<RestartPhase>("idle");
  const [problem, setProblem] = useState("");

  useEffect(() => {
    let current = true;
    void runtimeHealth(client)
      .then((health) => {
        if (!current) return;
        setCanRestart(health.can_restart ?? false);
        setCarrying(health.carrying ?? 0);
      })
      .catch(() => current && setCanRestart(false));
    return () => {
      current = false;
    };
  }, [client]);

  const restart = useCallback(async () => {
    setProblem("");
    setPhase("restarting");
    let before = "";
    try {
      before = (await requestRestart(client)).started_at;
    } catch (error) {
      const said = describe(error);
      setProblem(said);
      report(said);
      setPhase("failed");
      return;
    }
    const deadline = Date.now() + timeoutMs;
    while (Date.now() < deadline) {
      await wait(pollMs);
      try {
        const health = await runtimeHealth(client);
        if (health.started_at && health.started_at !== before) {
          (onRestarted ?? (() => window.location.reload()))();
          return;
        }
      } catch {
        // Not answering is the expected state for a second or two.
      }
    }
    setProblem("Prometheus did not come back. Start it again from the terminal or reopen the app.");
    setPhase("failed");
  }, [client, onRestarted, pollMs, timeoutMs]);

  return { canRestart, carrying, phase, problem, restart };
}
