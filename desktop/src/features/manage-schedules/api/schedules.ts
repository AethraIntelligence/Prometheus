/**
 * Making, pausing, running and removing a schedule.
 *
 * Whether a "when" means one thing, and which UTC moment a time of day is, are
 * the core's to decide; this sends what was chosen with the clock's offset and
 * shows what comes back.
 */

import type { Schedule } from "../../../entities/schedule";
import type { RuntimeClient } from "../../../shared/api";

export interface NewSchedule {
  request: string;
  name?: string;
  every_minutes?: number;
  /** HH:MM on the person's clock. */
  daily_at?: string;
  utc_offset_minutes?: number;
  /** IANA time zone so a daily time stays local across DST. */
  timezone?: string;
  on_event?: string;
  conversation_id?: string;
  /** A catalog entry its runs prefer. Empty: the router decides. */
  model?: string;
  /** ASK, AUTO or DENY. */
  approvals?: "ASK" | "AUTO" | "DENY";
  workflow_name?: string;
  workflow_version?: number;
  workflow_inputs?: Record<string, unknown>;
}

export async function createSchedule(client: RuntimeClient, schedule: NewSchedule): Promise<Schedule> {
  return client.post<Schedule>("/api/schedules", schedule);
}

/** The whole schedule as the form holds it; its thread and history stay. */
export async function updateSchedule(
  client: RuntimeClient,
  id: string,
  schedule: Omit<NewSchedule, "conversation_id">,
): Promise<Schedule> {
  return client.put<Schedule>(`/api/schedules/${id}`, schedule);
}

export async function setScheduleEnabled(
  client: RuntimeClient,
  id: string,
  enabled: boolean,
): Promise<Schedule> {
  return client.patch<Schedule>(`/api/schedules/${id}`, { enabled });
}

export async function deleteSchedule(client: RuntimeClient, id: string): Promise<void> {
  await client.del(`/api/schedules/${id}`);
}

export async function runScheduleNow(
  client: RuntimeClient,
  id: string,
): Promise<{ conversation_id: string | null }> {
  return client.post<{ conversation_id: string | null }>(`/api/schedules/${id}/run`);
}
