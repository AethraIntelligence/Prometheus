/**
 * Restarting against a scripted runtime.
 *
 * The runtime decides whether it can restart and says how many runs it would
 * stop; the window asks only when that number is not zero, and treats the
 * restart as done when a *new* process answers - not when any request succeeds.
 */

import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { RuntimeClient, RuntimeProvider } from "../../../shared/api";
import { RestartButton } from "./RestartButton";

const BASE = "http://127.0.0.1:9999";

function scriptedRuntime({ canRestart = true, carrying = 0, comesBackAfter = 2 } = {}) {
  const state = { restarted: false, polls: 0 };
  const fetchImpl = vi.fn(async (url: string, init?: RequestInit) => {
    const path = url.replace(BASE, "");
    if (path === "/api/runtime/restart" && init?.method === "POST") {
      state.restarted = true;
      return json({ restarting: true, stopping: carrying, started_at: "old" }, 202);
    }
    if (path === "/api/health") {
      if (state.restarted) {
        state.polls += 1;
        if (state.polls < comesBackAfter) throw new TypeError("connection refused");
        return json({ status: "ok", started_at: "new", can_restart: true, carrying: 0 });
      }
      return json({ status: "ok", started_at: "old", can_restart: canRestart, carrying });
    }
    return json({});
  });
  return { state, client: new RuntimeClient(BASE, fetchImpl as never) };
}

function json(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

function show(client: RuntimeClient, onRestarted: () => void) {
  render(
    <RuntimeProvider client={client}>
      <RestartButton onRestarted={onRestarted} pollMs={5} />
    </RuntimeProvider>,
  );
}

describe("RestartButton", () => {
  it("restarts straight away when nothing is running, and waits for the new process", async () => {
    const { client, state } = scriptedRuntime();
    const onRestarted = vi.fn();
    show(client, onRestarted);

    await userEvent.click(await screen.findByRole("button", { name: "Restart Prometheus" }));

    expect(await screen.findByRole("status")).toHaveTextContent("Restarting");
    await waitFor(() => expect(onRestarted).toHaveBeenCalledTimes(1));
    expect(state.polls).toBeGreaterThanOrEqual(2);
  });

  it("says what a restart would stop before doing it", async () => {
    const { client, state } = scriptedRuntime({ carrying: 2 });
    const onRestarted = vi.fn();
    show(client, onRestarted);

    await userEvent.click(await screen.findByRole("button", { name: "Restart Prometheus" }));

    expect(screen.getByText("2 runs in progress will be stopped.")).toBeInTheDocument();
    expect(state.restarted).toBe(false);
    await userEvent.click(screen.getByRole("button", { name: "Stop them and restart" }));
    await waitFor(() => expect(onRestarted).toHaveBeenCalled());
  });

  it("offers nothing for a runtime that cannot restart itself", async () => {
    const { client } = scriptedRuntime({ canRestart: false });
    show(client, vi.fn());

    expect(await screen.findByText(/cannot restart itself/)).toBeInTheDocument();
    expect(screen.queryByRole("button")).not.toBeInTheDocument();
  });
});
