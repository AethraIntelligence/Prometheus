/**
 * The schedules page's state: the standing requests, and whether anything
 * fires them.
 *
 * Whether the scheduler is running comes from the runtime, and whether it will
 * be running after a restart comes from Settings -> General's own answer - so
 * the page can say "turned on, restart to start" without deciding either.
 */

import { useCallback, useEffect, useState } from "react";

import { providerApi, type ModelEntry } from "../../../entities/provider";
import { scheduleApi, type Schedule } from "../../../entities/schedule";
import { settingsApi } from "../../../entities/setting";
import { workflowApi, type Workflow, type WorkflowDryRun, type WorkflowRun } from "../../../entities/workflow";
import { changeSettings } from "../../../features/change-setting";
import {
  createSchedule,
  deleteSchedule,
  runScheduleNow,
  setScheduleEnabled,
  updateSchedule,
  type NewSchedule,
} from "../../../features/manage-schedules";
import { report, type RuntimeClient } from "../../../shared/api";
import { describe } from "../../../shared/lib";

const SWITCH = "flags.scheduler";

export interface SchedulerSwitch {
  /** Saved for the next start. */
  on: boolean;
  /** Set in the environment, and so not the window's to change. */
  lockedBy: string;
}

export interface SchedulesState {
  ready: boolean;
  available: boolean;
  running: boolean;
  switch: SchedulerSwitch | null;
  problem: string;
  schedules: Schedule[];
  /** Models a run may prefer: the catalog's, minus what cannot write text. */
  models: ModelEntry[];
  workflows: Workflow[];
  create: (schedule: NewSchedule) => Promise<boolean>;
  update: (id: string, schedule: NewSchedule) => Promise<boolean>;
  toggle: (schedule: Schedule) => Promise<void>;
  remove: (schedule: Schedule) => Promise<void>;
  runNow: (schedule: Schedule) => Promise<string | null>;
  turnOn: () => Promise<void>;
  dryRun: (workflow: Workflow, inputs?: Record<string, unknown>) => Promise<WorkflowDryRun | null>;
  runWorkflow: (workflow: Workflow, inputs?: Record<string, unknown>) => Promise<WorkflowRun | null>;
}

export function useSchedules(client: RuntimeClient): SchedulesState {
  const [ready, setReady] = useState(false);
  const [available, setAvailable] = useState(true);
  const [running, setRunning] = useState(false);
  const [switchState, setSwitch] = useState<SchedulerSwitch | null>(null);
  const [problem, setProblem] = useState("");
  const [schedules, setSchedules] = useState<Schedule[]>([]);
  const [models, setModels] = useState<ModelEntry[]>([]);
  const [workflows, setWorkflows] = useState<Workflow[]>([]);

  const fail = useCallback((error: unknown) => {
    const said = describe(error);
    setProblem(said);
    report(said);
  }, []);

  const reload = useCallback(async () => {
    try {
      const body = await scheduleApi.all(client);
      setAvailable(body.available ?? true);
      setRunning(body.running ?? false);
      setSchedules(body.schedules ?? []);
    } catch (error) {
      fail(error);
    }
    try {
      const catalog = await providerApi.all(client);
      setModels((catalog?.models ?? []).filter((entry) => entry.generates_text));
    } catch {
      // No catalog: the form offers the automatic choice only.
      setModels([]);
    }
    try {
      const catalog = await workflowApi.all(client);
      setWorkflows(catalog.workflows ?? []);
    } catch {
      setWorkflows([]);
    }
    try {
      const settings = await settingsApi.all(client);
      const found = settings.settings?.find((item) => item.key === SWITCH);
      setSwitch(found ? { on: found.value === true, lockedBy: found.locked_by } : null);
    } catch {
      // Without the settings answer the page still works; it only cannot
      // offer to turn the scheduler on, which it then does not.
      setSwitch(null);
    } finally {
      setReady(true);
    }
  }, [client, fail]);

  useEffect(() => {
    void reload();
  }, [reload]);

  const run = useCallback(
    async <T,>(action: () => Promise<T>): Promise<T | undefined> => {
      setProblem("");
      try {
        return await action();
      } catch (error) {
        fail(error);
        return undefined;
      } finally {
        await reload();
      }
    },
    [fail, reload],
  );

  return {
    ready,
    available,
    running,
    switch: switchState,
    problem,
    schedules,
    models,
    workflows,
    create: async (schedule) =>
      (await run(() => createSchedule(client, schedule))) !== undefined,
    update: async (id, schedule) =>
      (await run(() => updateSchedule(client, id, schedule))) !== undefined,
    toggle: async (schedule) => {
      await run(() => setScheduleEnabled(client, schedule.id, !schedule.enabled));
    },
    remove: async (schedule) => {
      await run(() => deleteSchedule(client, schedule.id));
    },
    runNow: async (schedule) =>
      (await run(() => runScheduleNow(client, schedule.id)))?.conversation_id ?? null,
    turnOn: async () => {
      await run(() => changeSettings(client, { [SWITCH]: true }));
    },
    dryRun: async (workflow, inputs = {}) =>
      (await run(() => workflowApi.dryRun(client, workflow.name, workflow.version, inputs))) ?? null,
    runWorkflow: async (workflow, inputs = {}) =>
      (await run(() => workflowApi.run(client, workflow.name, workflow.version, inputs))) ?? null,
  };
}
