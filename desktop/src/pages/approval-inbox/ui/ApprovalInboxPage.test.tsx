import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import type { Approval, ApprovalInbox } from "../../../entities/approval";
import { RuntimeClient, RuntimeProvider } from "../../../shared/api";
import { ApprovalInboxPage } from "./ApprovalInboxPage";

const BASE = "http://127.0.0.1:9999";

function approval(overrides: Partial<Approval> = {}): Approval {
  return {
    id: "a1",
    task_id: "t1",
    action: "write report.md",
    risk: "HIGH",
    reason: "The existing report will be replaced.",
    payload: { path: "report.md" },
    requested_at: "2026-09-17T08:00:00Z",
    live: true,
    state: "PENDING",
    conversation_id: "c1",
    objective_id: "o1",
    task_goal: "Prepare the launch report",
    task_status: "WAITING_FOR_APPROVAL",
    resource_group: "path:report.md",
    wait_seconds: 360,
    wait_group: "LONG_WAIT",
    actionable: true,
    approve_effect: "The employee will write this exact file.",
    reject_effect: "The file will not be written.",
    status_explanation: "The employee is waiting for your decision.",
    ...overrides,
  };
}

function body(inbox: Partial<ApprovalInbox> = {}): ApprovalInbox {
  return {
    pending: [],
    recent: [],
    counts: { total: 0, actionable: 0, critical: 0, long_wait: 0 },
    ...inbox,
  };
}

function json(value: unknown) {
  return new Response(JSON.stringify(value), {
    status: 200,
    headers: { "Content-Type": "application/json" },
  });
}

describe("Approval Inbox", () => {
  it("groups global decisions, explains their effects and links to their work", async () => {
    const pending = [
      approval(),
      approval({ id: "a2", action: "send launch email", risk: "CRITICAL", resource_group: "to:client@example.com", conversation_id: "c2", objective_id: "o2" }),
    ];
    const fetch = vi.fn(async () => json(body({ pending, counts: { total: 2, actionable: 2, critical: 1, long_wait: 2 } })));
    const openWork = vi.fn();
    const openThread = vi.fn();
    render(
      <RuntimeProvider client={new RuntimeClient(BASE, fetch as never)}>
        <ApprovalInboxPage onOpenWork={openWork} onOpenThread={openThread} />
      </RuntimeProvider>,
    );

    expect(await screen.findByText("write report.md")).toBeInTheDocument();
    expect(screen.getByText("send launch email")).toBeInTheDocument();
    expect(screen.getByText("The employee will write this exact file.")).toBeInTheDocument();
    expect(screen.getByText("The file will not be written.")).toBeInTheDocument();
    await userEvent.selectOptions(screen.getByLabelText("Group by"), "resource");
    expect(screen.getByRole("heading", { name: "path:report.md" })).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Open work and evidence" }));
    await userEvent.click(screen.getByRole("button", { name: "Open conversation" }));
    expect(openWork).toHaveBeenCalledWith("o1");
    expect(openThread).toHaveBeenCalledWith("c1");
  });

  it("sends a decision centrally and shows stale requests as final", async () => {
    const expired = approval({
      id: "old",
      live: false,
      state: "EXPIRED",
      actionable: false,
      resolved_at: "2026-09-17T09:00:00Z",
      comment: "No active action was waiting for this decision.",
      status_explanation: "Expired. Nothing can execute from this request.",
    });
    const posted: string[] = [];
    const fetch = vi.fn(async (url: string, init?: RequestInit) => {
      const path = url.replace(BASE, "");
      if ((init?.method ?? "GET") === "POST") {
        posted.push(path);
        return json({ id: "a1", state: "APPROVED", live: true });
      }
      return json(body({ pending: [approval()], recent: [expired], counts: { total: 1, actionable: 1, critical: 0, long_wait: 1 } }));
    });
    render(
      <RuntimeProvider client={new RuntimeClient(BASE, fetch as never)}>
        <ApprovalInboxPage />
      </RuntimeProvider>,
    );

    await userEvent.click(await screen.findByRole("button", { name: "Approve" }));
    await waitFor(() => expect(posted).toContain("/api/approvals/a1"));
    await userEvent.click(screen.getByRole("button", { name: "Recent decisions" }));
    expect(screen.getByText("Expired. Nothing can execute from this request.")).toBeInTheDocument();
    expect(screen.getByText("No active action was waiting for this decision.")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Approve" })).not.toBeInTheDocument();
  });
});
