export type WorkBucket = "ACTIVE" | "WAITING" | "BLOCKED" | "FAILED" | "COMPLETED";

export interface WorkArtifact {
  path: string;
  name: string;
  location: string;
  media_type: string;
  size: number | null;
  exists: boolean;
}

export interface WorkControls {
  pause: boolean;
  resume: boolean;
  cancel: boolean;
  retry: boolean;
}

export interface BudgetValue {
  used: number;
  limit: number | null;
}

export interface WorkTask {
  id: string;
  goal: string;
  status: string;
  employee: string;
  employee_title: string;
  assignment_reason: string;
  depends_on: string[];
  current_step: number;
  cost_usd: number;
  budgets: {
    steps: BudgetValue;
    cost_usd: BudgetValue;
    wall_time_seconds: BudgetValue;
  };
  models: Array<{ provider: string; model: string; calls: number; cost_usd: number }>;
  model_reason: string;
  tools: Array<{ tool: string; success: boolean; reason: string; output: unknown; error: string }>;
  result: { summary: string; artifacts: string[]; evidence: unknown } | null;
  error: { kind: string; message: string; details: Record<string, unknown> } | null;
  controls: WorkControls & { handoff: boolean };
}

export interface WorkPlan {
  id: string;
  revision: number;
  status: string;
  rationale: string;
  tasks: Array<{ id: string; goal: string; status: string; depends_on: string[] }>;
}

export interface WorkItem {
  id: string;
  text: string;
  status: string;
  bucket: WorkBucket;
  next_action: string;
  conversation_id: string | null;
  constraints: Record<string, unknown>;
  acceptance_criteria: string[];
  cost_usd: number;
  current_task_id: string | null;
  current_step: number;
  plan: WorkPlan | null;
  tasks: WorkTask[];
  artifacts: WorkArtifact[];
  result: { summary: string; missing: string[]; output: unknown } | null;
  controls: WorkControls;
  created_at: string;
  finished_at: string | null;
}
