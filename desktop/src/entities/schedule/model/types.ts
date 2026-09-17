/**
 * A standing request, as the runtime describes it.
 *
 * Times are UTC ISO strings and the recurrence arrives as its parts; turning
 * them into the person's own clock is presentation (`model/when.ts`), while
 * which moment is meant is the runtime's.
 */

export interface ScheduleRun {
  id: string;
  objective_id: string;
  status: string;
  started_at: string | null;
  finished_at: string;
  cost_usd: number;
  summary: string;
  schedule_version: number;
}

export interface Schedule {
  id: string;
  name: string;
  request: string;
  enabled: boolean;
  every_seconds: number | null;
  /** HH:MM in UTC. */
  daily_at_utc: string | null;
  /** Wall-clock time for zoned schedules, or the same UTC time for legacy ones. */
  daily_at?: string | null;
  /** IANA zone, for example Europe/Rome. Empty on legacy UTC schedules. */
  timezone?: string;
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
  version?: number;
  /** The nearest completed firings, newest first. */
  recent_runs?: ScheduleRun[];
  consecutive_failures?: number;
  last_success_at?: string | null;
  /** A still-running firing prevents another one from starting. */
  overlap_policy?: "SKIP";
  /** Missed times collapse into one current run, never a catch-up burst. */
  misfire_policy?: "COALESCE";
}

export interface ScheduleList {
  available?: boolean;
  /** Whether this runtime is firing schedules at all. */
  running?: boolean;
  schedules?: Schedule[];
}
