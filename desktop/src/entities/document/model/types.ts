/**
 * What the runtime says about a document somebody brought.
 *
 * `searchable` arrives decided. A document that has text but no vectors is
 * found by its words and not by its meaning, and which statuses count as
 * searchable is the core's answer - derived here, it would be a second copy of
 * a rule that can change.
 */

export interface Document {
  id: string;
  title: string;
  source: string;
  media_type: string;
  status: string;
  searchable: boolean;
  /** Decided by the core: text without vectors, which only a re-index fixes. */
  needs_indexing: boolean;
  chunks: number;
  size_bytes: number;
  error: string;
  created_at: string;
  updated_at: string;
}

export interface DocumentList {
  /** False where the machine has knowledge switched off entirely. */
  available: boolean;
  documents: Document[];
}
