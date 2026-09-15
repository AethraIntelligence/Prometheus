import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { ApprovalDecision } from "../../../features/decide-approval";
import type { Approval } from "../model/types";
import { ApprovalCard } from "./ApprovalCard";

const approval = (extra: Partial<Approval> = {}): Approval => ({
  id: "a1",
  task_id: "t1",
  action: "send an email",
  risk: "HIGH",
  reason: "Sending cannot be undone.",
  payload: { to: "client@example.com", subject: "Project proposal" },
  requested_at: "2026-09-08T09:00:00+00:00",
  live: true,
  ...extra,
});

describe("ApprovalCard", () => {
  it("shows what is about to happen, in the runtime's own words", () => {
    render(<ApprovalCard approval={approval()} />);

    expect(screen.getByText("Prometheus wants to send an email")).toBeInTheDocument();
    expect(screen.getByText("HIGH")).toBeInTheDocument();
  });

  it("keeps the arguments folded until somebody asks to see them", async () => {
    render(<ApprovalCard approval={approval()} />);

    expect(screen.queryByText("client@example.com")).not.toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Show details" }));
    expect(screen.getByText("client@example.com")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Hide details" }));
    expect(screen.queryByText("client@example.com")).not.toBeInTheDocument();
  });

  it("has no decision of its own until one is handed to it", () => {
    render(<ApprovalCard approval={approval()} />);

    expect(screen.queryByRole("button", { name: "Approve" })).not.toBeInTheDocument();
  });

  it("says when the run that asked has already ended", () => {
    render(<ApprovalCard approval={approval({ live: false })} />);

    expect(screen.getByText(/Nothing is waiting on this any more/)).toBeInTheDocument();
  });

  it("carries an approval and a rejection to whoever was given the decision", async () => {
    const onDecide = vi.fn();
    render(
      <ApprovalCard
        approval={approval()}
        actions={<ApprovalDecision approvalId="a1" onDecide={onDecide} />}
      />,
    );

    await userEvent.click(screen.getByRole("button", { name: "Approve" }));
    await userEvent.click(screen.getByRole("button", { name: "Reject" }));

    expect(onDecide.mock.calls).toEqual([
      ["a1", true],
      ["a1", false],
    ]);
  });
});
