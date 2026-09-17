import { useEffect, useMemo, useState } from "react";

import type { BudgetValue, WorkBucket, WorkItem, WorkTask } from "../../../entities/work-item";
import { useRuntime } from "../../../shared/api";
import { PageHead } from "../../../shared/ui";
import { useWorkCenter } from "../model/useWorkCenter";

const BUCKETS: Array<{ value: WorkBucket; label: string }> = [
  { value: "ACTIVE", label: "Active" },
  { value: "WAITING", label: "Waiting for approval" },
  { value: "BLOCKED", label: "Blocked" },
  { value: "FAILED", label: "Failed" },
  { value: "COMPLETED", label: "Completed" },
];

const compact = (value: number) => (value < 10 ? value.toFixed(2) : Math.round(value).toString());

function Budget({ label, value, suffix = "" }: { label: string; value: BudgetValue; suffix?: string }) {
  const ratio = value.limit ? Math.min(100, (value.used / value.limit) * 100) : 0;
  return (
    <div className="work-budget">
      <span>{label}</span>
      <b>
        {compact(value.used)}{suffix} / {value.limit == null ? "No limit" : `${compact(value.limit)}${suffix}`}
      </b>
      <i><span style={{ width: `${ratio}%` }} /></i>
    </div>
  );
}

function TaskDetail({
  task,
  employees,
  disabled,
  onRetry,
  onHandoff,
}: {
  task: WorkTask;
  employees: string[];
  disabled: boolean;
  onRetry: () => void;
  onHandoff: (employee: string) => void;
}) {
  const [employee, setEmployee] = useState(employees[0] ?? "");
  return (
    <article className="work-task">
      <div className="work-task-head">
        <div>
          <span className={`work-state ${task.status.toLowerCase()}`}>{task.status.replaceAll("_", " ")}</span>
          <h4>{task.goal}</h4>
        </div>
        <strong>{task.employee_title || task.employee}</strong>
      </div>
      <p className="work-why">Why this employee: {task.assignment_reason}</p>
      {task.depends_on.length > 0 && <p className="work-muted">Starts after {task.depends_on.length} earlier step(s).</p>}
      <div className="work-budgets">
        <Budget label="Actions" value={task.budgets.steps} />
        <Budget label="Cost" value={task.budgets.cost_usd} suffix=" USD" />
        <Budget label="Time" value={task.budgets.wall_time_seconds} suffix="s" />
      </div>
      {(task.models.length > 0 || task.tools.length > 0) && (
        <details>
          <summary>Models and tools</summary>
          <p className="work-muted">{task.model_reason}</p>
          {task.models.map((model) => (
            <p key={`${model.provider}:${model.model}`} className="work-evidence">
              Model · {model.model} · {model.calls} call(s) · ${model.cost_usd.toFixed(4)}
            </p>
          ))}
          {task.tools.map((tool, index) => (
            <p key={`${tool.tool}:${index}`} className="work-evidence">
              {tool.success ? "Used" : "Failed"} · {tool.reason}
            </p>
          ))}
        </details>
      )}
      {task.result && (
        <div className="work-result">
          <b>Result and evidence</b>
          <p>{task.result.summary}</p>
          {task.result.artifacts.map((artifact) => <code key={artifact}>{artifact}</code>)}
        </div>
      )}
      {task.error && <p className="problem">{task.error.message}</p>}
      {(task.controls.retry || task.controls.handoff) && (
        <div className="work-task-actions">
          {task.controls.retry && <button type="button" disabled={disabled} onClick={onRetry}>Retry step</button>}
          {task.controls.handoff && (
            <>
              <select aria-label="New employee" value={employee} onChange={(event) => setEmployee(event.target.value)}>
                {employees.map((name) => <option key={name}>{name}</option>)}
              </select>
              <button type="button" disabled={disabled || !employee} onClick={() => onHandoff(employee)}>Hand off</button>
            </>
          )}
        </div>
      )}
    </article>
  );
}

function WorkDetail({ item, page, onOpenThread }: { item: WorkItem; page: ReturnType<typeof useWorkCenter>; onOpenThread?: (id: string) => void }) {
  const employeeNames = page.employees.map((employee) => employee.name);
  return (
    <section className="work-detail" aria-label="Work details">
      <div className="work-title">
        <div><span className={`work-state ${item.bucket.toLowerCase()}`}>{item.bucket}</span><h2>{item.text}</h2></div>
        {item.conversation_id && onOpenThread && <button type="button" onClick={() => onOpenThread(item.conversation_id!)}>Open conversation</button>}
      </div>
      <div className="work-next"><b>Next action</b><p>{item.next_action}</p></div>
      <div className="work-actions">
        {item.controls.pause && <button type="button" disabled={page.busy} onClick={() => void page.pause(item.id)}>Pause safely</button>}
        {item.controls.resume && <button type="button" className="primary" disabled={page.busy} onClick={() => void page.resume(item.id)}>Resume</button>}
        {item.controls.cancel && <button type="button" disabled={page.busy} onClick={() => void page.cancel(item.id)}>Cancel</button>}
        {item.controls.retry && <button type="button" className="primary" disabled={page.busy} onClick={() => void page.retry(item.id)}>Retry work</button>}
      </div>
      <section className="work-section">
        <h3>Definition of done</h3>
        {item.acceptance_criteria.length ? <ul>{item.acceptance_criteria.map((criterion) => <li key={criterion}>{criterion}</li>)}</ul> : <p className="work-muted">No explicit acceptance criteria were recorded.</p>}
        {Object.keys(item.constraints).length > 0 && (
          <p className="work-muted">Constraints: {JSON.stringify(item.constraints)}</p>
        )}
      </section>
      {item.plan && <section className="work-section"><h3>Plan</h3><p>{item.plan.rationale}</p></section>}
      <section className="work-section">
        <h3>Execution</h3>
        {item.tasks.length === 0 ? <p className="work-muted">Prometheus is preparing the plan.</p> : item.tasks.map((task) => (
          <TaskDetail key={task.id} task={task} employees={employeeNames} disabled={page.busy} onRetry={() => void page.retryTask(task.id)} onHandoff={(employee) => void page.handoff(task.id, employee)} />
        ))}
      </section>
      {(item.result || item.artifacts.length > 0) && <section className="work-section"><h3>Outcome</h3>{item.result && <p>{item.result.summary}</p>}{item.artifacts.map((artifact) => <div className="work-artifact" key={artifact.path}><b>{artifact.name}</b><span>{artifact.exists ? artifact.location : "File is no longer available"}</span></div>)}</section>}
    </section>
  );
}

export function WorkCenterPage({ initialObjectiveId = null, railOpen = true, onOpenRail, onOpenThread }: { initialObjectiveId?: string | null; railOpen?: boolean; onOpenRail?: () => void; onOpenThread?: (id: string) => void }) {
  const page = useWorkCenter(useRuntime());
  const [bucket, setBucket] = useState<WorkBucket>("ACTIVE");
  const [selected, setSelected] = useState<string | null>(initialObjectiveId);
  const shown = useMemo(() => page.items.filter((item) => item.bucket === bucket), [page.items, bucket]);
  const item = page.items.find((candidate) => candidate.id === selected) ?? shown[0] ?? null;

  useEffect(() => {
    if (selected && item && item.bucket !== bucket) setBucket(item.bucket);
  }, [bucket, item, selected]);

  return (
    <main className="main">
      <PageHead title="Work Center" railOpen={railOpen} onOpenRail={onOpenRail} chip={`${page.items.filter((entry) => entry.bucket !== "COMPLETED").length} open`} />
      <div className="work-center">
        <nav className="work-tabs" aria-label="Work states">
          {BUCKETS.map((entry) => <button type="button" className={bucket === entry.value ? "on" : ""} key={entry.value} onClick={() => { setBucket(entry.value); setSelected(null); }}>{entry.label}<span>{page.items.filter((item) => item.bucket === entry.value).length}</span></button>)}
        </nav>
        {page.problem && <p className="problem" role="alert">{page.problem}</p>}
        <div className="work-layout">
          <aside className="work-list" aria-label={`${bucket} work`}>
            {!page.ready ? <p className="card-empty">Loading work…</p> : shown.length === 0 ? <p className="card-empty">Nothing here.</p> : shown.map((entry) => <button type="button" className={entry.id === item?.id ? "on" : ""} key={entry.id} onClick={() => setSelected(entry.id)}><b>{entry.text}</b><span>{entry.next_action}</span><small>${entry.cost_usd.toFixed(4)} · {entry.tasks.length} step(s)</small></button>)}
          </aside>
          {item ? <WorkDetail item={item} page={page} onOpenThread={onOpenThread} /> : <section className="work-detail empty"><p>Select another state to inspect its work.</p></section>}
        </div>
      </div>
    </main>
  );
}
