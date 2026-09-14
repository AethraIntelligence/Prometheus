import type { RuntimeClient } from "../../../shared/api";
import type { MemoryList } from "../model/types";

export const memoryApi = {
  /** Everything shown here, or - with words - what the core's recall ranks for them. */
  async all(client: RuntimeClient, search = ""): Promise<MemoryList> {
    const words = search.trim();
    return client.get<MemoryList>(
      words ? `/api/memory?q=${encodeURIComponent(words)}` : "/api/memory",
    );
  },
};
