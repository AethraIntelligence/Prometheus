import type { Workspace } from "../../../entities/workspace";
import type { RuntimeClient } from "../../../shared/api";

/**
 * The three things a person does to their contexts of work.
 *
 * Each is one call and no logic. In particular, switching does not touch work
 * that is already running: a run keeps the workspace it started in, which the
 * core decides and this window neither knows nor needs to.
 */

export async function useWorkspace(client: RuntimeClient, id: string): Promise<Workspace> {
  return client.post<Workspace>(`/api/workspaces/${id}/use`);
}

export async function addWorkspace(
  client: RuntimeClient,
  name: string,
  description = "",
): Promise<Workspace> {
  return client.post<Workspace>("/api/workspaces", { name, description });
}

export async function removeWorkspace(client: RuntimeClient, id: string): Promise<boolean> {
  const body = await client.del<{ removed: boolean }>(`/api/workspaces/${id}`);
  return body.removed;
}

/** Where its files go, and the folders offered to a thread. Each is checked by the core. */
export async function editWorkspace(
  client: RuntimeClient,
  id: string,
  change: { file_root?: string; folders?: string[] },
): Promise<Workspace> {
  return client.patch<Workspace>(`/api/workspaces/${id}`, change);
}
