/**
 * A schedule's timing in the person's own words and clock.
 *
 * Only presentation: the runtime has already decided which moment is meant and
 * sends it in UTC; this says it the way the clock on the wall would.
 */

import type { Schedule } from "./types";

function plural(count: number, unit: string): string {
  return count === 1 ? `Every ${unit}` : `Every ${count} ${unit}s`;
}

/** "Every 2 hours", "Every day at 09:30", "When objective.finished happens". */
export function describeWhen(schedule: Schedule, now: Date = new Date()): string {
  if (schedule.every_seconds) {
    const minutes = Math.round(schedule.every_seconds / 60);
    if (minutes % (60 * 24) === 0) return plural(minutes / (60 * 24), "day");
    if (minutes % 60 === 0) return plural(minutes / 60, "hour");
    return plural(minutes, "minute");
  }
  if (schedule.daily_at && schedule.timezone) {
    return `Every day at ${schedule.daily_at} (${schedule.timezone})`;
  }
  if (schedule.daily_at_utc) {
    return `Every day at ${localTime(schedule.daily_at_utc, now)}`;
  }
  if (schedule.on_event) return `When ${schedule.on_event} happens`;
  return "";
}

/** A UTC HH:MM as today's local HH:MM. */
export function localTime(utc: string, now: Date = new Date()): string {
  const [hours, minutes] = utc.split(":").map(Number);
  const moment = new Date(
    Date.UTC(now.getUTCFullYear(), now.getUTCMonth(), now.getUTCDate(), hours, minutes),
  );
  return moment.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
}

/** A UTC HH:MM as today's local HH:MM on a 24-hour clock, for a time field. */
export function localClock(utc: string, now: Date = new Date()): string {
  const [hours, minutes] = utc.split(":").map(Number);
  const moment = new Date(
    Date.UTC(now.getUTCFullYear(), now.getUTCMonth(), now.getUTCDate(), hours, minutes),
  );
  const pad = (value: number) => String(value).padStart(2, "0");
  return `${pad(moment.getHours())}:${pad(moment.getMinutes())}`;
}

/** "Mon 14 Sep, 09:30" in the person's clock, or "" for no moment. */
export function formatMoment(iso: string | null): string {
  if (!iso) return "";
  const moment = new Date(iso);
  if (Number.isNaN(moment.getTime())) return "";
  return moment.toLocaleString([], {
    weekday: "short",
    day: "numeric",
    month: "short",
    hour: "2-digit",
    minute: "2-digit",
  });
}

const LAST: Record<string, string> = {
  DONE: "finished",
  FAILED: "failed",
  ESCALATED: "fell short",
  CANCELLED: "was stopped",
  RECEIVED: "is working",
  PLANNING: "is working",
  RUNNING: "is working",
};

/** "Last run finished Mon 14 Sep, 09:30", or "Not run yet". */
export function describeLastRun(schedule: Schedule): string {
  if (!schedule.last_run_at) return "Not run yet";
  const outcome = (schedule.last_status && LAST[schedule.last_status]) || "ran";
  return `Last run ${outcome} ${formatMoment(schedule.last_run_at)}`;
}
