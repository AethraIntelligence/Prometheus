import type { RuntimeClient } from "../../../shared/api";

export const dismissSuggestion = (client: RuntimeClient, id: string) =>
  client.post(`/api/workflow-suggestions/${id}/dismiss`);

export const snoozeSuggestion = (client: RuntimeClient, id: string, days: number) =>
  client.post(`/api/workflow-suggestions/${id}/snooze`, { days });

export const saveSuggestion = (
  client: RuntimeClient,
  id: string,
  name: string,
  description: string,
) =>
  client.post<{ workflow: string; file: string }>(`/api/workflow-suggestions/${id}/save`, {
    name,
    description,
  });
