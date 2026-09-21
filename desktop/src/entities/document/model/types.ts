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

/**
 * One document being read or embedded right now.
 *
 * Keyed by `key`, not by `document_id`: reading a file happens before there is
 * a document, and a row that appears only once the slowest stage is over is a
 * row that appears too late to be of use. `fraction` arrives computed - the
 * core answers how far along something is, and a window deriving it from `done`
 * and `total` would be the second place that answer lives.
 */
export interface Indexing {
  key: string;
  /** Empty until the text has been read and a record exists. */
  document_id: string;
  title: string;
  stage: "READING" | "CHUNKING" | "EMBEDDING" | "DONE" | "FAILED";
  done: number;
  total: number;
  /** 0 where there is no countable progress to show - not "nothing done yet". */
  fraction: number;
  finished: boolean;
  error: string;
  at: string;
}

export interface DocumentList {
  /** False where the machine has knowledge switched off entirely. */
  available: boolean;
  documents: Document[];
  /** What is being worked on at the moment the list was read. */
  indexing: Indexing[];
}

/**
 * A passage the retrieval returned, and why it was chosen.
 *
 * Both halves of the score travel with it: a machine with no embedding model
 * retrieves on words alone and answers slightly worse, and that belongs on the
 * screen rather than in an inference from worse answers.
 */
export interface Passage {
  document_id: string;
  title: string;
  source: string;
  content: string;
  score: number;
  lexical: number;
  semantic: number;
}
