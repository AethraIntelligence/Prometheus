/**
 * What contexts exist here, and which one this machine is in.
 *
 * Every operation ends by re-reading the runtime's answer rather than patching
 * a local copy: which workspace is active is a file the core owns, another
 * process may have changed it, and a window that trusted its own optimistic
 * update would be the one showing the wrong context's history.
 */

import { useCallback, useEffect, useState } from "react";

import { workspaceApi, type Workspace } from "../../../entities/workspace";
import {
  addWorkspace,
  editWorkspace,
  removeWorkspace,
  useWorkspace as switchTo,
} from "../../../features/switch-workspace";
import { report, type RuntimeClient } from "../../../shared/api";
import { describe } from "../../../shared/lib";

export interface WorkspacesState {
  ready: boolean;
  problem: string;
  workspaces: Workspace[];
  active?: Workspace;
  use: (id: string) => Promise<void>;
  add: (name: string, description: string) => Promise<void>;
  remove: (id: string) => Promise<void>;
  edit: (id: string, change: { file_root?: string; folders?: string[] }) => Promise<void>;
}

export function useWorkspaces(
  client: RuntimeClient,
  onSwitched?: () => void,
): WorkspacesState {
  const [ready, setReady] = useState(false);
  const [problem, setProblem] = useState("");
  const [workspaces, setWorkspaces] = useState<Workspace[]>([]);

  const reload = useCallback(async () => {
    try {
      const body = await workspaceApi.all(client);
      // Coalesced rather than trusted: an answer without the field is a
      // runtime older than this window, and a frame that throws over it takes
      // the whole page down with it. A missing list means none, not a crash.
      setWorkspaces(body.workspaces ?? []);
      setProblem("");
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
    async (action: () => Promise<unknown>, switched = false) => {
      try {
        await action();
        setProblem("");
        // Told rather than inferred: everything on screen belongs to a
        // workspace, and what to do about a switch is the frame's decision.
        if (switched) onSwitched?.();
      } catch (error) {
        const said = describe(error);
        setProblem(said);
        report(said);
      }
      await reload();
    },
    [onSwitched, reload],
  );

  return {
    ready,
    problem,
    workspaces,
    active: workspaces.find((workspace) => workspace.active),
    use: (id) => perform(() => switchTo(client, id), true),
    add: (name, description) => perform(() => addWorkspace(client, name, description)),
    remove: (id) => perform(() => removeWorkspace(client, id), true),
    edit: (id, change) => perform(() => editWorkspace(client, id, change)),
  };
}
