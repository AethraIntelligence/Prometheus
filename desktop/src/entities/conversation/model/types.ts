/**
 * The shapes the runtime sends for a conversation, mirrored once.
 *
 * Produced by `application/interface/views.py`. They are declared here so a
 * component can be type-checked - not so this layer can reinterpret them.
 * Nothing in the frontend derives a status, recomputes a cost or decides
 * whether work is finished; if a field the UI needs does not exist, it is added
 * to the projection in Python, where every interface gets it at once.
 */

export type ObjectiveStatus =
  | "RECEIVED"
  | "PLANNING"
  | "RUNNING"
  | "DONE"
  | "FAILED"
  | "ESCALATED"
  | "CANCELLED";

/**
 * Whether an action that needs approval is asked about, done or refused, for
 * one request. The gate reads it in the runtime; the window only carries it.
 */
export type ApprovalChoice = "ASK" | "AUTO" | "DENY";

/** How to go about a request, said beside it rather than inside it. */
export interface Directions {
  approvals: ApprovalChoice;
  /** A catalog entry by name. Empty lets the runtime choose. */
  model: string;
}

export const NO_DIRECTIONS: Directions = { approvals: "ASK", model: "" };

/**
 * One turn: what was asked, and what came back.
 *
 * A turn is an objective - the unit the platform already records in full.
 * There is no separate assistant message anywhere in this application, because
 * a second record of the same answer is the one that goes stale.
 */
export interface Message {
  id: string;
  text: string;
  /** How this request was asked to be carried out. Absent from an older runtime. */
  directions?: Directions;
  status: ObjectiveStatus;
  thinking: boolean;
  answer: string;
  missing: string[];
  answered: boolean;
  cost_usd: number;
  created_at: string;
  finished_at: string | null;
}

export interface Conversation {
  id: string;
  title: string;
  messages: number;
  /** Where the latest request in it stands, as the runtime says. None before the first. */
  status?: ObjectiveStatus | null;
  created_at: string;
  updated_at: string;
}

export interface ConversationList {
  conversations: Conversation[];
}

export interface Thread extends Omit<Conversation, "messages"> {
  messages: Message[];
  /**
   * What the composer opens on, as the runtime decided: the schedule's own
   * settings in a schedule's thread, otherwise how the last request was asked.
   * Null for a thread nothing has been asked in.
   */
  directions?: Directions | null;
  /** The schedule that writes into this thread, if one does. */
  schedule_id?: string | null;
}
