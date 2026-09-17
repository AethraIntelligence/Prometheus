import type { RuntimeClient } from "../../../shared/api";
import type { WorkItem } from "../model/types";

export const workApi = {
  async all(client: RuntimeClient): Promise<WorkItem[]> {
    const body = await client.get<{ items: WorkItem[] }>("/api/work");
    return body.items;
  },
  get: (client: RuntimeClient, id: string) => client.get<WorkItem>(`/api/work/${id}`),
};
