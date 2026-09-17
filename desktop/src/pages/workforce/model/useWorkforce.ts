import { useCallback, useEffect, useState } from "react";

import {
  employeeApi,
  type EmployeeProfile,
  type Workforce,
} from "../../../entities/employee";
import { workflowApi, type WorkflowSuggestionList } from "../../../entities/workflow";
import {
  dismissSuggestion,
  saveSuggestion,
  snoozeSuggestion,
} from "../../../features/review-workflow-suggestion";
import { report, type RuntimeClient } from "../../../shared/api";
import { describe } from "../../../shared/lib";

const EMPTY: Workforce = { available: true, employees: [], overlaps: [] };

/**
 * The workforce, one profile, and the drafts on offer - as the runtime states them.
 *
 * Nothing is computed here: readiness, outcomes and rates arrive decided, and a
 * missing number arrives with the reason it is missing. The hook only loads,
 * reloads after an action, and says when a request failed.
 */
export function useWorkforce(client: RuntimeClient) {
  const [workforce, setWorkforce] = useState<Workforce>(EMPTY);
  const [suggestions, setSuggestions] = useState<WorkflowSuggestionList>({
    available: true,
    suggestions: [],
  });
  const [selected, setSelected] = useState<string | null>(null);
  const [windowDays, setWindowDays] = useState(30);
  const [profile, setProfile] = useState<EmployeeProfile | null>(null);
  const [ready, setReady] = useState(false);
  const [busy, setBusy] = useState(false);
  const [problem, setProblem] = useState("");
  const [notice, setNotice] = useState("");

  const fail = useCallback((error: unknown) => {
    const message = describe(error);
    setProblem(message);
    void report(message);
  }, []);

  const reload = useCallback(async (clearProblem = true) => {
    try {
      const [roster, drafts] = await Promise.all([
        employeeApi.workforce(client),
        workflowApi.suggestions(client),
      ]);
      setWorkforce(roster);
      setSuggestions(drafts);
      setSelected((current) =>
        current && roster.employees.some((employee) => employee.name === current)
          ? current
          : roster.employees[0]?.name ?? null,
      );
      if (clearProblem) setProblem("");
    } catch (error) {
      fail(error);
    } finally {
      setReady(true);
    }
  }, [client, fail]);

  useEffect(() => {
    void reload();
    const timer = window.setInterval(() => void reload(), 5000);
    return () => window.clearInterval(timer);
  }, [reload]);

  useEffect(() => {
    if (!selected) {
      setProfile(null);
      return;
    }
    let live = true;
    employeeApi
      .profile(client, selected, windowDays)
      .then((found) => live && setProfile(found))
      .catch((error) => live && fail(error));
    return () => {
      live = false;
    };
  }, [client, selected, windowDays, workforce, fail]);

  const act = useCallback(
    async (action: () => Promise<unknown>, done = "") => {
      setBusy(true);
      setProblem("");
      try {
        await action();
        setNotice(done);
        return true;
      } catch (error) {
        fail(error);
        return false;
      } finally {
        await reload(false);
        setBusy(false);
      }
    },
    [fail, reload],
  );

  return {
    workforce,
    suggestions,
    selected,
    select: setSelected,
    windowDays,
    setWindowDays,
    profile,
    ready,
    busy,
    problem,
    notice,
    dismiss: (id: string) => act(() => dismissSuggestion(client, id), "Suggestion dismissed."),
    snooze: (id: string, days: number) =>
      act(() => snoozeSuggestion(client, id, days), `Suggestion snoozed for ${days} days.`),
    save: (id: string, name: string, description: string) =>
      act(() => saveSuggestion(client, id, name, description), `Saved workflow "${name}".`),
  };
}
