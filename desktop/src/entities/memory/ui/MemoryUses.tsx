import type { MemoryUse } from "../model/types";
import { provenance } from "./MemoryLine";

const READER: Record<string, string> = {
  manager: "Prometheus, reading the request",
  task: "an employee, planning a task",
};

/**
 * Why each memory was put in front of the work. The reason is the core's,
 * derived from the record when the memory was recalled; this renders it.
 */
export function MemoryUses({ uses, empty }: { uses: MemoryUse[]; empty: string }) {
  if (uses.length === 0) return <p className="card-empty">{empty}</p>;
  return (
    <ul className="memory-uses">
      {uses.map((use, index) => (
        <li key={`${use.memory_id}:${use.task_id}:${index}`}>
          <p className="memory-used">
            {use.memory ? use.memory.content : "A memory that is not shown here"}
          </p>
          {use.memory && <p className="provenance">{provenance(use.memory)}</p>}
          <p className="note">
            Used by {READER[use.reader] ?? use.reader} · {use.used_at.slice(0, 16).replace("T", " ")}
          </p>
          <p className="memory-reason">{use.reason}</p>
        </li>
      ))}
    </ul>
  );
}
