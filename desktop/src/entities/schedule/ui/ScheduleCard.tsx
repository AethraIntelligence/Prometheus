import type { ReactNode } from "react";

import type { Schedule } from "../model/types";
import { describeLastRun, describeWhen, formatMoment } from "../model/when";

const APPROVALS: Record<string, string> = {
  ASK: "asks before approvals",
  AUTO: "approves without asking",
  DENY: "refuses anything needing approval",
};

/** One standing request: what, when, what happened last time. Actions are handed in. */
export function ScheduleCard({
  schedule,
  running,
  actions,
}: {
  schedule: Schedule;
  /** Whether the runtime is firing schedules; a next time it will not keep is not shown as one. */
  running: boolean;
  actions?: ReactNode;
}) {
  const next = formatMoment(schedule.next_due_at);
  return (
    <article className={schedule.enabled ? "schedule" : "schedule paused"}>
      <div className="schedule-head">
        <strong>{schedule.name || schedule.request}</strong>
        {!schedule.enabled && <span className="badge quiet">Paused</span>}
      </div>
      {schedule.name && <p className="schedule-request">{schedule.request}</p>}
      <p className="note">
        {describeWhen(schedule)}
        {schedule.enabled && running && next && ` · next ${next}`}
        {` · ${schedule.model ? `model ${schedule.model}` : "model chosen automatically"}`}
        {` · ${APPROVALS[schedule.approvals] ?? "asks before approvals"}`}
      </p>
      <p className="note">
        {describeLastRun(schedule)}
        {schedule.runs > 0 && ` · ${schedule.runs} ${schedule.runs === 1 ? "run" : "runs"}`}
      </p>
      {actions}
    </article>
  );
}
