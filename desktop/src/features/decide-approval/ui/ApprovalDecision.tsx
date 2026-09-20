/**
 * The three answers, as three buttons.
 *
 * A person reading a gate has three things to say and one of them is "yes, and
 * stop asking me this". That used to be a select of grants and a second select
 * of expiries sitting in front of the buttons - a form to fill in before the
 * question could be answered, for a decision that is made in a second. The
 * grants have not changed; what changed is that the window no longer asks the
 * person to compose one.
 *
 * "Always" is `PERSISTENT` with no expiry, because a permission that quietly
 * lapses is a permission that starts asking again without anybody choosing
 * that. It stays exact - this employee, this tool, this resource - and Settings
 * -> Permissions is where it is taken back.
 */

import type { ApprovalGrant } from "../../../entities/approval";

interface Props {
  approvalId: string;
  disabled?: boolean;
  onDecide: (
    approvalId: string,
    approved: boolean,
    grant?: ApprovalGrant,
    durationSeconds?: number,
  ) => void | Promise<void>;
}

export function ApprovalDecision({ approvalId, disabled = false, onDecide }: Props) {
  return (
    <>
      <button
        type="button"
        className="btn btn-ink"
        disabled={disabled}
        onClick={() => void onDecide(approvalId, true, "ONCE", undefined)}
      >
        Approve
      </button>
      <button
        type="button"
        className="btn btn-line"
        disabled={disabled}
        title="Approve this exact action and stop asking about it"
        onClick={() => void onDecide(approvalId, true, "PERSISTENT", undefined)}
      >
        Always approve
      </button>
      <button
        type="button"
        className="btn btn-line"
        disabled={disabled}
        onClick={() => void onDecide(approvalId, false, "ONCE")}
      >
        Reject
      </button>
    </>
  );
}
