export type WorkflowInputKind = "STRING" | "INTEGER" | "NUMBER" | "BOOLEAN";

export interface WorkflowInput {
  name: string;
  kind: WorkflowInputKind;
  required: boolean;
  default: unknown;
  description: string;
}

export interface WorkflowRun {
  id: string;
  workflow: string;
  workflow_version: number;
  trigger: string;
  status: string;
  summary: string;
  cost_usd: number;
  quality: number;
  started_at: string;
  finished_at: string | null;
}

export interface Workflow {
  name: string;
  version: number;
  description: string;
  trigger: string;
  inputs: WorkflowInput[];
  profile: { approvals: "ASK" | "AUTO" | "DENY"; model: string };
  budget: {
    max_steps: number | null;
    max_cost_usd: number | null;
    max_wall_time_seconds: number | null;
  };
  steps: Array<{
    name: string;
    employee: string;
    depends_on: string[];
    max_attempts: number;
    on_failure: string;
  }>;
  readiness: { ready: boolean; issues: string[] };
  metrics: {
    runs: number;
    success_rate: number | null;
    average_cost_usd: number | null;
  };
  recent_runs: WorkflowRun[];
}

export interface WorkflowList {
  available: boolean;
  workflows: Workflow[];
}

export interface WorkflowDryRun {
  workflow: string;
  version: number;
  inputs: Record<string, unknown>;
  executable: boolean;
  readiness: { ready: boolean; issues: string[] };
  steps: Array<{
    name: string;
    employee: string;
    instruction: string;
    depends_on: string[];
    max_attempts: number;
  }>;
}
