import { useCallback, useEffect, useState } from "react";

import { employeeApi, type Employee } from "../../../entities/employee";
import { workApi, type WorkItem } from "../../../entities/work-item";
import {
  cancelWork,
  handoffTask,
  pauseWork,
  resumeWork,
  retryTask,
  retryWork,
} from "../../../features/control-work";
import { report, type RuntimeClient } from "../../../shared/api";
import { describe } from "../../../shared/lib";

export function useWorkCenter(client: RuntimeClient) {
  const [items, setItems] = useState<WorkItem[]>([]);
  const [employees, setEmployees] = useState<Employee[]>([]);
  const [ready, setReady] = useState(false);
  const [problem, setProblem] = useState("");
  const [busy, setBusy] = useState(false);

  const reload = useCallback(async () => {
    try {
      const [work, workforce] = await Promise.all([workApi.all(client), employeeApi.all(client)]);
      setItems(work);
      setEmployees(workforce);
      setProblem("");
    } catch (error) {
      const message = describe(error);
      setProblem(message);
      void report(message);
    } finally {
      setReady(true);
    }
  }, [client]);

  useEffect(() => {
    void reload();
    const timer = window.setInterval(() => void reload(), 3000);
    return () => window.clearInterval(timer);
  }, [reload]);

  const run = useCallback(
    async (action: () => Promise<unknown>) => {
      setBusy(true);
      setProblem("");
      try {
        await action();
      } catch (error) {
        const message = describe(error);
        setProblem(message);
        void report(message);
      } finally {
        await reload();
        setBusy(false);
      }
    },
    [reload],
  );

  return {
    items,
    employees,
    ready,
    problem,
    busy,
    pause: (id: string) => run(() => pauseWork(client, id)),
    resume: (id: string) => run(() => resumeWork(client, id)),
    cancel: (id: string) => run(() => cancelWork(client, id)),
    retry: (id: string) => run(() => retryWork(client, id)),
    retryTask: (id: string) => run(() => retryTask(client, id)),
    handoff: (id: string, employee: string) => run(() => handoffTask(client, id, employee)),
  };
}
