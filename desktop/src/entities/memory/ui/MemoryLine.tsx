import type { ReactNode } from "react";

import type { MemoryItem } from "../model/types";

/** One remembered thing, with what it is true of, who wrote it and when. */
export function MemoryLine({ item, actions }: { item: MemoryItem; actions?: ReactNode }) {
  return (
    <li className="memory">
      <span className="badge quiet">{item.scope === "USER" ? "you" : "here"}</span>
      <span>{item.content}</span>
      <span className="note">
        {item.stated ? "noted by you · " : ""}
        {item.created_at.slice(0, 10)}
      </span>
      {actions}
    </li>
  );
}
