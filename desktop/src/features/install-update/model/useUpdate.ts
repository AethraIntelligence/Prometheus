/**
 * Finding a signed update and installing it at a safe point.
 *
 * The order is the contract. The shell finds and later verifies the package;
 * before it is installed, the runtime is asked to hold new effects and to wait
 * for the ones in flight (`prepare-update`). Only a runtime that says it is
 * ready gets replaced. One that is not - a message still sending, a file still
 * being deleted - is waited for, or the person may pull the emergency stop;
 * the window never decides that an effect is safe to cut.
 *
 * A download that fails verification throws inside the shell before anything is
 * replaced; the hold is lifted and the running version carries on.
 */

import { useCallback, useState } from "react";

import {
  checkForUpdate,
  report,
  restartApplication,
  type AvailableUpdate,
  type RuntimeClient,
} from "../../../shared/api";
import { describe } from "../../../shared/lib";
import { cancelUpdate, prepareUpdate, type EffectInFlight } from "../api/readiness";

export type UpdatePhase =
  | "idle"
  | "checking"
  | "current"
  | "unsupported"
  | "available"
  | "waiting"
  | "installing"
  | "failed";

export interface UpdateState {
  phase: UpdatePhase;
  update: AvailableUpdate | null;
  note: string;
  inFlight: EffectInFlight[];
  progress: number | null;
  check: () => Promise<void>;
  install: () => Promise<void>;
  cancel: () => Promise<void>;
}

export function useUpdate(
  client: RuntimeClient,
  {
    find = checkForUpdate,
    restart = restartApplication,
    waitSeconds = 30,
  }: {
    find?: typeof checkForUpdate;
    restart?: () => Promise<void>;
    waitSeconds?: number;
  } = {},
): UpdateState {
  const [phase, setPhase] = useState<UpdatePhase>("idle");
  const [update, setUpdate] = useState<AvailableUpdate | null>(null);
  const [note, setNote] = useState("");
  const [inFlight, setInFlight] = useState<EffectInFlight[]>([]);
  const [progress, setProgress] = useState<number | null>(null);

  const failed = useCallback((error: unknown) => {
    const said = describe(error);
    setNote(said);
    report(said);
    setPhase("failed");
  }, []);

  const check = useCallback(async () => {
    setPhase("checking");
    setNote("");
    try {
      const found = await find();
      if (found.kind === "unsupported") {
        setNote(found.reason);
        setPhase("unsupported");
      } else if (found.kind === "current") {
        setPhase("current");
      } else {
        setUpdate(found.update);
        setPhase("available");
      }
    } catch (error) {
      failed(error);
    }
  }, [failed, find]);

  const install = useCallback(async () => {
    if (!update) return;
    setNote("");
    setPhase("waiting");
    try {
      const readiness = await prepareUpdate(client, waitSeconds);
      if (!readiness.ready) {
        setInFlight(readiness.in_flight);
        setPhase("available");
        setNote(
          "Prometheus is in the middle of an action that cannot be interrupted safely. " +
            "Try again when it finishes, or stop all work first.",
        );
        return;
      }
    } catch (error) {
      failed(error);
      return;
    }
    setInFlight([]);
    setPhase("installing");
    try {
      await update.install((downloaded, total) =>
        setProgress(total ? Math.round((downloaded / total) * 100) : null),
      );
    } catch (error) {
      // Nothing was replaced. Let work continue on the version that runs.
      await cancelUpdate(client).catch(() => undefined);
      failed(error);
      return;
    }
    await restart();
  }, [client, failed, restart, update, waitSeconds]);

  const cancel = useCallback(async () => {
    await cancelUpdate(client).catch(() => undefined);
    setPhase(update ? "available" : "idle");
  }, [client, update]);

  return { phase, update, note, inFlight, progress, check, install, cancel };
}
