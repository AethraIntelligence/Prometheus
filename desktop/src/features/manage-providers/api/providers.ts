/**
 * Adding a provider, a key and a model, and saying where work goes.
 *
 * Each of these is one call to the core and no decision here. In particular the
 * window never checks whether a connection can be removed - the core refuses
 * one that models depend on and says which, and re-deriving that rule here
 * would be a second copy of it that disagrees the first time somebody adds a
 * third dependency.
 */

import type { Connection, ModelEntry, ProviderSettings } from "../../../entities/provider";
import type { RuntimeClient } from "../../../shared/api";

export interface NewConnection {
  name: string;
  kind: string;
  /** Goes in and is never read back: no endpoint returns a key. */
  api_key?: string;
  base_url?: string;
  description?: string;
}

export interface NewModel {
  name: string;
  provider: string;
  model: string;
  connection?: string;
  capabilities?: string[];
  context_tokens?: number;
  quality?: number;
}

export async function addConnection(
  client: RuntimeClient,
  connection: NewConnection,
): Promise<Connection> {
  return client.post<Connection>("/api/providers/connections", connection);
}

export async function replaceKey(
  client: RuntimeClient,
  name: string,
  apiKey: string,
): Promise<Connection> {
  return client.put<Connection>(
    `/api/providers/connections/${encodeURIComponent(name)}/key`,
    { api_key: apiKey },
  );
}

export async function removeConnection(client: RuntimeClient, name: string): Promise<void> {
  await client.del(`/api/providers/connections/${encodeURIComponent(name)}`);
}

export async function addModel(client: RuntimeClient, entry: NewModel): Promise<ModelEntry> {
  return client.post<ModelEntry>("/api/providers/models", entry);
}

export async function removeModel(client: RuntimeClient, name: string): Promise<void> {
  await client.del(`/api/providers/models/${encodeURIComponent(name)}`);
}

export async function sendWorkTo(
  client: RuntimeClient,
  taskKind: string,
  entryName: string,
): Promise<ProviderSettings["defaults"]> {
  const body = await client.put<{ defaults: Record<string, string> }>(
    "/api/providers/defaults",
    { task_kind: taskKind, entry_name: entryName },
  );
  return body.defaults;
}

/**
 * Stop sending a kind of work anywhere in particular.
 *
 * Not the same as sending it to nothing: without a default the router ranks the
 * candidates itself, which is what a fresh installation does and a perfectly
 * good answer.
 */
export async function clearWorkRouting(
  client: RuntimeClient,
  taskKind: string,
): Promise<ProviderSettings["defaults"]> {
  const body = await client.del<{ defaults: Record<string, string> }>(
    `/api/providers/defaults/${encodeURIComponent(taskKind)}`,
  );
  return body.defaults;
}

/**
 * Add a recommended setup's models and send work to them, in one request.
 *
 * The core picks the connection (the first of the setup's kind), reuses what
 * is already in the catalog and never overwrites an entry of the same name.
 */
export async function applySetup(
  client: RuntimeClient,
  setupId: string,
  connection = "",
): Promise<{ added: string[] }> {
  return client.post<{ added: string[] }>(
    `/api/providers/setups/${encodeURIComponent(setupId)}/apply`,
    { connection },
  );
}
