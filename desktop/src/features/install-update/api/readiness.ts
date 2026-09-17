import type { RuntimeClient } from "../../../shared/api";

export interface EffectInFlight {
  task_id: string;
  tool: string;
  effect: string;
  since: string;
}

export interface UpdateReadiness {
  safe: boolean;
  held: boolean;
  in_flight: EffectInFlight[];
  carrying: number;
  resumes_after_restart: boolean;
  ready?: boolean;
}

export async function updateReadiness(client: RuntimeClient): Promise<UpdateReadiness> {
  return client.get<UpdateReadiness>("/api/runtime/update-readiness");
}

export async function prepareUpdate(
  client: RuntimeClient,
  timeoutSeconds = 30,
): Promise<UpdateReadiness> {
  return client.post<UpdateReadiness>("/api/runtime/prepare-update", {
    timeout_seconds: timeoutSeconds,
  });
}

export async function cancelUpdate(client: RuntimeClient): Promise<UpdateReadiness> {
  return client.del<UpdateReadiness>("/api/runtime/prepare-update");
}
