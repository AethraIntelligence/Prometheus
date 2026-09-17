import type { RuntimeClient } from "../../../shared/api";
import type { MemoryList, MemoryTrace, MemoryUsed } from "../model/types";

export const memoryApi = {
  /**
   * Everything shown here, or - with words - what the core's recall ranks for
   * them. `superseded` also lists what corrections replaced.
   */
  async all(client: RuntimeClient, search = "", superseded = false): Promise<MemoryList> {
    const params = new URLSearchParams();
    const words = search.trim();
    if (words) params.set("q", words);
    if (superseded) params.set("superseded", "true");
    const query = params.toString();
    return client.get<MemoryList>(query ? `/api/memory?${query}` : "/api/memory");
  },

  /** One memory traced: where it came from, what replaced it, where it was used. */
  async trace(client: RuntimeClient, id: string): Promise<MemoryTrace> {
    return client.get<MemoryTrace>(`/api/memory/${id}`);
  },

  /** Which memories one answer was given, and why each one. */
  async usedBy(client: RuntimeClient, objectiveId: string): Promise<MemoryUsed> {
    return client.get<MemoryUsed>(`/api/objectives/${objectiveId}/memory`);
  },
};
