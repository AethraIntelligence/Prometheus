/**
 * The processes this machine holds, read and not chosen.
 *
 * Every other way to see them has gone: a person setting work on a clock is
 * not choosing a process, so the list left the new-schedule form and the
 * catalog left the Scheduled page. What is left is the honest question this
 * screen answers everywhere else - what is on this machine - and it belongs
 * beside the rest of that, not in front of somebody trying to put a task on a
 * clock.
 *
 * Read-only on purpose. A process gets here one of two ways: a schedule's own
 * runs settled into it and somebody kept it, or somebody wrote the file. There
 * is no third way from this screen, and running one from here would be starting
 * real work off an analytics page.
 */

import { useEffect, useState } from "react";

import { workflowApi, type Workflow } from "../../../../entities/workflow";
import { useRuntime } from "../../../../shared/api";
import { describe } from "../../../../shared/lib";

export function ProcessesSection() {
  const client = useRuntime();
  const [workflows, setWorkflows] = useState<Workflow[]>([]);
  const [ready, setReady] = useState(false);
  const [problem, setProblem] = useState("");

  useEffect(() => {
    let current = true;
    void workflowApi
      .all(client)
      .then((body) => current && setWorkflows(body.workflows ?? []))
      .catch((error) => current && setProblem(describe(error)))
      .finally(() => current && setReady(true));
    return () => {
      current = false;
    };
  }, [client]);

  return (
    <>
      <p className="lede">
        A process is a fixed order of steps, kept so that work stops being planned again from
        scratch every time. One arrives when a schedule's own runs settle into the same order and
        you keep it, or when somebody writes the declaration by hand.
      </p>
      {problem && (
        <p className="problem" role="alert">
          {problem}
        </p>
      )}

      <section className="panel">
        <div className="panel-head">
          <h2>Declared here</h2>
        </div>
        <div className="card">
          {!ready ? (
            <p className="card-empty">Reading…</p>
          ) : workflows.length === 0 ? (
            <p className="card-empty">
              Nothing yet. Schedules run their request the long way until one settles.
            </p>
          ) : (
            workflows.map((workflow) => (
              <article className="process" key={`${workflow.name}@${workflow.version}`}>
                <header>
                  <strong>{workflow.name}</strong>
                  <span className="process-version">v{workflow.version}</span>
                  <span className={workflow.readiness.ready ? "badge" : "badge danger"}>
                    {workflow.readiness.ready ? "Ready" : "Not ready"}
                  </span>
                </header>
                {workflow.description && <p className="note">{workflow.description}</p>}
                <p className="note">
                  {workflow.steps.length} step(s) · {workflow.metrics.runs} run(s) ·{" "}
                  {workflow.metrics.success_rate === null
                    ? "no quality history"
                    : `${Math.round(workflow.metrics.success_rate * 100)}% success`}
                </p>
                {workflow.readiness.issues.map((issue) => (
                  <p className="problem" key={issue}>
                    {issue}
                  </p>
                ))}
              </article>
            ))
          )}
        </div>
      </section>
    </>
  );
}
