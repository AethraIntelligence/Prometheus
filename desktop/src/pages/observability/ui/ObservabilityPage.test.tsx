import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { RuntimeClient, RuntimeProvider } from "../../../shared/api";
import { ObservabilityPage } from "./ObservabilityPage";

const BASE = "http://127.0.0.1:9999";
const root = "10000000-0000-0000-0000-000000000001";
const failed = "10000000-0000-0000-0000-000000000002";

const trace = {
  schema_version: 1,
  trace_id: root,
  run_kind: "TASK",
  workspace_id: "default",
  root: { type: "objective", id: root },
  degraded: false,
  degradation_reason: "",
  first_failure_span_id: failed,
  events: [
    {
      schema_version: 1, event_id: "e1", trace_id: root, span_id: root, parent_id: null,
      correlation_id: "", causation_id: null, workspace_id: "default", entity_type: "objective",
      entity_id: root, actor: "user", kind: "OBJECTIVE", status: "OK", name: "User request",
      reason_code: "DONE", started_at: "2026-09-17T10:00:00Z", ended_at: "2026-09-17T10:00:02Z",
      duration_ms: 2000, attributes: { content: "not captured" }, sequence: 1,
    },
    {
      schema_version: 1, event_id: "e2", trace_id: root, span_id: failed, parent_id: root,
      correlation_id: "", causation_id: root, workspace_id: "default", entity_type: "tool_call",
      entity_id: "7", actor: "operator", kind: "TOOL", status: "ERROR", name: "Tool fs.write",
      reason_code: "FAILED", started_at: "2026-09-17T10:00:01Z", ended_at: "2026-09-17T10:00:01Z",
      duration_ms: 20, attributes: { input: "redacted", output: "redacted" }, sequence: 2,
    },
    {
      schema_version: 1, event_id: "e3", trace_id: root, span_id: "10000000-0000-0000-0000-000000000003", parent_id: root,
      correlation_id: "", causation_id: failed, workspace_id: "default", entity_type: "task",
      entity_id: "t1", actor: "prometheus", kind: "RETRY", status: "OK", name: "Task retry",
      reason_code: "TOOL", started_at: "2026-09-17T10:00:01Z", ended_at: "2026-09-17T10:00:02Z",
      duration_ms: 1000, attributes: { attempt: 2 }, sequence: 3,
    },
  ],
};

function json(value: unknown, status = 200) {
  return new Response(JSON.stringify(value), { status, headers: { "Content-Type": "application/json" } });
}

describe("Observability", () => {
  it("shows the first causal failure, recovery, health and a reviewed export", async () => {
    const posts: unknown[] = [];
    const fetch = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const path = String(input).replace(BASE, "");
      if (init?.method === "POST") {
        posts.push(JSON.parse(String(init.body)));
        return json({ path: "/tmp/diagnostics.json" }, 201);
      }
      if (path === "/api/traces") return json([trace]);
      if (path.startsWith("/api/traces/")) return json(trace);
      if (path === "/api/diagnostics/health") return json({
        database: { status: "configured", backend: "sqlite" },
        model_providers: { status: "configured", profiles: 2 },
        sandbox: { status: "available" }, integrations: { status: "enabled" },
        scheduler: { status: "running" },
        observability: { available: true, queued: 0, dropped: 0, last_error: "", exporter: "disabled" },
        audit: { valid: true, checked: 4, first_invalid_sequence: null, reason: "" },
      });
      if (path === "/api/diagnostics/metrics") return json({ success_rate: { value: null, denominator: 0 } });
      if (path.startsWith("/api/diagnostics/bundle")) return json({ format: "prometheus-diagnostic-bundle", excluded: ["credentials", "prompt and response content"] });
      return json({});
    });
    render(<RuntimeProvider client={new RuntimeClient(BASE, fetch as never)}><ObservabilityPage /></RuntimeProvider>);

    expect(await screen.findByText("First causal failure")).toBeInTheDocument();
    expect(screen.getByText("Task retry")).toBeInTheDocument();
    expect(screen.getByText("verified · 4 records")).toBeInTheDocument();
    expect(screen.getByText(/content.*not captured/)).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Preview bundle" }));
    expect(await screen.findByText("Sanitized preview")).toBeInTheDocument();
    await userEvent.type(screen.getByLabelText("Save to path"), "/tmp/diagnostics.json");
    await userEvent.click(screen.getByRole("button", { name: "Save new file" }));
    expect(await screen.findByText("Saved to /tmp/diagnostics.json")).toBeInTheDocument();
    expect(posts).toEqual([{ path: "/tmp/diagnostics.json", trace_ids: [root] }]);
  });
});
