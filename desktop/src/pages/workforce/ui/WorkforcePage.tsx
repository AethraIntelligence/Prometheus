import { useState } from "react";

import {
  ReadinessBadge,
  type EmployeeProfile,
  type Metric,
  type RecoveryPlace,
} from "../../../entities/employee";
import type { WorkflowSuggestion } from "../../../entities/workflow";
import { SaveSuggestionDialog } from "../../../features/review-workflow-suggestion";
import { useRuntime } from "../../../shared/api";
import { PageHead } from "../../../shared/ui";
import { useWorkforce } from "../model/useWorkforce";

type Tab = "roles" | "suggestions";

const OUTCOMES: Array<[string, string]> = [
  ["ACCEPTED", "Accepted"],
  ["NOT_ACCEPTED", "Completed, not accepted"],
  ["REFUSED", "Refused"],
  ["FAILED", "Failed"],
  ["CANCELLED", "Cancelled"],
  ["OPEN", "In progress"],
];

const words = (value: string) => value.replaceAll("_", " ").toLowerCase();

/** A metric as the runtime sent it: its value, or the reason it has none. */
function Figure({ label, metric, format }: { label: string; metric: Metric; format: (value: number) => string }) {
  return (
    <div className="workforce-figure">
      <span>{label}</span>
      <b>{metric.value === null ? "—" : format(metric.value)}</b>
      <small>{metric.value === null ? metric.note : `n = ${metric.sample}`}</small>
    </div>
  );
}

function Recovery({ place, hint, onRecover }: { place: RecoveryPlace | null; hint: string; onRecover?: (place: RecoveryPlace) => void }) {
  if (!place) return null;
  const opens = place !== "DECLARATION" && onRecover;
  return (
    <span className="workforce-recovery">
      {hint}
      {opens && (
        <button type="button" className="mini bordered" onClick={() => onRecover(place)}>
          Open
        </button>
      )}
    </span>
  );
}

function Profile({ profile, windowDays, onWindow, onRecover }: {
  profile: EmployeeProfile;
  windowDays: number;
  onWindow: (days: number) => void;
  onRecover?: (place: RecoveryPlace) => void;
}) {
  const record = profile.performance;
  return (
    <section className="work-detail" aria-label={`${profile.name} profile`}>
      <div className="work-title">
        <div>
          <ReadinessBadge state={profile.readiness.state} />
          <h2>{profile.title}</h2>
          <p className="work-muted">{profile.name} · version {profile.version}</p>
        </div>
      </div>
      {profile.description && <p className="workforce-description">{profile.description}</p>}

      <section className="work-section">
        <h3>Readiness</h3>
        {profile.readiness.reasons.length === 0 ? (
          <p className="work-muted">Everything it declares is available on this machine.</p>
        ) : (
          <ul className="workforce-reasons">
            {profile.readiness.reasons.map((reason) => (
              <li key={`${reason.code}:${reason.message}`}>
                <ReadinessBadge state={reason.state} /> {reason.message}
                <Recovery place={reason.recovery} hint={reason.recovery_hint} onRecover={onRecover} />
              </li>
            ))}
          </ul>
        )}
      </section>

      <section className="work-section">
        <h3>What it can do</h3>
        <p>{profile.capabilities.length ? profile.capabilities.map(words).join(", ") : "No declared capabilities."}</p>
        {profile.goals.length > 0 && <ul>{profile.goals.map((goal) => <li key={goal}>{goal}</li>)}</ul>}
      </section>

      <section className="work-section">
        <h3>Hand-off contract</h3>
        {!profile.contract.declared && <p className="work-muted">Not declared; safe defaults apply.</p>}
        <dl className="approval-context">
          <div><dt>Starts from</dt><dd>{profile.contract.accepts_anything ? "Anything" : profile.contract.accepts.map(words).join(", ")}</dd></div>
          <div><dt>Delivers</dt><dd>{profile.contract.produces.map(words).join(", ")}</dd></div>
          <div><dt>Evidence required</dt><dd>{profile.contract.evidence.length ? profile.contract.evidence.map(words).join(", ") : "None"}</dd></div>
        </dl>
      </section>

      <section className="work-section">
        <h3>What it is allowed to do</h3>
        <table className="workforce-table">
          <thead><tr><th>Tool</th><th>Effect</th><th>Here</th></tr></thead>
          <tbody>
            {profile.tools.map((tool) => (
              <tr key={tool.name}>
                <td><code>{tool.name}</code></td>
                <td>{tool.effect ? words(tool.effect) : "—"}</td>
                <td>
                  {!tool.available ? "Not offered here" : tool.denied_by_policy ? "Denied by policy" : tool.asks_first ? "Asks a person first" : "Available"}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
        {profile.integrations.length > 0 && (
          <p className="work-muted">
            Services: {profile.integrations.map((item) => `${item.name} (${item.connected ? "connected" : "not connected"}${item.declared ? "" : ", granted"})`).join("; ")}
          </p>
        )}
        {profile.policies.length > 0 && (
          <ul>{profile.policies.map((policy) => <li key={policy.name}><b>{policy.name}</b> — {policy.description}</li>)}</ul>
        )}
        <p className="work-muted">
          Memory: {words(profile.memory_scope)} · Limits: {profile.limits.max_steps} steps, ${profile.limits.max_cost_usd}, {Math.round(profile.limits.max_wall_time_seconds)}s
        </p>
      </section>

      <section className="work-section">
        <h3>Model requirements</h3>
        <p>
          {profile.model.capabilities.map(words).join(", ") || "Any model"}
          {profile.model.min_context_tokens ? ` · at least ${profile.model.min_context_tokens.toLocaleString()} tokens of context` : ""}
          {` · temperature ${profile.model.temperature}`}
        </p>
      </section>

      <section className="work-section">
        <div className="workforce-section-head">
          <h3>Record</h3>
          <label>
            Window
            <select aria-label="Window" value={windowDays} onChange={(event) => onWindow(Number(event.target.value))}>
              <option value={7}>7 days</option>
              <option value={30}>30 days</option>
              <option value={90}>90 days</option>
            </select>
          </label>
        </div>
        {!record.has_history ? (
          <p className="card-empty">No assignments in the last {record.window.days} days — no data yet.</p>
        ) : (
          <>
            <p className="work-muted">
              {record.assignments} assignment(s) in the last {record.window.days} days in this workspace
              {record.derived_verdicts > 0 ? ` · ${record.derived_verdicts} verdict(s) derived from older records` : ""}
            </p>
            <div className="workforce-figures">
              <Figure label="Accepted results" metric={record.accepted_rate} format={(value) => `${Math.round(value * 100)}%`} />
              <Figure label="Cost per accepted result" metric={record.cost_per_accepted_usd} format={(value) => `$${value.toFixed(4)}`} />
              <Figure label="Median time" metric={record.median_latency_seconds} format={(value) => `${Math.round(value)}s`} />
              <Figure label="95th percentile time" metric={record.p95_latency_seconds} format={(value) => `${Math.round(value)}s`} />
              <Figure label="Interventions per assignment" metric={record.interventions_per_assignment} format={(value) => value.toFixed(2)} />
              <Figure label="Scenario pass rate" metric={record.scenario_pass_rate} format={(value) => `${Math.round(value * 100)}%`} />
            </div>
            <ul className="workforce-outcomes" aria-label="Outcomes">
              {OUTCOMES.map(([key, label]) => <li key={key}><span>{label}</span><b>{record.outcomes[key] ?? 0}</b></li>)}
            </ul>
            {Object.keys(record.failures).length > 0 && (
              <p className="work-muted">Failure kinds: {Object.entries(record.failures).map(([kind, count]) => `${words(kind)} ${count}`).join(", ")}</p>
            )}
          </>
        )}
      </section>

      <section className="work-section">
        <h3>Recent assignments</h3>
        {profile.recent_assignments.length === 0 ? (
          <p className="work-muted">Nothing has been assigned to it in this window.</p>
        ) : (
          profile.recent_assignments.map((item) => (
            <article className="work-task" key={item.id}>
              <div className="work-task-head">
                <div>
                  <span className={`work-state ${item.outcome.toLowerCase()}`}>{words(item.outcome)}</span>
                  <h4>{item.goal || "Task no longer stored"}</h4>
                </div>
                <strong>${item.cost_usd.toFixed(4)}</strong>
              </div>
              <p className="work-why">Why: {item.decision.reason || words(item.decision.code)}</p>
              {item.acceptance && !item.acceptance.accepted && <p className="work-muted">Not accepted: {item.acceptance.reason}</p>}
            </article>
          ))
        )}
      </section>
    </section>
  );
}

function Suggestion({ suggestion, busy, onDismiss, onSnooze, onSave }: {
  suggestion: WorkflowSuggestion;
  busy: boolean;
  onDismiss: () => void;
  onSnooze: () => void;
  onSave: () => void;
}) {
  return (
    <article className="workforce-suggestion" aria-label={`Suggestion ${suggestion.proposed_name}`}>
      <h3>{suggestion.proposed_name}</h3>
      <p className="work-muted">
        Seen in {suggestion.occurrences} successful runs between {new Date(suggestion.first_seen).toLocaleDateString()} and {new Date(suggestion.last_seen).toLocaleDateString()}.
      </p>
      <p className="work-muted">Input: {suggestion.inputs.map((input) => `${input.name}${input.required ? " (required)" : ""}`).join(", ")}</p>
      <ol className="workforce-steps">
        {suggestion.steps.map((step) => (
          <li key={step.name}>
            <b>{step.employee}</b>
            {step.depends_on.length > 0 && <span> after {step.depends_on.join(", ")}</span>}
            {step.needs.length > 0 && <span> · needs {step.needs.map(words).join(", ")}</span>}
            <small>Effects: {step.effects.length ? step.effects.map(words).join(", ") : "none"} · {words(step.readiness)}</small>
          </li>
        ))}
      </ol>
      <details>
        <summary>Source runs</summary>
        <ul>{suggestion.sources.map((source) => <li key={source}><code>{source}</code></li>)}</ul>
      </details>
      <div className="work-task-actions">
        <button type="button" disabled={busy} onClick={onSave}>Save as workflow…</button>
        <button type="button" disabled={busy} onClick={onSnooze}>Snooze 7 days</button>
        <button type="button" disabled={busy} onClick={onDismiss}>Dismiss</button>
      </div>
    </article>
  );
}

export function WorkforcePage({
  railOpen = true,
  onOpenRail,
  onRecover,
}: {
  railOpen?: boolean;
  onOpenRail?: () => void;
  onRecover?: (place: RecoveryPlace) => void;
}) {
  const page = useWorkforce(useRuntime());
  const [tab, setTab] = useState<Tab>("roles");
  const [saving, setSaving] = useState<WorkflowSuggestion | null>(null);
  const unavailable = page.workforce.employees.filter((item) => item.readiness.state === "UNAVAILABLE").length;

  return (
    <main className="main">
      <PageHead
        title="Workforce"
        railOpen={railOpen}
        onOpenRail={onOpenRail}
        chip={unavailable ? `${unavailable} unavailable` : `${page.workforce.employees.length} roles`}
      />
      <div className="work-center">
        <nav className="work-tabs" aria-label="Workforce views">
          <button type="button" className={tab === "roles" ? "on" : ""} onClick={() => setTab("roles")}>
            Roles<span>{page.workforce.employees.length}</span>
          </button>
          <button type="button" className={tab === "suggestions" ? "on" : ""} onClick={() => setTab("suggestions")}>
            Workflow suggestions<span>{page.suggestions.suggestions.length}</span>
          </button>
        </nav>
        {page.problem && <p className="problem" role="alert">{page.problem}</p>}
        {page.notice && <p className="work-muted workforce-notice" role="status">{page.notice}</p>}

        {tab === "roles" ? (
          !page.workforce.available ? (
            <p className="card-empty">This runtime was started without workforce profiles.</p>
          ) : (
            <div className="work-layout">
              <aside className="work-list" aria-label="Roles">
                {!page.ready ? (
                  <p className="card-empty">Loading the workforce…</p>
                ) : page.workforce.employees.length === 0 ? (
                  <p className="card-empty">No employee is declared.</p>
                ) : (
                  page.workforce.employees.map((employee) => (
                    <button type="button" key={employee.name} className={employee.name === page.selected ? "on" : ""} onClick={() => page.select(employee.name)}>
                      <b>{employee.title}</b>
                      <span>{employee.readiness.state === "READY" ? employee.description : employee.readiness.summary}</span>
                      <small><ReadinessBadge state={employee.readiness.state} /> {employee.name}</small>
                    </button>
                  ))
                )}
                {page.workforce.overlaps.map((group) => (
                  <p className="work-muted workforce-overlap" key={group.join(",")}>
                    {group.join(" and ")} declare exactly the same work; nothing tells them apart when work is assigned.
                  </p>
                ))}
              </aside>
              {page.profile ? (
                <Profile profile={page.profile} windowDays={page.windowDays} onWindow={page.setWindowDays} onRecover={onRecover} />
              ) : (
                <section className="work-detail empty"><p>Select a role to see its profile.</p></section>
              )}
            </div>
          )
        ) : !page.suggestions.available ? (
          <p className="card-empty">Workflows are switched off on this machine, so nothing is suggested.</p>
        ) : page.suggestions.suggestions.length === 0 ? (
          <p className="card-empty">No recurring process yet. A draft appears after the same structure of work succeeds at least three times.</p>
        ) : (
          <div className="workforce-suggestions">
            {page.suggestions.suggestions.map((suggestion) => (
              <Suggestion
                key={suggestion.id}
                suggestion={suggestion}
                busy={page.busy}
                onDismiss={() => void page.dismiss(suggestion.id)}
                onSnooze={() => void page.snooze(suggestion.id, 7)}
                onSave={() => setSaving(suggestion)}
              />
            ))}
          </div>
        )}
      </div>
      {saving && (
        <SaveSuggestionDialog
          suggestion={saving}
          busy={page.busy}
          onClose={() => setSaving(null)}
          onSave={(name, description) => {
            void page.save(saving.id, name, description).then((saved) => {
              if (saved) setSaving(null);
            });
          }}
        />
      )}
    </main>
  );
}
