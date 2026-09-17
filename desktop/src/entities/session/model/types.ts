/**
 * A thread's brief, as the core projects and compacts it.
 *
 * Decisions, open questions and files are read off the thread's own requests
 * on every read; stages are older requests folded into a summary. The window
 * renders them and derives none of them.
 */

export interface SessionNote {
  text: string;
  objective_id: string;
  recorded_at: string;
}

export interface SessionArtifact {
  path: string;
  objective_id: string;
  recorded_at: string;
}

export interface SessionStage {
  index: number;
  summary: string;
  objective_ids: string[];
  started_at: string;
  ended_at: string;
  compacted_at: string;
  /** False where the fallback wrote the summary because no model could. */
  summarised: boolean;
}

export interface SessionBrief {
  conversation_id: string;
  goal: string;
  decisions: SessionNote[];
  open_questions: SessionNote[];
  artifacts: SessionArtifact[];
  stages: SessionStage[];
  total_turns: number;
  compacted_turns: number;
  recent_turns: number;
}
