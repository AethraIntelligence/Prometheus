/**
 * The provider section of the settings screen, against a scripted runtime.
 *
 * Everything below the client is real and only the HTTP is replaced. What is
 * asserted is mostly what the window does *not* do: it never shows a key, never
 * decides whether a connection can be removed, and never works out which model
 * a kind of work would reach.
 */

import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { RuntimeClient, RuntimeProvider } from "../../../shared/api";
import { SettingsPage } from "./SettingsPage";

const BASE = "http://127.0.0.1:9999";
const KEY = "sk-live-not-a-real-key";

const KINDS = [
  { name: "hosted", label: "A Hosted Provider", needs_credential: true, default_base_url: "" },
  {
    name: "local",
    label: "Local model runner",
    needs_credential: false,
    default_base_url: "http://127.0.0.1:11434/v1",
  },
];

function scriptedRuntime({ withRunner = false, stopped = false } = {}) {
  const state = {
    connections: [] as Record<string, unknown>[],
    models: [] as Record<string, unknown>[],
    defaults: {} as Record<string, string>,
    posted: [] as { path: string; body: unknown }[],
    put: [] as { path: string; body: unknown }[],
    deleted: [] as string[],
    refuseRemoval: false,
  };

  const fetchImpl = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const path = String(input).replace(BASE, "");
    const body = init?.body ? JSON.parse(String(init.body)) : undefined;

    if (init?.method === "DELETE") {
      state.deleted.push(path);
      if (path.startsWith("/api/providers/connections")) {
        if (state.refuseRemoval) {
          return problem("'work-account' is used by fast-local. Remove them first.");
        }
        state.connections = [];
      }
      if (path.startsWith("/api/providers/models")) state.models = [];
      return json({ removed: true, defaults: state.defaults });
    }
    if (init?.method === "PUT" && path === "/api/providers/defaults") {
      state.put.push({ path, body });
      state.defaults = { ...state.defaults, [body.task_kind]: body.entry_name };
      return json({ defaults: state.defaults });
    }
    if (init?.method === "POST") {
      state.posted.push({ path, body });
      if (path === "/api/providers/connections") {
        state.connections = [
          {
            id: "c1",
            name: body.name,
            kind: body.kind,
            base_url: body.base_url ?? "",
            description: "",
            needs_credential: body.kind !== "local",
            has_key: Boolean(body.api_key),
            usable: true,
          },
        ];
        return json(state.connections[0]);
      }
      if (path === "/api/providers/models") {
        state.models = [
          {
            name: body.name,
            provider: body.provider,
            model: body.model,
            connection: body.connection,
            capabilities: body.capabilities ?? [],
            context_tokens: 8192,
            input_cost_per_1k_usd: 0,
            output_cost_per_1k_usd: 0,
            quality: 0.5,
            dimensions: 0,
            embeds: (body.capabilities ?? []).includes("EMBEDDING"),
            generates_text: !(
              (body.capabilities ?? []).length === 1 && body.capabilities[0] === "EMBEDDING"
            ),
            used_for: [],
          },
        ];
        return json(state.models[0]);
      }
    }
    if (path === "/api/providers") {
      return json({
        kinds: KINDS,
        connections: state.connections,
        models: state.models.map((entry) => ({
          ...entry,
          used_for: Object.entries(state.defaults)
            .filter(([, name]) => name === entry.name)
            .map(([kind]) => kind),
        })),
        defaults: state.defaults,
      });
    }
    if (path.includes("/installed")) {
      if (stopped) {
        return json({
          models: ["a-small-model"],
          supported: true,
          reachable: false,
          from_disk: true,
          runner: "Ollama",
          address: "http://127.0.0.1:11434/v1",
        });
      }
      return json({
        models: withRunner ? ["a-small-model", "a-large-model"] : [],
        supported: withRunner,
        reachable: withRunner,
        from_disk: false,
        runner: withRunner ? "Ollama" : "",
        address: withRunner ? "http://127.0.0.1:11434/v1" : "",
      });
    }
    if (path === "/api/integrations") return json({ available: false, integrations: [] });
    if (path === "/api/tools") return json({ tools: [] });
    if (path === "/api/workspaces") return json({ workspaces: [] });
    if (path === "/api/documents") return json({ available: false, documents: [] });
    if (path.startsWith("/api/memory")) return json({ items: [] });
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
    status: 400,
    headers: { "Content-Type": "application/json" },
  });
}

/** Render and walk to the section, the way a person reaches it. */
async function show(client: RuntimeClient) {
  render(
    <RuntimeProvider client={client}>
      <SettingsPage />
    </RuntimeProvider>,
  );
  await userEvent.click(await screen.findByRole("button", { name: "Providers and models" }));
}

/** Adding is a dialog now: the form does not stand open above the list. */
async function addProvider(name: string, { key = "", kind = "" } = {}) {
  await userEvent.click(await screen.findByRole("button", { name: "New provider" }));
  await userEvent.type(await screen.findByLabelText("Connection name"), name);
  if (kind) await userEvent.selectOptions(screen.getByLabelText("Provider"), kind);
  if (key) await userEvent.type(screen.getByLabelText("API key"), key);
  await userEvent.click(screen.getByRole("button", { name: "Add provider" }));
}

describe("Settings → Providers", () => {
  it("says when nothing has been added, and what the machine is using instead", async () => {
    const { client } = scriptedRuntime();
    await show(client);

    expect(await screen.findByText(/Nothing added/)).toBeInTheDocument();
  });

  it("offers the kinds the runtime named and never a vendor of its own", async () => {
    const { client } = scriptedRuntime();
    await show(client);
    await userEvent.click(await screen.findByRole("button", { name: "New provider" }));

    const chooser = await screen.findByLabelText("Provider");
    expect(within(chooser).getByText("A Hosted Provider")).toBeInTheDocument();
    expect(within(chooser).getByText("Local model runner")).toBeInTheDocument();
  });

  it("sends a key and never shows it again", async () => {
    const { state, client } = scriptedRuntime();
    await show(client);

    await addProvider("work-account", { key: KEY });

    await waitFor(() => expect(state.connections).toHaveLength(1));
    expect(state.posted[0].body).toMatchObject({ name: "work-account", api_key: KEY });
    expect(screen.queryByText(KEY)).not.toBeInTheDocument();
    expect(await screen.findByText(/key stored/)).toBeInTheDocument();
  });

  it("cannot be asked for a model until there is something to reach one through", async () => {
    const { client } = scriptedRuntime();
    await show(client);

    // Disabled rather than hidden: the button is where a person looks for the
    // reason, and the reason is that a model is reached through a connection.
    expect(await screen.findByRole("button", { name: "New model" })).toBeDisabled();
  });

  it("asks a local runner what it has instead of asking the person to type it", async () => {
    const { state, client } = scriptedRuntime({ withRunner: true });
    await show(client);
    await addProvider("the-runner", { kind: "local" });
    await waitFor(() => expect(state.connections).toHaveLength(1));

    await userEvent.click(await screen.findByRole("button", { name: "New model" }));

    const models = await screen.findByLabelText("Model");
    expect(within(models).getByText("a-small-model")).toBeInTheDocument();
  });

  it("still lists a stopped runner's models, and says it is not answering", async () => {
    const { state, client } = scriptedRuntime({ stopped: true });
    await show(client);
    await addProvider("the-runner", { kind: "local" });
    await waitFor(() => expect(state.connections).toHaveLength(1));

    await userEvent.click(await screen.findByRole("button", { name: "New model" }));

    const models = await screen.findByLabelText("Model");
    expect(within(models).getByText("a-small-model")).toBeInTheDocument();
    expect(await screen.findByRole("status")).toHaveTextContent(
      "Ollama is not answering at http://127.0.0.1:11434/v1",
    );
    expect(screen.getByRole("button", { name: "Check again" })).toBeInTheDocument();

    await userEvent.selectOptions(models, "a-small-model");
    expect(screen.getByLabelText("Entry name")).toHaveValue("a-small-model");
  });

  it("falls back to typing where the provider cannot be asked", async () => {
    const { state, client } = scriptedRuntime({ withRunner: false });
    await show(client);
    await addProvider("work-account", { key: KEY });
    await waitFor(() => expect(state.connections).toHaveLength(1));

    await userEvent.click(await screen.findByRole("button", { name: "New model" }));

    const model = await screen.findByLabelText("Model");
    expect(model.tagName).toBe("INPUT");
  });

  it("sends work to a model and shows what the runtime said it is used for", async () => {
    const { state, client } = scriptedRuntime({ withRunner: true });
    await show(client);
    await addProvider("the-runner", { kind: "local" });
    await waitFor(() => expect(state.connections).toHaveLength(1));

    await userEvent.click(await screen.findByRole("button", { name: "New model" }));
    await userEvent.type(await screen.findByLabelText("Entry name"), "fast-local");
    await userEvent.selectOptions(screen.getByLabelText("Model"), "a-small-model");
    await userEvent.click(screen.getByRole("button", { name: "Add model" }));
    await waitFor(() => expect(state.models).toHaveLength(1));

    await userEvent.selectOptions(
      await screen.findByLabelText("Model for planning"),
      "fast-local",
    );

    await waitFor(() => expect(state.defaults.PLANNING).toBe("fast-local"));
    // On the model itself, not merely in the routing table: this is the window
    // rendering what the core said the entry is used for, rather than the
    // choice it just made.
    const entry = await screen.findByLabelText("Model: fast-local");
    expect(within(entry).getByText("planning")).toBeInTheDocument();
  });

  it("shows the runtime's own refusal rather than deciding for itself", async () => {
    const { state, client } = scriptedRuntime();
    state.refuseRemoval = true;
    await show(client);
    await addProvider("work-account", { key: KEY });
    await screen.findByText(/key stored/);

    await userEvent.click(screen.getByRole("button", { name: "Remove" }));

    expect(await screen.findByRole("alert")).toHaveTextContent("is used by fast-local");
  });
});
