import { useState } from "react";

import { memoryApi, MemoryUses, type MemoryUsed } from "../../../entities/memory";
import { useRuntime } from "../../../shared/api";
import { describe } from "../../../shared/lib";
import { ChevronRight } from "../../../shared/ui";

/**
 * "Which memories did this answer use, and why" - asked when somebody wants to
 * know, not with every turn: most answers are read without it.
 */
export function MemoryUsedButton({ objectiveId }: { objectiveId: string }) {
  const client = useRuntime();
  const [open, setOpen] = useState(false);
  const [used, setUsed] = useState<MemoryUsed | null>(null);
  const [problem, setProblem] = useState("");

  const toggle = async () => {
    const next = !open;
    setOpen(next);
    if (!next || used) return;
    try {
      setUsed(await memoryApi.usedBy(client, objectiveId));
      setProblem("");
    } catch (error) {
      setProblem(describe(error));
    }
  };

  return (
    <div className="memory-used-by">
      <button type="button" className="worked" aria-expanded={open} onClick={() => void toggle()}>
        <ChevronRight className="cv" />
        Memory used
      </button>
      {open && problem && (
        <p className="problem" role="alert">
          {problem}
        </p>
      )}
      {open && used && (
        <MemoryUses
          uses={used.uses}
          empty={
            used.recorded
              ? "This answer was given no memories."
              : "This machine does not record which memories were used."
          }
        />
      )}
    </div>
  );
}
