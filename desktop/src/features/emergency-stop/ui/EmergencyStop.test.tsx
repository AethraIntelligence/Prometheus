/**
 * The brake against a scripted runtime: one press in settings, a banner on
 * every screen that says what stopping does not mean, and a stop set elsewhere
 * showing up without a click.
 */

import { act, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { RuntimeClient, RuntimeProvider } from "../../../shared/api";
import { EmergencyStop } from "./EmergencyStop";

const BASE = "http://127.0.0.1:9999";

function json(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

function scriptedRuntime() {
  const state = { engaged: false, reason: "", stops: 0, resumes: 0 };
  const fetchImpl = vi.fn(async (url: string, init?: RequestInit) => {
    const path = url.replace(BASE, "");
    const view = () => ({ engaged: state.engaged, reason: state.reason, available: true });
    if (path === "/api/runtime/stop" && init?.method === "POST") {
      state.engaged = true;
      state.reason = "a person stopped all work";
      state.stops += 1;
      return json({ state: view(), approvals_released: 1, failures: [] });
    }
    if (path === "/api/runtime/resume" && init?.method === "POST") {
      state.engaged = false;
      state.resumes += 1;
      return json({ state: view(), failures: [] });
    }
    if (path === "/api/runtime/stop") return json(view());
    return json({});
  });
  return { state, client: new RuntimeClient(BASE, fetchImpl as never) };
}

function show(
  client: RuntimeClient,
  pollMs = 10_000,
  placement: "banner" | "section" = "section",
) {
  render(
    <RuntimeProvider client={client}>
      <EmergencyStop pollMs={pollMs} placement={placement} />
    </RuntimeProvider>,
  );
}

describe("EmergencyStop", () => {
  it("stops everything in one press, without asking", async () => {
    const { client, state } = scriptedRuntime();
    show(client);

    await userEvent.click(await screen.findByRole("button", { name: "Stop all work" }));

    expect(state.stops).toBe(1);
    const stopped = await screen.findByRole("alert");
    expect(stopped).toHaveTextContent("All work is stopped.");
    expect(stopped).toHaveTextContent("were not undone");
  });

  it("resumes only when asked, and the button comes back", async () => {
    const { client, state } = scriptedRuntime();
    state.engaged = true;
    state.reason = "from the terminal";
    show(client);

    expect(await screen.findByText("from the terminal")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Resume work" }));

    expect(state.resumes).toBe(1);
    expect(await screen.findByRole("button", { name: "Stop all work" })).toBeInTheDocument();
  });

  it("shows a stop set by another process without anybody clicking", async () => {
    const { client, state } = scriptedRuntime();
    show(client, 20);
    await screen.findByRole("button", { name: "Stop all work" });

    await act(async () => {
      state.engaged = true;
      state.reason = "stopped from the menu";
    });

    await waitFor(() => expect(screen.getByRole("alert")).toHaveTextContent("stopped from the menu"));
  });

  it("offers no press outside settings, and announces a stop there anyway", async () => {
    const { client, state } = scriptedRuntime();
    show(client, 20, "banner");

    await waitFor(() => expect(screen.queryByRole("button", { name: "Stop all work" })).toBeNull());

    await act(async () => {
      state.engaged = true;
      state.reason = "stopped from the terminal";
    });

    await waitFor(() =>
      expect(screen.getByRole("alert")).toHaveTextContent("stopped from the terminal"),
    );
  });
});
