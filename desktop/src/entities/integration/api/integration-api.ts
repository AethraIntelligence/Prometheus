import type { RuntimeClient } from "../../../shared/api";
import type { PluginList } from "../model/plugin";
import type { Integration, IntegrationList } from "../model/types";

export const integrationApi = {
  async all(client: RuntimeClient): Promise<IntegrationList> {
    return client.get<IntegrationList>("/api/integrations");
  },

  /** What can be installed, what is, and what this machine lacks - one read for the screen. */
  async plugins(client: RuntimeClient): Promise<PluginList> {
    return client.get<PluginList>("/api/plugins");
  },

  async one(client: RuntimeClient, id: string): Promise<Integration> {
    return client.get<Integration>(`/api/integrations/${id}`);
  },
};
