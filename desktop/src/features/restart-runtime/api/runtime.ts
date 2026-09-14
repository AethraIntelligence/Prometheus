import type { RuntimeClient } from "../../../shared/api";

export interface RuntimeHealth {
  status: string;
  /** When this process started; a new value means a new process. */
  started_at?: string;
  /** Whether it can restart itself - only `prometheus serve` can. */
  can_restart?: boolean;
  /** Runs a restart would stop. */
  carrying?: number;
}

export async function runtimeHealth(client: RuntimeClient): Promise<RuntimeHealth> {
  return client.get<RuntimeHealth>("/api/health");
}

export async function requestRestart(
  client: RuntimeClient,
): Promise<{ restarting: boolean; stopping: number; started_at: string }> {
  return client.post("/api/runtime/restart");
}
