export interface Employee {
  id: string;
  name: string;
  title: string;
  description: string;
  tools: string[];
  /** Services granted in the employee's own file. */
  integrations?: string[];
  limits: {
    max_steps: number;
    max_cost_usd: number;
    max_wall_time_seconds: number;
  };
}

/** The core's verdict on whether a role can work here now. Rendered, never derived. */
export type ReadinessState = "READY" | "DEGRADED" | "UNAVAILABLE";

/** Where a person goes to fix a reason. Named places; the app maps them to screens. */
export type RecoveryPlace = "PLUGINS" | "MODELS" | "GENERAL" | "DECLARATION";

export interface ReadinessReason {
  code: string;
  state: ReadinessState;
  message: string;
  recovery: RecoveryPlace | null;
  recovery_hint: string;
}

export interface Readiness {
  state: ReadinessState;
  assignable: boolean;
  summary: string;
  lost_capabilities: string[];
  reasons: ReadinessReason[];
}

/** One role in the workforce list. */
export interface EmployeeCard {
  id: string;
  name: string;
  title: string;
  description: string;
  version: string;
  capabilities: string[];
  readiness: Readiness;
}

export interface Workforce {
  available: boolean;
  employees: EmployeeCard[];
  /** Groups of roles that declare exactly the same work. */
  overlaps: string[][];
}

/** A number, or the reason there is none - with its sample and minimum. */
export interface Metric {
  value: number | null;
  sample: number;
  minimum: number;
  sufficient: boolean;
  note: string;
}

export interface Performance {
  window: { start: string; end: string; days: number };
  scope: string;
  has_history: boolean;
  assignments: number;
  outcomes: Record<string, number>;
  failures: Record<string, number>;
  derived_verdicts: number;
  accepted_rate: Metric;
  cost_per_accepted_usd: Metric;
  median_latency_seconds: Metric;
  p95_latency_seconds: Metric;
  interventions_per_assignment: Metric;
  scenario_pass_rate: Metric;
  total_cost_usd: number;
}

export interface Alternative {
  employee: string;
  code: string;
  reason: string;
}

/** Why an employee was given a piece of work, and who else was considered. */
export interface AssignmentDecision {
  code: string;
  reason: string;
  alternatives: Alternative[];
  indistinguishable: string[];
}

export interface AssignmentVerdict {
  accepted: boolean;
  reason: string;
  refused: boolean;
  code: string;
}

export interface RecentAssignment {
  id: string;
  task_id: string;
  goal: string;
  plan_id: string | null;
  status: string | null;
  outcome: string;
  cost_usd: number;
  assigned_at: string;
  completed_at: string | null;
  decision: AssignmentDecision;
  acceptance: AssignmentVerdict | null;
}

export interface EmployeeProfile extends EmployeeCard {
  enabled: boolean;
  goals: string[];
  tools: Array<{
    name: string;
    effect: string | null;
    available: boolean;
    capabilities: string[];
    denied_by_policy: boolean;
    asks_first: boolean;
  }>;
  integrations: Array<{ name: string; declared: boolean; connected: boolean }>;
  policies: Array<{ name: string; description: string; denies: string[] }>;
  model: {
    capabilities: string[];
    min_context_tokens: number | null;
    max_cost_per_1k_usd: number | null;
    temperature: number;
  };
  memory_scope: string;
  limits: Employee["limits"];
  contract: {
    declared: boolean;
    accepts: string[];
    accepts_anything: boolean;
    produces: string[];
    evidence: string[];
    failure_kinds: string[];
  };
  performance: Performance;
  recent_assignments: RecentAssignment[];
}
