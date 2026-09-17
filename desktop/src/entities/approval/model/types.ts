export interface Approval {
  id: string;
  task_id: string;
  action: string;
  risk: string;
  reason: string;
  payload: Record<string, unknown>;
  requested_at: string;
  /** Whether a tool call is actually parked on this, or the run has since died. */
  live: boolean;
  /** The thread whose work asked. Null for work no thread holds - a terminal, a script. */
  conversation_id?: string | null;
  state?: string;
  scope?: {
    subject: string;
    subject_name?: string;
    action: string;
    resource: string;
    limits: Record<string, number | string>;
  } | null;
  preview?: Record<string, unknown>;
  policy_source?: string;
  requires_explicit_confirmation?: boolean;
  context_sources?: Array<{ source: string; kind: string; trust: string }>;
  grant?: ApprovalGrant;
  lease_id?: string | null;
}

export type ApprovalGrant = "ONCE" | "TASK" | "PERSISTENT";

export interface CapabilityLease {
  id: string;
  workspace_id: string;
  subject: string;
  subject_name?: string;
  action: string;
  resource: string;
  limits: Record<string, number | string>;
  grant: Exclude<ApprovalGrant, "ONCE">;
  reason: string;
  task_id?: string | null;
  approval_id: string;
  created_at: string;
  expires_at?: string | null;
}
