import type { RuntimeClient } from "../../../shared/api";

/** The runtime's stop record, as it reports it. Never derived here. */
export interface StopState {
  engaged: boolean;
  reason?: string;
  engaged_by?: string;
  engaged_at?: string | null;
  unreadable?: boolean;
  available?: boolean;
}

export interface StopReport {
  state: StopState;
  objectives_cancelled: number;
  tasks_signalled: number;
  tasks_closed: number;
  approvals_released: number;
  leases_revoked: number;
  failures: string[];
  completed_effects_undone: false;
}

export async function stopState(client: RuntimeClient): Promise<StopState> {
  return client.get<StopState>("/api/runtime/stop");
}

export async function emergencyStop(client: RuntimeClient, reason = ""): Promise<StopReport> {
  return client.post<StopReport>("/api/runtime/stop", { reason });
}

export async function resumeWork(client: RuntimeClient): Promise<StopReport> {
  return client.post<StopReport>("/api/runtime/resume");
}
