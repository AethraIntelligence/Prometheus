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
}
