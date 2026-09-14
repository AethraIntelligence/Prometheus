/**
 * A standing request, as the runtime describes it.
 *
 * Times are UTC ISO strings and the recurrence arrives as its parts; turning
 * them into the person's own clock is presentation (`model/when.ts`), while
 * which moment is meant is the runtime's.
 */

export interface Schedule {
  id: string;
  name: string;
  request: string;
  enabled: boolean;
  every_seconds: number | null;
  /** HH:MM in UTC. */
  daily_at_utc: string | null;
  on_event: string;
  next_due_at: string | null;
  last_run_at: string | null;
  /** Where the last run's request ended up, as the runtime says. */
  last_status: string | null;
  runs: number;
  /** The thread its runs are written into. */
  conversation_id: string | null;
  /** A catalog entry its runs prefer. Empty: the router decides. */
  model: string;
  /** ASK, AUTO or DENY: what its runs do about an action that needs approval. */
  approvals: "ASK" | "AUTO" | "DENY";
  created_at: string;
}

export interface ScheduleList {
  available?: boolean;
  /** Whether this runtime is firing schedules at all. */
  running?: boolean;
  schedules?: Schedule[];
}
