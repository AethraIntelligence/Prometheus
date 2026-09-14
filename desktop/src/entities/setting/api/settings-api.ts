import type { RuntimeClient } from "../../../shared/api";
import type { SettingsList } from "../model/types";

export const settingsApi = {
  /** Every switch this installation lets a window change, in the order to read them. */
  async all(client: RuntimeClient): Promise<SettingsList> {
    return client.get<SettingsList>("/api/settings");
  },
};
