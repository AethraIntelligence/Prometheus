/**
 * One standing request: what it is, when it runs next, and - only if asked -
 * everything else.
 *
 * The card used to say all of it at once: three lines of prose between the
 * name and the run list, of which the person glancing at the page needed one.
 * What is wanted at a glance is whether this thing is on and when it goes
 * again; "last run fell short", how many runs there have been, what happens to
 * a missed time and the list of what each run cost are all the same question -
 * what has this been doing - and that question is asked deliberately, so it
 * lives behind one summary that says how many runs there are to look at.
 *
 * Two things stay out of the fold that could have gone in. The model, because
 * a schedule running on something that cannot do the work is the commonest way
 * one quietly fails; and the approvals, because "approves without asking" is
 * the riskiest fact about a schedule and hiding it would be hiding exactly the
 * thing somebody should notice in passing.
 *
 * The actions are handed in, and the card puts them in the middle under a
 * rule: they read as a quiet row of choices about this one card rather than as
 * buttons competing with the work above them.
 */

import type { ReactNode } from "react";

import type { Schedule } from "../model/types";
import { describeLastRun, describeWhen, formatMoment } from "../model/when";

const APPROVALS: Record<string, string> = {
  ASK: "asks before approvals",
  AUTO: "approves without asking",
  DENY: "refuses anything needing approval",
};

export function ScheduleCard({
  schedule,
  running,
  actions,
  order,
}: {
  schedule: Schedule;
  /** Whether the runtime is firing schedules; a next time it will not keep is not shown as one. */
  running: boolean;
  actions?: ReactNode;
  /**
   * Whether this one runs in a settled order, and the offer to keep one when
   * the platform has watched it settle. Handed in for the reason the actions
   * are: deciding is a feature's, and this card renders.
   */
  order?: ReactNode;
}) {
  const next = formatMoment(schedule.next_due_at);
  const runs = schedule.recent_runs ?? [];
  const everRan = schedule.runs > 0 || runs.length > 0;

  return (
    <article className={schedule.enabled ? "schedule" : "schedule paused"}>
      <div className="schedule-head">
        <strong>{schedule.name || schedule.request}</strong>
        {!schedule.enabled && <span className="badge quiet">Paused</span>}
        {(schedule.consecutive_failures ?? 0) > 0 && (
          <span className="badge danger">
            {schedule.consecutive_failures} consecutive failure
            {schedule.consecutive_failures === 1 ? "" : "s"}
          </span>
        )}
      </div>
      {schedule.name && <p className="schedule-request">{schedule.request}</p>}
      {order}

      <p className="schedule-when">
        <b>{describeWhen(schedule)}</b>
        {schedule.enabled && running && next && <span>next {next}</span>}
      </p>
      <p className="note schedule-meta">
        {schedule.model ? `Model ${schedule.model}` : "Model chosen automatically"}
        {` · ${APPROVALS[schedule.approvals] ?? "asks before approvals"}`}
      </p>

      {everRan ? (
        <details className="schedule-history">
          <summary>
            History
            <span>
              {schedule.runs} {schedule.runs === 1 ? "run" : "runs"}
            </span>
          </summary>
          <p className="note">{describeLastRun(schedule)}</p>
          {runs.length > 0 && (
            <ol className="schedule-runs" aria-label="Recent runs">
              {runs.map((run) => (
                <li key={run.id}>
                  <span className={`run-state ${run.status.toLowerCase()}`}>{run.status}</span>
                  <time dateTime={run.finished_at}>{formatMoment(run.finished_at)}</time>
                  <span>${run.cost_usd.toFixed(2)}</span>
                  {run.quality !== undefined && (
                    <span>{Math.round(run.quality * 100)}% quality</span>
                  )}
                </li>
              ))}
            </ol>
          )}
          <p className="note schedule-policy">
            Declared step retries · overlapping runs are skipped · missed times are combined into
            one current run
          </p>
        </details>
      ) : (
        <p className="note">Not run yet</p>
      )}

      {actions && <div className="schedule-foot">{actions}</div>}
    </article>
  );
}
