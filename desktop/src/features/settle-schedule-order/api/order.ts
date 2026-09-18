/**
 * The order a schedule settled into: what was noticed, keeping it, and undoing that.
 *
 * Nothing here names a process or a version. A person watching a schedule is
 * not choosing a declaration, and the core decides what the confirmed order is
 * called and which version it becomes - so the only thing this sends is which
 * of that schedule's own suggestions was confirmed.
 */

import type { Schedule } from "../../../entities/schedule";
import type { RuntimeClient } from "../../../shared/api";

export interface SettledOrder {
  id: string;
  occurrences: number;
  first_seen: string;
  last_seen: string;
  steps: { name: string; employee: string }[];
}

export interface OfferedOrders {
  available: boolean;
  suggestions: SettledOrder[];
}

export const offeredOrders = (client: RuntimeClient, scheduleId: string) =>
  client.get<OfferedOrders>(`/api/schedules/${scheduleId}/suggestions`);

export const keepOrder = (client: RuntimeClient, scheduleId: string, suggestionId: string) =>
  client.post<Schedule>(`/api/schedules/${scheduleId}/order`, { suggestion_id: suggestionId });

export const dropOrder = (client: RuntimeClient, scheduleId: string) =>
  client.del<Schedule>(`/api/schedules/${scheduleId}/order`);

/** Not now: the same offer returns only when new successful runs have happened. */
export const declineOrder = (client: RuntimeClient, suggestionId: string) =>
  client.post(`/api/workflow-suggestions/${suggestionId}/dismiss`);
