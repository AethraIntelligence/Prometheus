import type { ApprovalGrant } from "../../../entities/approval";
import type { RuntimeClient } from "../../../shared/api";

/**
 * Carry one person's answer to the runtime. The whole of this feature's logic.
 *
 * There is deliberately no "approve everything", no remembered answer and no
 * default: an unanswered question is a refusal, and a window that could turn
 * silence into a yes would be the one place in the platform where nobody
 * decided.
 */
export function decideApproval(
  client: RuntimeClient,
  approvalId: string,
  approved: boolean,
  comment = "",
  grant: ApprovalGrant = "ONCE",
  durationSeconds?: number,
): Promise<unknown> {
  return client.post(`/api/approvals/${approvalId}`, {
    approved,
    comment,
    grant,
    duration_seconds: durationSeconds,
  });
}
