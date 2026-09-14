/**
 * Settings -> General against a scripted runtime.
 *
 * What is asserted is what the window does not do: it never decides a value is
 * acceptable, never claims a change is already running, and never offers a
 * switch the environment holds.
 */

import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import type { Setting } from "../../../entities/setting";
import { RuntimeClient, RuntimeProvider } from "../../../shared/api";
import { SettingsPage } from "./SettingsPage";

const BASE = "http://127.0.0.1:9999";

function setting(overrides: Partial<Setting>): Setting {
  return {
    key: "flags.scheduler",
    group: "Capabilities",
    label: "Scheduled work",
    help: "Schedules start work on their own.",
    kind: "BOOLEAN",
    value: false,
    running: false,
    default: false,
    saved: false,
    choices: [],
    minimum: null,
    optional: false,
    locked_by: "",
    restart_needed: false,
    ...overrides,
  };
}

function scriptedRuntime({ refuse = "" } = {}) {
  const state = {
    settings: [
      setting({}),
      setting({
        key: "flags.memory",
        label: "Memory",
        value: true,
        running: true,
        locked_by: "PROMETHEUS_FLAGS__MEMORY",
      }),
      setting({
        key: "approval_mode",
        group: "Approvals",
        label: "When nobody can be asked",
        kind: "CHOICE",
        value: "deny",
        running: "prompt",
        default: "prompt",
        saved: true,
        restart_needed: true,
        choices: ["prompt", "deny", "allow"],
      }),
      setting({
        key: "computer_max_actions",
        group: "Desktop control",
        label: "Actions per run",
        kind: "INTEGER",
        value: 200,
        running: 200,
        default: 200,
        minimum: 1,
      }),
    ],
    sent: [] as Record<string, unknown>[],
    resets: [] as (string[] | null)[],
  };

  const listing = () =>
    json({
      available: true,
      restart_needed: state.settings.some((one) => one.restart_needed),
      any_saved: state.settings.some((one) => one.saved),
      settings: state.settings,
    });

  const fetchImpl = vi.fn(async (url: string, init?: RequestInit) => {
    const path = url.replace(BASE, "");
    if (path === "/api/settings" && init?.method === "PUT") {
      const { values } = JSON.parse(String(init.body));
      state.sent.push(values);
      if (refuse) {
        return new Response(JSON.stringify({ detail: refuse }), {
          status: 400,
          headers: { "Content-Type": "application/json" },
        });
      }
      state.settings = state.settings.map((one) =>
        one.key in values
          ? {
              ...one,
              value: values[one.key],
              saved: true,
              restart_needed: values[one.key] !== one.running,
            }
          : one,
      );
      return listing();
    }
    if (path === "/api/settings/reset") {
      const { keys } = JSON.parse(String(init?.body));
      state.resets.push(keys);
      state.settings = state.settings.map((one) =>
        keys === null || keys.includes(one.key)
          ? { ...one, value: one.default, saved: false, restart_needed: one.default !== one.running }
          : one,
      );
      return listing();
    }
    if (path === "/api/settings") return listing();
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

function show(client: RuntimeClient) {
  render(
    <RuntimeProvider client={client}>
      <SettingsPage />
    </RuntimeProvider>,
  );
}

describe("Settings → General", () => {
  it("opens first, grouped as the runtime groups them", async () => {
    const { client } = scriptedRuntime();
    show(client);

    expect(await screen.findByRole("heading", { name: "Capabilities" })).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Approvals" })).toBeInTheDocument();
    expect(screen.getByRole("switch", { name: "Scheduled work" })).not.toBeChecked();
  });

  it("saves a switch and says a restart is still owed", async () => {
    const { client, state } = scriptedRuntime();
    show(client);

    await userEvent.click(await screen.findByRole("switch", { name: "Scheduled work" }));

    await waitFor(() => expect(state.sent).toEqual([{ "flags.scheduler": true }]));
    expect(await screen.findByRole("status")).toHaveTextContent("Restart Prometheus");
    expect(screen.getByRole("switch", { name: "Scheduled work" })).toBeChecked();
    // Once in the note that explains the mark, once on each setting that carries it.
    expect(screen.getAllByText("after restart")).toHaveLength(3);
  });

  it("does not offer what the environment decides", async () => {
    const { client } = scriptedRuntime();
    show(client);

    expect(await screen.findByRole("switch", { name: "Memory" })).toBeDisabled();
    expect(screen.getByText("PROMETHEUS_FLAGS__MEMORY")).toBeInTheDocument();
  });

  it("sends a number when the field is left, and a choice at once", async () => {
    const { client, state } = scriptedRuntime();
    show(client);

    const field = await screen.findByLabelText("Actions per run");
    await userEvent.clear(field);
    await userEvent.type(field, "50{Enter}");
    await userEvent.selectOptions(screen.getByLabelText("When nobody can be asked"), "allow");

    await waitFor(() =>
      expect(state.sent).toEqual([{ computer_max_actions: 50 }, { approval_mode: "allow" }]),
    );
  });

  it("shows the runtime's refusal rather than deciding for it", async () => {
    const { client } = scriptedRuntime({ refuse: "Actions per run cannot be less than 1." });
    show(client);

    const field = await screen.findByLabelText("Actions per run");
    await userEvent.clear(field);
    await userEvent.type(field, "0{Enter}");

    expect(await screen.findByRole("alert")).toHaveTextContent("cannot be less than 1");
    await waitFor(() => expect(screen.getByLabelText("Actions per run")).toHaveValue(200));
  });

  it("resets one saved setting, and says what it resets to", async () => {
    const { client, state } = scriptedRuntime();
    show(client);

    const reset = await screen.findByRole("button", {
      name: "Reset When nobody can be asked to default",
    });
    expect(reset).toHaveTextContent("prompt");
    expect(
      screen.queryByRole("button", { name: "Reset Scheduled work to default" }),
    ).not.toBeInTheDocument();

    await userEvent.click(reset);

    await waitFor(() => expect(state.resets).toEqual([["approval_mode"]]));
    expect(screen.getByLabelText("When nobody can be asked")).toHaveValue("prompt");
  });

  it("resets everything only on the second click", async () => {
    const { client, state } = scriptedRuntime();
    show(client);

    await userEvent.click(await screen.findByRole("button", { name: "Reset all to defaults" }));
    expect(state.resets).toEqual([]);
    await userEvent.click(screen.getByRole("button", { name: "Reset every setting" }));

    await waitFor(() => expect(state.resets).toEqual([null]));
    expect(await screen.findByRole("button", { name: "Reset all to defaults" })).toBeDisabled();
  });
});
