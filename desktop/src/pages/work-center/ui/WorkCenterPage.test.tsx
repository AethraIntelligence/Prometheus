import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import type { WorkItem } from "../../../entities/work-item";
import { RuntimeClient, RuntimeProvider } from "../../../shared/api";
import { WorkCenterPage } from "./WorkCenterPage";

const BASE = "http://127.0.0.1:9999";

function item(overrides: Partial<WorkItem> = {}): WorkItem {
  return {
    id: "o1",
    text: "Prepare the launch report",
    status: "RUNNING",
    bucket: "ACTIVE",
    next_action: "No action is needed while the employees continue working.",
    conversation_id: "c1",
    constraints: { deadline: "Friday" },
    acceptance_criteria: ["Covers revenue and risks"],
    cost_usd: 0.04,
    current_task_id: "t1",
    current_step: 2,
    plan: { id: "p1", revision: 1, status: "RUNNING", rationale: "Research before writing.", tasks: [] },
    tasks: [{
      id: "t1",
      goal: "Collect reliable figures",
      status: "RUNNING",
      employee: "researcher",
      employee_title: "Researcher",
      assignment_reason: "Best match for source research.",
      depends_on: [],
      current_step: 2,
      cost_usd: 0.04,
      budgets: {
        steps: { used: 2, limit: 12 },
        cost_usd: { used: 0.04, limit: 1 },
        wall_time_seconds: { used: 30, limit: 300 },
      },
      models: [
        { provider: "local", model: "reasoner", calls: 2, cost_usd: 0.04, task_kind: "EXECUTION", reason: "configured default for execution", escalation_level: 0 },
        { provider: "local", model: "thinker", calls: 1, cost_usd: 0.02, task_kind: "EXECUTION", reason: "escalated from 'fast' (fast) to strong after verification rejected", escalation_level: 1 },
      ],
      escalated: true,
      model_reason: "Routed for execution requirements.",
      tools: [{ tool: "search", success: true, reason: "Used search for current sources.", output: {}, error: "" }],
      result: null,
      error: null,
      controls: { pause: true, resume: false, cancel: true, retry: false, handoff: false },
    }],
    artifacts: [],
    result: null,
    controls: { pause: true, resume: false, cancel: true, retry: false },
    created_at: "2026-09-17T08:00:00Z",
    finished_at: null,
    ...overrides,
  };
}

function json(body: unknown) {
  return new Response(JSON.stringify(body), { status: 200, headers: { "Content-Type": "application/json" } });
}

describe("Work Center", () => {
  it("shows state, next action, ownership, budgets and safe controls", async () => {
    const posted: string[] = [];
    const fetch = vi.fn(async (url: string, init?: RequestInit) => {
      const path = url.replace(BASE, "");
      if ((init?.method ?? "GET") === "POST") posted.push(path);
      if (path === "/api/employees") return json({ employees: [{ id: "e1", name: "researcher", title: "Researcher", description: "", tools: [], limits: { max_steps: 12, max_cost_usd: 1, max_wall_time_seconds: 300 } }] });
      if (path === "/api/work/o1/pause") return json(item({ status: "PAUSED", bucket: "BLOCKED" }));
      return json({ items: [item()] });
    });
    const client = new RuntimeClient(BASE, fetch as never);
    render(<RuntimeProvider client={client}><WorkCenterPage /></RuntimeProvider>);

    expect(await screen.findByRole("heading", { name: "Prepare the launch report" })).toBeInTheDocument();
    expect(screen.getByText("Covers revenue and risks")).toBeInTheDocument();
    expect(screen.getByText("Why this employee: Best match for source research.")).toBeInTheDocument();
    expect(screen.getByText(/Escalated model · thinker · execution .* after verification rejected/)).toBeInTheDocument();
    expect(screen.getByText(/Model · reasoner · execution .* configured default for execution/)).toBeInTheDocument();
    expect(within(screen.getByText("Actions").parentElement!).getByText("2.00 / 12")).toBeInTheDocument();
    expect(screen.getAllByText("No action is needed while the employees continue working.")).toHaveLength(2);

    await userEvent.click(screen.getByRole("button", { name: "Pause safely" }));
    await waitFor(() => expect(posted).toContain("/api/work/o1/pause"));
  });

  it("separates approval, blocked, failed and completed work", async () => {
    const items = [
      item({ id: "waiting", text: "Needs approval", bucket: "WAITING" }),
      item({ id: "blocked", text: "Needs help", bucket: "BLOCKED" }),
      item({ id: "failed", text: "Needs retry", bucket: "FAILED" }),
      item({ id: "done", text: "Finished report", bucket: "COMPLETED" }),
    ];
    const fetch = vi.fn(async (url: string) => url.endsWith("/api/employees") ? json({ employees: [] }) : json({ items }));
    render(<RuntimeProvider client={new RuntimeClient(BASE, fetch as never)}><WorkCenterPage /></RuntimeProvider>);

    await screen.findByRole("button", { name: /Waiting for approval/ });
    await userEvent.click(screen.getByRole("button", { name: /Waiting for approval/ }));
    expect(await screen.findByRole("heading", { name: "Needs approval" })).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: /Completed/ }));
    expect(await screen.findByRole("heading", { name: "Finished report" })).toBeInTheDocument();
  });

  it("opens the exact work item linked from an approval", async () => {
    const items = [
      item({ id: "active", text: "Other work", bucket: "ACTIVE" }),
      item({ id: "waiting", text: "Approval source", bucket: "WAITING" }),
    ];
    const fetch = vi.fn(async (url: string) => url.endsWith("/api/employees") ? json({ employees: [] }) : json({ items }));

    render(
      <RuntimeProvider client={new RuntimeClient(BASE, fetch as never)}>
        <WorkCenterPage initialObjectiveId="waiting" />
      </RuntimeProvider>,
    );

    expect(await screen.findByRole("heading", { name: "Approval source" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /Waiting for approval/ })).toHaveClass("on");
  });
});
