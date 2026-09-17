/**
 * What the runtime says it remembers, and what each line rests on.
 *
 * A person can search it, add a note of their own, correct a line, decide how
 * long to keep it and forget it. What can be forgotten is decided by the core -
 * exactly what the listing shows - and `can_forget` says whether this machine
 * offers forgetting at all; the window renders both and derives neither.
 *
 * Since Phase 9 each line also carries its provenance: the basis (stated,
 * recorded, reported, inferred), the source it came from, a confidence the
 * writer set, and whether it is still current. All of it is rendered as sent.
 */

export type MemoryBasis = "STATED" | "OBSERVED" | "REPORTED" | "INFERRED";
export type MemoryStatus = "ACTIVE" | "SUPERSEDED" | "CONTESTED";

export interface MemorySource {
  /** PERSON, OBJECTIVE, TASK, CONSOLIDATION or UNKNOWN. */
  kind: string;
  ref: string;
  label: string;
  derived_from: string[];
}

export interface MemoryItem {
  id: string;
  kind: string;
  /** WORKSPACE is true of this context; USER is true of the person. */
  scope: string;
  content: string;
  importance: number;
  created_at: string;
  expires_at: string;
  /** Added by a person rather than written by the platform about its own work. */
  stated: boolean;
  basis?: MemoryBasis;
  /** Stated or recorded, rather than somebody's account or a model's guess. */
  factual?: boolean;
  confidence?: number;
  status?: MemoryStatus;
  superseded_by?: string;
  revised_at?: string;
  contradicts?: string[];
  source?: MemorySource;
}

export interface MemoryList {
  available?: boolean;
  can_forget?: boolean;
  items: MemoryItem[];
}

/** One recorded use: who read the memory, and the reason the core gave. */
export interface MemoryUse {
  memory_id: string;
  reader: string;
  reason: string;
  weight: number;
  objective_id: string;
  task_id: string;
  used_at: string;
  /** Null where the memory is not shown here: forgotten, or an employee's own note. */
  memory: MemoryItem | null;
}

export interface MemoryTrace {
  item: MemoryItem;
  superseded_by: MemoryItem | null;
  contradicts: MemoryItem[];
  derived_from: MemoryItem[];
  uses: MemoryUse[];
}

export interface MemoryUsed {
  objective_id: string;
  recorded: boolean;
  uses: MemoryUse[];
}
