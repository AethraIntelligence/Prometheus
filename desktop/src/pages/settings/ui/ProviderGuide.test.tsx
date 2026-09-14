/**
 * "Which models to use", against a scripted runtime.
 *
 * The advice is the runtime's: this asserts the window shows it, walks a person
 * through it in order and applies a setup with one request - and that whether a
 * step is done is read from what the runtime says, never guessed.
 */

import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { RuntimeClient, RuntimeProvider } from "../../../shared/api";
import { SettingsPage } from "./SettingsPage";

const BASE = "http://127.0.0.1:9999";

const KINDS = [
  { name: "router", label: "A Model Router", needs_credential: true, default_base_url: "" },
  { name: "local", label: "Local model runner", needs_credential: false, default_base_url: "" },
];

function guide(connection: string, applied: boolean) {
  return {
    intro: "Pick one of the setups below.",
    checked: "2026-09-14",
    setups: [
      {
        id: "router-free",
        title: "Start free",
        badge: "Free",
        summary: "Free models through one key.",
        kind: "router",
        connection_name: "router",
        good_for: "Trying it out.",
        connection,
        applied,
        steps: [
          { text: "Create a key.", action: "link", url: "https://example.org/keys", command: "" },
          { text: "Add the key.", action: "connect", url: "", command: "" },
          { text: "Add the models.", action: "apply", url: "", command: "" },
        ],
        models: [
          {
            name: "free-main",
            role: "Planning, acting and answering",
            model: "some-lab/some-model:free",
            why: "Strong and free.",
            capabilities: ["TOOL_CALLING"],
            context_tokens: 262144,
            free: true,
            input_cost_per_1m_usd: 0,
            output_cost_per_1m_usd: 0,
            route: ["PLANNING"],
          },
        ],
        cautions: ["Free models have a daily limit."],
      },
      {
        id: "on-machine",
        title: "Private",
        badge: "Private",
        summary: "On this machine.",
        kind: "local",
        connection_name: "local",
        good_for: "Private work.",
        connection: "",
        applied: false,
        steps: [{ text: "Download the models.", action: "", url: "", command: "pull everything" }],
        models: [],
        cautions: [],
      },
    ],
    providers: [
      { kind: "router", label: "A Model Router", verdict: "RECOMMENDED", text: "One key." },
    ],
    requirements: [{ title: "Tool calling - required", text: "Employees act by calling tools." }],
  };
}

function scriptedRuntime({ connected = false } = {}) {
  const state = { connection: connected ? "router" : "", applied: false, posted: [] as string[] };

  const fetchImpl = vi.fn(async (url: string, init?: RequestInit) => {
    const path = url.replace(BASE, "");
    const body = init?.body ? JSON.parse(String(init.body)) : undefined;
    if (init?.method === "POST") {
      state.posted.push(path);
      if (path === "/api/providers/connections") {
        state.connection = body.name;
        return json({ id: "c1", name: body.name, kind: body.kind, has_key: true, usable: true });
      }
      if (path === "/api/providers/setups/router-free/apply") {
        state.applied = true;
        return json({ added: ["free-main"] });
      }
    }
    if (path === "/api/providers") {
      return json({
        kinds: KINDS,
        connections: state.connection
          ? [{ id: "c1", name: state.connection, kind: "router", base_url: "", description: "", needs_credential: true, has_key: true, usable: true }]
          : [],
        models: [],
        defaults: {},
        guide: guide(state.connection, state.applied),
      });
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

async function show(client: RuntimeClient) {
  render(
    <RuntimeProvider client={client}>
      <SettingsPage initial="models" />
    </RuntimeProvider>,
  );
}

describe("Settings → Providers and models → Which models to use", () => {
  it("opens on a machine with nothing connected, with the free setup first", async () => {
    const { client } = scriptedRuntime();
    await show(client);

    const panel = await screen.findByRole("tabpanel", { name: "Start free" });
    expect(within(panel).getByText("some-lab/some-model:free")).toBeInTheDocument();
    expect(within(panel).getAllByText("Free").length).toBeGreaterThan(0);
    expect(within(panel).getByText("Free models have a daily limit.")).toBeInTheDocument();
    expect(within(panel).getByRole("button", { name: "Use this setup" })).toBeDisabled();
  });

  it("walks from a connection to one request that applies the setup", async () => {
    const { client, state } = scriptedRuntime();
    await show(client);

    await userEvent.click(await screen.findByRole("button", { name: "Add connection" }));
    const dialog = await screen.findByRole("dialog");
    expect(within(dialog).getByLabelText("Connection name")).toHaveValue("router");
    await userEvent.type(within(dialog).getByLabelText("API key"), "a-key");
    await userEvent.click(within(dialog).getByRole("button", { name: "Add provider" }));

    expect(await screen.findByText("Connected as “router”")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Use this setup" }));

    await waitFor(() =>
      expect(state.posted).toContain("/api/providers/setups/router-free/apply"),
    );
    expect(await screen.findByRole("button", { name: "Apply again" })).toBeInTheDocument();
  });

  it("stays folded for a machine that already has a connection, one click away", async () => {
    const { client } = scriptedRuntime({ connected: true });
    await show(client);

    const summary = await screen.findByText("Which models to use");
    expect(screen.queryByRole("tabpanel")).not.toBeVisible();
    await userEvent.click(summary);
    expect(await screen.findByRole("tabpanel", { name: "Start free" })).toBeVisible();
  });

  it("shows the other setups, provider advice and what a model needs", async () => {
    const { client } = scriptedRuntime();
    await show(client);

    await userEvent.click(await screen.findByRole("tab", { name: "Private" }));
    expect(screen.getByText("pull everything")).toBeInTheDocument();
    expect(screen.getByText("One key.")).toBeInTheDocument();
    expect(screen.getByText("Tool calling - required")).toBeInTheDocument();
  });
});
