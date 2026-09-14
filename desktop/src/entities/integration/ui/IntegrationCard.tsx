/**
 * One connected service, shown as it is.
 *
 * The status word comes from the runtime and is rendered, never derived: this
 * layer has no idea what "READY" implies beyond how to spell it, which is what
 * keeps a lifecycle from being reimplemented in a component. Actions arrive
 * from a feature for the same reason the approval card's do.
 */

import type { ReactNode } from "react";

import type { Integration } from "../model/types";

interface Props {
  integration: Integration;
  actions?: ReactNode;
  children?: ReactNode;
}

/** Purely presentational: which of the runtime's words reads as trouble. */
const UNHAPPY = ["CONNECTION_FAILED", "AUTHENTICATION_REQUIRED", "CONFIGURATION_INVALID", "UNAVAILABLE"];

export function IntegrationCard({ integration, actions, children }: Props) {
  const tone = !integration.enabled
    ? "off"
    : UNHAPPY.includes(integration.status)
      ? "bad"
      : integration.status === "READY"
        ? "good"
        : "waiting";
  return (
    <article className="integration" aria-label={`Integration: ${integration.name}`}>
      <header>
        <h3>{integration.name}</h3>
        <span className={`state ${tone}`}>{integration.status.replace(/_/g, " ").toLowerCase()}</span>
      </header>
      <p className="meta">
        {integration.kind} · {integration.tool_count} capabilit
        {integration.tool_count === 1 ? "y" : "ies"}
        {integration.capabilities.length > 0 && ` · offers ${integration.capabilities.join(", ")}`}
      </p>
      {integration.secrets.length > 0 && (
        <p className="meta">needs {integration.secrets.join(", ")}</p>
      )}
      {children}
      {actions && <footer>{actions}</footer>}
    </article>
  );
}
