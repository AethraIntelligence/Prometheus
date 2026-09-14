/**
 * What the runtime says can be installed, and what already is.
 *
 * Read and rendered, never derived. Whether a server can start on this machine
 * (`runtime_ready`), who a plugin should go to (`suggested_employees`), whether a
 * token is already kept (`stored`) and who may use a connected service
 * (`holders`) are all the core's answers, arriving as fields.
 */

import type { Integration } from "./types";

export interface PluginIcon {
  view_box: string;
  paths: string[];
  color: string;
  background: string;
}

export interface PluginSetting {
  key: string;
  label: string;
  /** SECRET is kept encrypted and never shown; PATH and TEXT are plain. */
  kind: "SECRET" | "PATH" | "TEXT";
  required: boolean;
  placeholder: string;
  help: string;
  help_url: string;
  /** A value is already kept under this name. Never the value. */
  stored: boolean;
  /** This installation supplies it; a person may leave it empty. */
  provided?: boolean;
}

export interface Plugin {
  id: string;
  name: string;
  description: string;
  about: string;
  category: string;
  publisher: string;
  homepage: string;
  popular: boolean;
  runtime: string;
  runtime_ready: boolean;
  icon: PluginIcon | null;
  capabilities: string[];
  settings: PluginSetting[];
  /** Things to do before installing, where a token alone is not enough. */
  setup: { text: string; url: string }[];
  /** Present where the server signs in through the browser after installing. */
  sign_in: { label: string; help: string } | null;
  suggested_employees: string[];
  /** The integration id once installed, empty before. */
  installed: string;
  status: string;
}

export interface InstalledIntegration extends Integration {
  /** The plugin it came from, or empty for a server added by hand. */
  plugin: string;
  /** Everyone who may use it, however they were granted it. */
  holders: string[];
  /** The part of that a person chose here, and can change here. */
  granted_to: string[];
}

export interface RuntimeState {
  ready: boolean;
  hint: string;
  url: string;
}

export interface PluginList {
  available: boolean;
  runtimes: Record<string, RuntimeState>;
  plugins: Plugin[];
  installed: InstalledIntegration[];
}
