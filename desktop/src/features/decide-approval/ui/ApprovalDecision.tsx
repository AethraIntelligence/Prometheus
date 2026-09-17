/**
 * The two buttons. Neither answer is the quiet one: Approve carries the colour
 * of the card it sits on and Reject a full outline, so a person scanning the
 * card sees two choices rather than one choice and a way out.
 */

import { useState } from "react";

import type { ApprovalGrant } from "../../../entities/approval";

interface Props {
  approvalId: string;
  exactOnly?: boolean;
  onDecide: (
    approvalId: string,
    approved: boolean,
    grant?: ApprovalGrant,
    durationSeconds?: number,
  ) => void | Promise<void>;
}

export function ApprovalDecision({ approvalId, exactOnly = false, onDecide }: Props) {
  const [grant, setGrant] = useState<ApprovalGrant>("ONCE");
  const [duration, setDuration] = useState("86400");
  const durationSeconds = grant === "PERSISTENT" && duration ? Number(duration) : undefined;
  return (
    <>
      <label className="gate-scope">
        Permission
        <select
          value={exactOnly ? "ONCE" : grant}
          disabled={exactOnly}
          onChange={(event) => setGrant(event.target.value as ApprovalGrant)}
        >
          <option value="ONCE">Only this action</option>
          <option value="TASK">This exact action for this task</option>
          <option value="PERSISTENT">Remember this exact rule</option>
        </select>
      </label>
      {!exactOnly && grant === "PERSISTENT" && (
        <label className="gate-scope">
          Expires
          <select value={duration} onChange={(event) => setDuration(event.target.value)}>
            <option value="3600">In 1 hour</option>
            <option value="86400">In 24 hours</option>
            <option value="2592000">In 30 days</option>
            <option value="">Never</option>
          </select>
        </label>
      )}
      <button
        type="button"
        className="btn btn-wait"
        onClick={() => void onDecide(approvalId, true, exactOnly ? "ONCE" : grant, durationSeconds)}
      >
        Approve
      </button>
      <button
        type="button"
        className="btn btn-line"
        onClick={() => void onDecide(approvalId, false, "ONCE")}
      >
        Reject
      </button>
    </>
  );
}
