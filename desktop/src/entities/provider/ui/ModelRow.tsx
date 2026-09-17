/**
 * One catalog entry and the work that currently goes to it.
 *
 * `used_for` is rendered, never computed: which model a kind of work reaches is
 * the router's answer, and a second copy of that rule here would be wrong on
 * the first day somebody changed the real one.
 */

import type { ReactNode } from "react";

import type { ModelEntry } from "../model/types";

interface Props {
  entry: ModelEntry;
  actions?: ReactNode;
}

export function ModelRow({ entry, actions }: Props) {
  return (
    <article className="model" aria-label={`Model: ${entry.name}`}>
      <header>
        <h3>{entry.name}</h3>
        {entry.used_for.length > 0 && (
          <span className="state good">{entry.used_for.join(", ").toLowerCase()}</span>
        )}
      </header>
      <p className="meta">
        {entry.model}
        {entry.connection && ` · via ${entry.connection}`}
        {entry.capabilities.length > 0 && ` · ${entry.capabilities.join(", ").toLowerCase()}`}
      </p>
      {entry.tier && (
        <p className="meta" aria-label="Contract">
          {entry.tier.toLowerCase()}
          {entry.privacy === "LOCAL" ? " · stays on this machine" : " · prompts leave this machine"}
          {entry.latency_ms ? ` · ~${entry.latency_ms} ms` : ""}
          {` · $${entry.input_cost_per_1k_usd}/1k in, $${entry.output_cost_per_1k_usd}/1k out`}
        </p>
      )}
      {actions}
    </article>
  );
}
