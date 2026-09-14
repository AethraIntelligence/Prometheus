import type { ReactNode } from "react";

import type { Plugin } from "../model/plugin";
import { PluginMark } from "./PluginMark";

/** One installable service: its mark, what it is, and whatever can be done with it. */
export function PluginRow({
  plugin,
  onOpen,
  action,
}: {
  plugin: Plugin;
  onOpen: () => void;
  action?: ReactNode;
}) {
  return (
    <li className="plugin-row">
      <button type="button" className="plugin-open" onClick={onOpen} aria-label={plugin.name}>
        <PluginMark name={plugin.name} icon={plugin.icon} />
        <span className="plugin-text">
          <strong>{plugin.name}</strong>
          <span>{plugin.description}</span>
        </span>
      </button>
      {action}
    </li>
  );
}
