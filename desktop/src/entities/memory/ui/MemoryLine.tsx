import type { ReactNode } from "react";

import type { MemoryItem } from "../model/types";

const BASIS: Record<string, string> = {
  STATED: "stated",
  OBSERVED: "recorded",
  REPORTED: "reported",
  INFERRED: "assumption",
};

const SOURCE: Record<string, string> = {
  PERSON: "your note",
  OBJECTIVE: "a request",
  TASK: "a task",
  CONSOLIDATION: "a summary",
  UNKNOWN: "unrecorded source",
};

/** What a memory rests on, in one line: basis, source, confidence, status. */
export function provenance(item: MemoryItem): string {
  const parts: string[] = [];
  if (item.basis) parts.push(BASIS[item.basis] ?? item.basis.toLowerCase());
  if (item.source) {
    const from = SOURCE[item.source.kind] ?? item.source.kind.toLowerCase();
    parts.push(item.source.label ? `${from}: ${item.source.label}` : from);
  }
  if (typeof item.confidence === "number") {
    parts.push(`confidence ${Math.round(item.confidence * 100)}%`);
  }
  if (item.status === "SUPERSEDED") parts.push("replaced");
  if (item.status === "CONTESTED") parts.push("disputed");
  if (item.expires_at) parts.push(`kept until ${item.expires_at.slice(0, 10)}`);
  return parts.join(" · ");
}

/** One remembered thing, with what it is true of, what it rests on and when. */
export function MemoryLine({ item, actions }: { item: MemoryItem; actions?: ReactNode }) {
  const status = (item.status ?? "ACTIVE").toLowerCase();
  return (
    <li className={`memory ${status}`}>
      <span className="badge quiet">{item.scope === "USER" ? "you" : "here"}</span>
      <div className="memory-body">
        <span className={item.factual === false ? "memory-claim" : undefined}>{item.content}</span>
        {item.basis && <span className="provenance">{provenance(item)}</span>}
      </div>
      <span className="note">
        {item.stated ? "noted by you · " : ""}
        {item.created_at.slice(0, 10)}
      </span>
      {actions}
    </li>
  );
}
