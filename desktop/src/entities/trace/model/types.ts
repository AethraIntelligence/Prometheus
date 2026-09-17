export type TraceStatus = "UNSET" | "RUNNING" | "OK" | "ERROR" | "CANCELLED" | "DENIED" | "DEGRADED";

export interface TraceEvent {
  schema_version: number;
  event_id: string;
  trace_id: string;
  span_id: string;
  parent_id: string | null;
  correlation_id: string;
  causation_id: string | null;
  workspace_id: string;
  entity_type: string;
  entity_id: string;
  actor: string;
  kind: string;
  status: TraceStatus;
  name: string;
  reason_code: string;
  started_at: string;
  ended_at: string | null;
  duration_ms: number | null;
  attributes: Record<string, unknown>;
  sequence: number;
}

export interface Trace {
  schema_version: number;
  trace_id: string;
  run_kind: "ASK" | "TASK" | "WORKFLOW" | "SCHEDULE";
  workspace_id: string;
  root: { type: string; id: string };
  degraded: boolean;
  degradation_reason: string;
  first_failure_span_id: string | null;
  events: TraceEvent[];
}

export interface DiagnosticHealth {
  database: { status: string; backend: string };
  model_providers: { status: string; profiles: number };
  sandbox: { status: string };
  integrations: { status: string };
  scheduler: { status: string };
  observability: { available: boolean; queued: number; dropped: number; last_error: string; exporter: string };
  audit: { valid: boolean; checked: number; first_invalid_sequence: number | null; reason: string };
}
