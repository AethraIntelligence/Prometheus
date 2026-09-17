import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { RuntimeClient, RuntimeProvider } from "../../../shared/api";
import { ThreadBrief } from "./ThreadBrief";

const BASE = "http://127.0.0.1:9999";

function brief(open: string[]) {
  return {
    conversation_id: "c1",
    goal: "Build the Q3 report",
    decisions: [{ text: "format: CSV", objective_id: "o1", recorded_at: "2026-09-17T10:00:00Z" }],
    open_questions: open.map((text) => ({
      text,
      objective_id: "o2",
      recorded_at: "2026-09-17T10:05:00Z",
    })),
    artifacts: [{ path: "reports/q3.csv", objective_id: "o1", recorded_at: "2026-09-17T10:00:00Z" }],
    stages: [
      {
        index: 1,
        summary: "Chose CSV and wrote the first draft.",
        objective_ids: ["o0", "o1"],
        started_at: "2026-09-16T10:00:00Z",
        ended_at: "2026-09-16T11:00:00Z",
        compacted_at: "2026-09-17T09:00:00Z",
        summarised: true,
      },
    ],
    total_turns: 9,
    compacted_turns: 2,
    recent_turns: 6,
  };
}

function json(value: unknown) {
  return new Response(JSON.stringify(value), {
    status: 200,
    headers: { "Content-Type": "application/json" },
  });
}

describe("thread brief", () => {
  it("shows what the thread established and settles an open question", async () => {
    const fetch = vi.fn(async (_url: string, init?: RequestInit) =>
      json(init?.method === "POST" ? brief([]) : brief(["the Q3 totals"])),
    );
    render(
      <RuntimeProvider client={new RuntimeClient(BASE, fetch as never)}>
        <ThreadBrief conversationId="c1" />
      </RuntimeProvider>,
    );

    await userEvent.click(screen.getByRole("button", { name: "Brief" }));

    expect(await screen.findByText("format: CSV")).toBeInTheDocument();
    expect(screen.getByText("reports/q3.csv")).toBeInTheDocument();
    expect(screen.getByText("Chose CSV and wrote the first draft.")).toBeInTheDocument();
    expect(screen.getByText("the Q3 totals")).toBeInTheDocument();

    await userEvent.click(screen.getByRole("button", { name: "Settled" }));

    expect(await screen.findByText("Nothing is waiting on an answer.")).toBeInTheDocument();
  });
});
