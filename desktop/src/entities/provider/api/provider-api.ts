import type { RuntimeClient } from "../../../shared/api";
import type { InstalledModels, ProviderSettings } from "../model/types";

export const providerApi = {
  /** Everything the settings screen shows, in one request. */
  async all(client: RuntimeClient): Promise<ProviderSettings> {
    return client.get<ProviderSettings>("/api/providers");
  },

  /** What a runner already has, and whether it answered or only its disk did. */
  async installed(client: RuntimeClient, connection: string): Promise<InstalledModels> {
    const body = await client.get<Partial<InstalledModels>>(
      `/api/providers/connections/${encodeURIComponent(connection)}/installed`,
    );
    // Filled out rather than trusted whole, as the settings body is: a runtime
    // from before the fields existed answers with the list alone.
    const models = body.models ?? [];
    return {
      models,
      supported: body.supported ?? models.length > 0,
      reachable: body.reachable ?? models.length > 0,
      from_disk: body.from_disk ?? false,
      runner: body.runner ?? "",
      address: body.address ?? "",
    };
  },
};
