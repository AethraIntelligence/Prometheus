import type { WorkItem } from "../../../entities/work-item";
import type { RuntimeClient } from "../../../shared/api";

export const pauseWork = (client: RuntimeClient, id: string) =>
  client.post<WorkItem>(`/api/work/${id}/pause`);

export const resumeWork = (client: RuntimeClient, id: string) =>
  client.post<WorkItem>(`/api/work/${id}/resume`);

export const cancelWork = (client: RuntimeClient, id: string) =>
  client.post(`/api/objectives/${id}/cancel`);

export const retryWork = (client: RuntimeClient, id: string) =>
  client.post<WorkItem>(`/api/work/${id}/retry`);

export const retryTask = (client: RuntimeClient, id: string) =>
  client.post(`/api/tasks/${id}/retry`);

export const handoffTask = (client: RuntimeClient, id: string, employee: string) =>
  client.post(`/api/tasks/${id}/handoff`, { employee });
