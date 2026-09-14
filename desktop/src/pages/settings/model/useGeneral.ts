/**
 * Settings -> General: the switches this installation starts with.
 *
 * Every change goes to the runtime and the listing is replaced with what it
 * answered, including after a refusal, so what is shown is what is saved rather
 * than what the window hoped.
 */

import { useCallback, useEffect, useState } from "react";

import {
  settingsApi,
  type Setting,
  type SettingValue,
  type SettingsList,
} from "../../../entities/setting";
import { changeSettings, resetSettings } from "../../../features/change-setting";
import { report, type RuntimeClient } from "../../../shared/api";
import { describe } from "../../../shared/lib";

export interface GeneralState {
  ready: boolean;
  available: boolean;
  restartNeeded: boolean;
  anySaved: boolean;
  problem: string;
  settings: Setting[];
  change: (key: string, value: SettingValue) => Promise<void>;
  /** One key, or every one when none is named. */
  reset: (key?: string) => Promise<void>;
}

export function useGeneral(client: RuntimeClient): GeneralState {
  const [ready, setReady] = useState(false);
  const [available, setAvailable] = useState(true);
  const [restartNeeded, setRestartNeeded] = useState(false);
  const [anySaved, setAnySaved] = useState(false);
  const [problem, setProblem] = useState("");
  const [settings, setSettings] = useState<Setting[]>([]);

  const show = (body: SettingsList) => {
    setRestartNeeded(body.restart_needed ?? false);
    setAnySaved(body.any_saved ?? false);
    setSettings(body.settings ?? []);
  };

  const reload = useCallback(async () => {
    try {
      const body = await settingsApi.all(client);
      setAvailable(body.available ?? true);
      show(body);
    } catch (error) {
      const said = describe(error);
      setProblem(said);
      report(said);
    } finally {
      setReady(true);
    }
  }, [client]);

  useEffect(() => {
    void reload();
  }, [reload]);

  const perform = useCallback(
    async (action: () => Promise<SettingsList>) => {
      try {
        show(await action());
        setProblem("");
      } catch (error) {
        const said = describe(error);
        setProblem(said);
        report(said);
        await reload();
      }
    },
    [reload],
  );

  const change = useCallback(
    (key: string, value: SettingValue) => perform(() => changeSettings(client, { [key]: value })),
    [client, perform],
  );

  const reset = useCallback(
    (key?: string) => perform(() => resetSettings(client, key ? [key] : null)),
    [client, perform],
  );

  return { ready, available, restartNeeded, anySaved, problem, settings, change, reset };
}
