/**
 * What the runtime says it remembers.
 *
 * A person can search it, add a note of their own and forget one line. What
 * can be forgotten is decided by the core - exactly what the listing shows -
 * and `can_forget` says whether this machine offers forgetting at all; the
 * window renders both and derives neither.
 */

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
}

export interface MemoryList {
  available?: boolean;
  can_forget?: boolean;
  items: MemoryItem[];
}
