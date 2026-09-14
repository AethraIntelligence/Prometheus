import type { SettingValue, SettingsList } from "../../../entities/setting";
import type { RuntimeClient } from "../../../shared/api";

/**
 * Save some switches for the next start.
 *
 * Whether a value is acceptable is the runtime's question - it validates
 * against the setting's own type and refuses the whole change with a sentence -
 * so this sends what was chosen and hands back what the runtime now holds.
 */
export async function changeSettings(
  client: RuntimeClient,
  values: Record<string, SettingValue>,
): Promise<SettingsList> {
  return client.put<SettingsList>("/api/settings", { values });
}

/** Forget what was saved here for these keys, or for every one. */
export async function resetSettings(
  client: RuntimeClient,
  keys: string[] | null = null,
): Promise<SettingsList> {
  return client.post<SettingsList>("/api/settings/reset", { keys });
}
