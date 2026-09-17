/** The Workforce screen renders core verdicts and confirms workflow drafts. */

import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { RuntimeClient, RuntimeProvider } from "../../../shared/api";
import { WorkforcePage } from "./WorkforcePage";

const BASE = "http://127.0.0.1:9999";

const ready = {
  state: "READY",
  assignable: true,
  summary: "ready",
  lost_capabilities: [],
  reasons: [],
};

const unavailable = {
  state: "UNAVAILABLE",
  assignable: false,
  summary: "Its declaration requires gmail, which is not connected here.",
  lost_capabilities: [],
  reasons: [
    {
      code: "INTEGRATION_NOT_CONNECTED",
      state: "UNAVAILABLE",
      message: "Its declaration requires gmail, which is not connected here.",
      recovery: "PLUGINS",
      recovery_hint: "Connect gmail in Settings > Plugins.",
    },
  ],
};

const noHistory = {
  window: { start: "2026-08-18T12:00:00+00:00", end: "2026-09-17T12:00:00+00:00", days: 30 },
  scope: "workspace",
  has_history: false,
  assignments: 0,
  outcomes: { ACCEPTED: 0, NOT_ACCEPTED: 0, REFUSED: 0, FAILED: 0, CANCELLED: 0, OPEN: 0 },
  failures: {},
  derived_verdicts: 0,
  accepted_rate: { value: null, sample: 0, minimum: 5, sufficient: false, note: "No data: no decided assignments in this window." },
  cost_per_accepted_usd: { value: null, sample: 0, minimum: 1, sufficient: false, note: "No accepted result in this window." },
  median_latency_seconds: { value: null, sample: 0, minimum: 5, sufficient: false, note: "No data." },
  p95_latency_seconds: { value: null, sample: 0, minimum: 20, sufficient: false, note: "No data." },
  interventions_per_assignment: { value: null, sample: 0, minimum: 5, sufficient: false, note: "No data." },
  scenario_pass_rate: { value: null, sample: 0, minimum: 5, sufficient: false, note: "No validation run records this employee." },
  total_cost_usd: 0,
};

function profile(name: "researcher" | "mailer") {
  const isMailer = name === "mailer";
  return {
    id: `${name}-id`,
    name,
    title: isMailer ? "Mailer" : "Researcher",
    description: isMailer ? "Sends mail." : "Finds facts.",
    version: "abc123",
    enabled: true,
    goals: [isMailer ? "Send a message." : "Find what is true."],
    capabilities: [isMailer ? "MESSAGING" : "FILE_ACCESS"],
    readiness: isMailer ? unavailable : ready,
    tools: isMailer
      ? []
      : [{ name: "fs.read", effect: "READ", available: true, capabilities: ["FILE_ACCESS"], denied_by_policy: false, asks_first: false }],
    integrations: isMailer ? [{ name: "gmail", declared: true, connected: false }] : [],
    policies: [],
    model: { capabilities: ["TEXT_REASONING"], min_context_tokens: null, max_cost_per_1k_usd: null, temperature: 0.2 },
    memory_scope: "WORKSPACE",
    limits: { max_steps: 10, max_cost_usd: 1, max_wall_time_seconds: 300 },
    contract: { declared: true, accepts: [], accepts_anything: true, produces: ["ANSWER"], evidence: [], failure_kinds: ["EXECUTION"] },
    performance: noHistory,
    recent_assignments: [],
  };
}

function scriptedRuntime() {
  const state = {
    suggestions: [
      {
        id: "suggestion-1",
        status: "OPEN",
        occurrences: 3,
        first_seen: "2026-09-01T12:00:00+00:00",
        last_seen: "2026-09-17T12:00:00+00:00",
        sources: ["objective-1", "objective-2", "objective-3"],
        proposed_name: "researcher-then-writer",
        description: "Seen three times.",
        inputs: [{ name: "request", kind: "STRING", required: true }],
        steps: [
          { name: "step-1", employee: "researcher", needs: ["FILE_ACCESS"], depends_on: [], instruction: "Do it", effects: ["READ"], readiness: "READY" },
          { name: "step-2", employee: "writer", needs: [], depends_on: ["step-1"], instruction: "Write it", effects: ["WRITE"], readiness: "READY" },
        ],
      },
    ],
    failSave: false,
    posts: [] as Array<{ path: string; body: Record<string, unknown> }>,
  };
  const fetchImpl = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const path = String(input).replace(BASE, "");
    const body = init?.body ? JSON.parse(String(init.body)) : {};
    if (init?.method === "POST") {
      state.posts.push({ path, body });
      if (path.endsWith("/save") && state.failSave) return problem("The workflow file could not be written.");
      if (path.includes("/workflow-suggestions/")) state.suggestions = [];
      return json(path.endsWith("/save") ? { workflow: body.name, file: `${body.name}.yaml` } : { status: "DISMISSED" });
    }
    if (path === "/api/workforce") {
      return json({
        available: true,
        employees: [profile("researcher"), profile("mailer")].map(({ tools, integrations, policies, model, memory_scope, limits, contract, performance, recent_assignments, goals, enabled, ...card }) => card),
        overlaps: [],
      });
    }
    if (path.startsWith("/api/workforce/")) {
      return json(profile(path.includes("mailer") ? "mailer" : "researcher"));
    }
    if (path === "/api/workflow-suggestions") {
      return json({ available: true, suggestions: state.suggestions });
    }
    return json({});
  });
  return { state, client: new RuntimeClient(BASE, fetchImpl as never) };
}

function json(body: unknown) {
  return new Response(JSON.stringify(body), {
    status: 200,
    headers: { "Content-Type": "application/json" },
  });
}

function problem(detail: string) {
  return new Response(JSON.stringify({ detail }), {
    status: 409,
    headers: { "Content-Type": "application/json" },
  });
}

function show(client: RuntimeClient, onRecover = vi.fn()) {
  render(
    <RuntimeProvider client={client}>
      <WorkforcePage onRecover={onRecover} />
    </RuntimeProvider>,
  );
  return onRecover;
}

describe("Workforce", () => {
  it("renders readiness from the runtime and opens the stated recovery place", async () => {
    const { client } = scriptedRuntime();
    const recover = show(client);

    expect(await screen.findByRole("heading", { name: "Researcher" })).toBeInTheDocument();
    expect(screen.getByText(/No assignments in the last 30 days/)).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: /Mailer/ }));

    const detail = await screen.findByLabelText("mailer profile");
    expect(within(detail).getByText(/requires gmail/)).toBeInTheDocument();
    await userEvent.click(within(detail).getByRole("button", { name: "Open" }));
    expect(recover).toHaveBeenCalledWith("PLUGINS");
  });

  it("shows an explainable structural suggestion and dismisses it", async () => {
    const { state, client } = scriptedRuntime();
    show(client);
    await userEvent.click(await screen.findByRole("button", { name: /Workflow suggestions/ }));

    const suggestion = await screen.findByLabelText("Suggestion researcher-then-writer");
    expect(within(suggestion).getByText(/Seen in 3 successful runs/)).toBeInTheDocument();
    expect(within(suggestion).getByText(/Effects: write/)).toBeInTheDocument();
    await userEvent.click(within(suggestion).getByRole("button", { name: "Dismiss" }));

    await waitFor(() => expect(state.suggestions).toEqual([]));
    expect(await screen.findByText(/No recurring process yet/)).toBeInTheDocument();
  });

  it("keeps the confirmation open after a failed save and closes it after success", async () => {
    const { state, client } = scriptedRuntime();
    state.failSave = true;
    show(client);
    await userEvent.click(await screen.findByRole("button", { name: /Workflow suggestions/ }));
    await userEvent.click(await screen.findByRole("button", { name: "Save as workflow…" }));
    const dialog = await screen.findByRole("dialog", { name: "Save as a workflow" });

    await userEvent.click(within(dialog).getByRole("button", { name: "Save workflow" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("could not be written");
    expect(screen.getByRole("dialog", { name: "Save as a workflow" })).toBeInTheDocument();

    state.failSave = false;
    await userEvent.click(screen.getByRole("button", { name: "Save workflow" }));
    await waitFor(() => expect(screen.queryByRole("dialog", { name: "Save as a workflow" })).not.toBeInTheDocument());
    expect(state.posts.at(-1)?.body.name).toBe("researcher-then-writer");
  });
});
