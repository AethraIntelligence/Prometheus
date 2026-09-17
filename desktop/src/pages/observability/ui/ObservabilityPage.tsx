import { useState } from "react";

import type { TraceEvent } from "../../../entities/trace";
import { useRuntime } from "../../../shared/api";
import { describe } from "../../../shared/lib";
import { PageHead } from "../../../shared/ui";
import { useObservability } from "../model/useObservability";

const duration = (value: number | null) => value == null ? "duration unavailable" : value < 1000 ? `${value} ms` : `${(value / 1000).toFixed(1)} s`;

function TimelineEvent({ event, firstFailure }: { event: TraceEvent; firstFailure: boolean }) {
  return (
    <li className={`trace-event ${event.status.toLowerCase()} ${firstFailure ? "causal" : ""}`}>
      <span className="trace-line" aria-hidden="true" />
      <div>
        <p><b>{event.name}</b><span>{event.kind.replaceAll("_", " ")}</span></p>
        <small>{event.status} · {duration(event.duration_ms)} · {new Date(event.started_at).toLocaleString()}</small>
        {firstFailure && <strong>First causal failure</strong>}
        {event.reason_code && <p className="trace-reason">Reason: {event.reason_code.replaceAll("_", " ").toLowerCase()}</p>}
        <details>
          <summary>Safe metadata</summary>
          <pre>{JSON.stringify(event.attributes, null, 2)}</pre>
        </details>
      </div>
    </li>
  );
}

export function ObservabilityPage({ initialTraceId = null, railOpen = true, onOpenRail }: { initialTraceId?: string | null; railOpen?: boolean; onOpenRail?: () => void }) {
  const page = useObservability(useRuntime(), initialTraceId);
  const [path, setPath] = useState("");
  const [exported, setExported] = useState("");
  const [exportProblem, setExportProblem] = useState("");
  const health = page.health;

  const save = async () => {
    setExported("");
    setExportProblem("");
    try {
      await page.exportBundle(path);
      setExported(path);
    } catch (error) {
      setExportProblem(describe(error));
    }
  };

  return (
    <main className="main">
      <PageHead title="Observability" railOpen={railOpen} onOpenRail={onOpenRail} chip={health?.observability.available ? "healthy" : "degraded"} />
      <div className="observability">
        {page.problem && <p className="problem" role="alert">{page.problem}</p>}
        <section className="health-grid" aria-label="Runtime health">
          {health && Object.entries({ Database: `${health.database.status} · ${health.database.backend}`, Models: `${health.model_providers.status} · ${health.model_providers.profiles} profiles`, Sandbox: health.sandbox.status, Integrations: health.integrations.status, Scheduler: health.scheduler.status, Exporter: health.observability.exporter, Audit: health.audit.valid ? `verified · ${health.audit.checked} records` : `invalid · ${health.audit.reason}` }).map(([label, value]) => <article key={label}><span>{label}</span><b>{value}</b></article>)}
        </section>
        {health && (!health.observability.available || !health.audit.valid) && <p className="problem">Diagnostics are degraded. Work continues, but some evidence may be missing.</p>}
        <div className="trace-layout">
          <aside className="trace-list" aria-label="Runs">
            {!page.ready ? <p>Loading traces…</p> : page.traces.length === 0 ? <p>No traces in this workspace.</p> : page.traces.map((trace) => <button type="button" className={page.selected === trace.trace_id ? "on" : ""} key={trace.trace_id} onClick={() => page.setSelected(trace.trace_id)}><b>{trace.run_kind}</b><span>{trace.root.type} · {trace.trace_id.slice(0, 8)}</span><small>{trace.events.length} event(s)</small></button>)}
          </aside>
          <section className="trace-detail" aria-label="Trace details">
            {!page.trace ? <p>Select a run to inspect its causal timeline.</p> : <>
              <header><div><span>{page.trace.run_kind}</span><h2>{page.trace.trace_id}</h2></div><small>Schema v{page.trace.schema_version}</small></header>
              {page.trace.degraded && <p className="problem">Some trace data is unavailable: {page.trace.degradation_reason || "reason unavailable"}</p>}
              <ol className="trace-timeline">{page.trace.events.map((event) => <TimelineEvent key={`${event.event_id}:${event.sequence}`} event={event} firstFailure={event.span_id === page.trace?.first_failure_span_id} />)}</ol>
            </>}
          </section>
        </div>
        <section className="diagnostic-export">
          <div><h2>Diagnostic bundle</h2><p>Review the sanitized bundle before saving it. Prompts, files, screenshots, credentials, environment variables and the raw database are excluded.</p></div>
          <button type="button" onClick={() => void page.preview()}>Preview bundle</button>
          {page.bundle && <><details open><summary>Sanitized preview</summary><pre>{JSON.stringify(page.bundle, null, 2)}</pre></details><label>Save to path<input value={path} onChange={(event) => setPath(event.target.value)} placeholder="/path/to/prometheus-diagnostics.json" /></label><button type="button" className="primary" disabled={!path} onClick={() => void save()}>Save new file</button></>}
          {exported && <p role="status">Saved to {exported}</p>}
          {exportProblem && <p className="problem" role="alert">{exportProblem}</p>}
        </section>
        <details className="metrics"><summary>Metric definitions and current window</summary><pre>{JSON.stringify(page.metrics, null, 2)}</pre></details>
      </div>
    </main>
  );
}
