/**
 * One switch in Settings -> General, as the runtime describes it.
 *
 * Two values because the runtime reads these when it starts: `value` is what
 * the next start gets, `running` is what the engine behind this window has now.
 * Whether they differ is `restart_needed`, said by the runtime rather than
 * compared here.
 */

export type SettingKind = "BOOLEAN" | "CHOICE" | "INTEGER" | "NUMBER" | "TEXT" | "LIST";

export type SettingValue = boolean | number | string | string[] | null;

export interface Setting {
  key: string;
  group: string;
  label: string;
  help: string;
  kind: SettingKind;
  value: SettingValue;
  running: SettingValue;
  /** What it falls back to when nothing is saved here: `.env`, or the platform's default. */
  default: SettingValue;
  /** Saved from this window, and so something a reset would forget. */
  saved: boolean;
  choices: string[];
  minimum: number | null;
  optional: boolean;
  /** The environment variable that decides this value; the window cannot change it. */
  locked_by: string;
  restart_needed: boolean;
}

export interface SettingsList {
  available?: boolean;
  restart_needed?: boolean;
  any_saved?: boolean;
  settings?: Setting[];
}
