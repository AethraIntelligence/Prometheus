import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { RuntimeClient, RuntimeProvider } from "../../../shared/api";
import { MemoryUsedButton } from "./MemoryUsedButton";

const BASE = "http://127.0.0.1:9999";

function json(value: unknown) {
  return new Response(JSON.stringify(value), {
    status: 200,
    headers: { "Content-Type": "application/json" },
  });
}

describe("Memory used", () => {
  it("asks only when opened, and shows each memory with the core's reason", async () => {
    const fetch = vi.fn(async (_url: string) =>
      json({
        objective_id: "o1",
        recorded: true,
        uses: [
          {
            memory_id: "m1",
            reader: "manager",
            reason: 'It mentions "invoices"; it belongs to this workspace.',
            weight: 0.4,
            objective_id: "o1",
            task_id: "",
            used_at: "2026-09-17T10:00:00Z",
            memory: {
              id: "m1",
              kind: "SEMANTIC",
              scope: "WORKSPACE",
              content: "Invoices live in finance/2026",
              importance: 0.8,
              created_at: "2026-09-10T08:00:00Z",
              expires_at: "",
              stated: true,
              basis: "STATED",
              confidence: 1,
              status: "ACTIVE",
              source: { kind: "PERSON", ref: "", label: "", derived_from: [] },
            },
          },
          {
            memory_id: "m2",
            reader: "task",
            reason: "It mentions \"csv\"; it belongs to this employee's own notes.",
            weight: 0.2,
            objective_id: "",
            task_id: "t1",
            used_at: "2026-09-17T10:01:00Z",
            memory: null,
          },
        ],
      }),
    );
    render(
      <RuntimeProvider client={new RuntimeClient(BASE, fetch as never)}>
        <MemoryUsedButton objectiveId="o1" />
      </RuntimeProvider>,
    );

    expect(fetch).not.toHaveBeenCalled();
    await userEvent.click(screen.getByRole("button", { name: "Memory used" }));

    expect(await screen.findByText("Invoices live in finance/2026")).toBeInTheDocument();
    expect(screen.getByText('It mentions "invoices"; it belongs to this workspace.')).toBeInTheDocument();
    expect(screen.getByText("A memory that is not shown here")).toBeInTheDocument();
    expect(String(fetch.mock.calls[0][0])).toContain("/api/objectives/o1/memory");
  });
});
