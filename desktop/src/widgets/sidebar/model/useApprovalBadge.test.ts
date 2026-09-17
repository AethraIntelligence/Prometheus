import { describe, expect, it } from "vitest";

import type { Approval } from "../../../entities/approval";
import { approvalNotificationState } from "./useApprovalBadge";

function pending(id: string, actionable = true): Approval {
  return {
    id,
    task_id: `task-${id}`,
    action: "write a file",
    risk: "HIGH",
    reason: "The file already exists.",
    payload: {},
    requested_at: "2026-09-17T08:00:00Z",
    live: actionable,
    actionable,
  };
}

describe("approval notification badge", () => {
  it("announces an actionable request once while keeping its durable count", () => {
    const seen = new Set<string>();
    const items = [pending("a1"), pending("stale", false)];

    const first = approvalNotificationState(items, seen);
    first.newIds.forEach((id) => seen.add(id));
    const nextPoll = approvalNotificationState(items, seen);

    expect(first).toEqual({ count: 1, newIds: ["a1"] });
    expect(nextPoll).toEqual({ count: 1, newIds: [] });
  });
});
