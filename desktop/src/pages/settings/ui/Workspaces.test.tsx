/**
 * Settings → Workspaces, Documents and Memory, against a scripted runtime.
 *
 * The same shape as the plugin tests, and asserting the same kind of thing: the
 * window carries operations and renders answers. It never decides which
 * workspace is active, never works out whether a document is searchable, and
 * never offers to remove the first workspace - all three are the core's
 * answers, arriving as fields.
 */

import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { RuntimeClient, RuntimeProvider } from "../../../shared/api";
import { SettingsPage } from "./SettingsPage";

const BASE = "http://127.0.0.1:9999";

function workspace(id: string, name: string, active = false) {
  return {
    id,
    name,
    description: "",
    file_root: `/tmp/${id}`,
    is_default: id === "default",
    active,
    created_at: "2026-09-08T09:00:00+00:00",
  };
}

function document(status: string) {
  return {
    id: "d1",
    title: "Delivery policy",
    source: "/tmp/delivery.md",
    media_type: "text/markdown",
    status,
    searchable: true,
    needs_indexing: status !== "INDEXED",
    chunks: 3,
    size_bytes: 120,
    error: "",
    created_at: "2026-09-08T09:00:00+00:00",
    updated_at: "2026-09-08T09:00:00+00:00",
  };
}

function note(id: string, content: string, extra: Record<string, unknown> = {}) {
  return {
    id,
    kind: "SEMANTIC",
    scope: "WORKSPACE",
    content,
    importance: 0.5,
    created_at: "2026-09-08T09:00:00+00:00",
    expires_at: "",
    stated: false,
    ...extra,
  };
}

function scriptedRuntime({
  documents = [] as unknown[],
  memory = [] as ReturnType<typeof note>[],
  canForget = true,
} = {}) {
  const state = {
    workspaces: [workspace("default", "Default", true), workspace("work", "Work")],
    documents,
    memory,
    searched: [] as string[],
    posted: [] as { path: string; body: unknown }[],
    deleted: [] as string[],
  };

  const fetchImpl = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const path = String(input).replace(BASE, "");
    const body = init?.body ? JSON.parse(String(init.body)) : undefined;

    if (init?.method === "DELETE") {
      state.deleted.push(path);
      if (path.startsWith("/api/memory/")) {
        const id = path.split("/")[3];
        state.memory = state.memory.filter((one) => one.id !== id);
        return json({ forgotten: true });
      }
      if (path.startsWith("/api/workspaces/")) {
        state.workspaces = [workspace("default", "Default", true)];
      }
      if (path.startsWith("/api/documents/")) state.documents = [];
      return json({ removed: true });
    }
    if (init?.method === "POST") {
      state.posted.push({ path, body });
      if (path === "/api/workspaces") {
        state.workspaces = [...state.workspaces, workspace("client-a", "Client A")];
        return json(state.workspaces.at(-1));
      }
      if (path.endsWith("/use")) {
        const chosen = path.split("/")[3];
        state.workspaces = state.workspaces.map((one) => ({
          ...one,
          active: one.id === chosen,
        }));
        return json(state.workspaces.find((one) => one.active));
      }
      if (path === "/api/documents") {
        state.documents = [document("INDEXED")];
        return json(state.documents[0]);
      }
      if (path === "/api/memory") {
        const added = note("m-new", body.content, {
          scope: body.about_the_person ? "USER" : "WORKSPACE",
          stated: true,
        });
        state.memory = [added, ...state.memory];
        return json(added);
      }
      if (path.endsWith("/reindex")) {
        state.documents = [document("INDEXED")];
        return json(state.documents[0]);
      }
    }
    if (path === "/api/workspaces") return json({ workspaces: state.workspaces });
    if (path === "/api/documents") {
      return json({ available: true, documents: state.documents });
    }
    if (path.startsWith("/api/memory")) {
      // The runtime ranks a search; the script only has to show that one was asked.
      const words = new URL(`${BASE}${path}`).searchParams.get("q") ?? "";
      if (words) state.searched.push(words);
      const items = words
        ? state.memory.filter((one) => one.content.toLowerCase().includes(words.toLowerCase()))
        : state.memory;
      return json({ available: true, can_forget: canForget, items });
    }
    if (path === "/api/integrations") return json({ available: false, integrations: [] });
    if (path === "/api/tools") return json({ tools: [] });
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

function show(client: RuntimeClient, onSwitched?: () => void) {
  return render(
    <RuntimeProvider client={client}>
      <SettingsPage initial="workspaces" onSwitched={onSwitched} />
    </RuntimeProvider>,
  );
}

/** Opened on Workspaces, as a link to it would; everything else is one click away. */
async function goTo(section: string) {
  await userEvent.click(await screen.findByRole("button", { name: section }));
}

describe("Settings → Workspaces", () => {
  it("shows what exists and which one the machine is working in", async () => {
    const { client } = scriptedRuntime();
    show(client);

    expect(await screen.findByText("Default")).toBeInTheDocument();
    expect(screen.getByText("Work")).toBeInTheDocument();
    expect(screen.getByText("working here")).toBeInTheDocument();
  });

  it("never offers to remove the first workspace", async () => {
    const { client } = scriptedRuntime();
    show(client);
    await screen.findByText("Work");

    // One Remove, for the workspace that is not the first one. The rule is the
    // core's - it refuses with a 409 - and this is the window agreeing with it.
    expect(screen.getAllByRole("button", { name: "Remove" })).toHaveLength(1);
  });

  it("switches by asking the runtime, and tells the frame it happened", async () => {
    const { state, client } = scriptedRuntime();
    const switched = vi.fn();
    show(client, switched);
    await screen.findByText("Work");

    await userEvent.click(screen.getByRole("button", { name: "Work here" }));

    await waitFor(() => expect(switched).toHaveBeenCalled());
    expect(state.posted.map((call) => call.path)).toContain("/api/workspaces/work/use");
  });

  it("adds a workspace from a name, in a dialog asked for on purpose", async () => {
    const { state, client } = scriptedRuntime();
    show(client);
    await screen.findByText("Work");

    await userEvent.click(screen.getByRole("button", { name: "New workspace" }));
    await userEvent.type(screen.getByLabelText("Workspace name"), "Client A");
    await userEvent.click(screen.getByRole("button", { name: "Add workspace" }));

    await waitFor(() => expect(screen.getByText("Client A")).toBeInTheDocument());
    expect(state.posted.find((call) => call.path === "/api/workspaces")?.body).toMatchObject({
      name: "Client A",
    });
    // The dialog is gone once the runtime answered.
    expect(screen.queryByLabelText("Workspace name")).toBeNull();
  });
});

describe("Settings → Documents", () => {
  it("says nothing is here yet, and adds one by path", async () => {
    const { state, client } = scriptedRuntime();
    show(client);
    await goTo("Documents");
    expect(await screen.findByText("Nothing added yet.")).toBeInTheDocument();

    await userEvent.click(screen.getByRole("button", { name: "New document" }));
    await userEvent.type(screen.getByLabelText("Path to the document"), "/tmp/delivery.md");
    await userEvent.click(screen.getByRole("button", { name: "Add document" }));

    await waitFor(() => expect(screen.getByText("Delivery policy")).toBeInTheDocument());
    expect(state.posted.find((call) => call.path === "/api/documents")?.body).toMatchObject({
      path: "/tmp/delivery.md",
    });
  });

  it("says when a document is found by words and not yet by meaning", async () => {
    const { client } = scriptedRuntime({ documents: [document("EXTRACTED")] });
    show(client);
    await goTo("Documents");

    expect(
      await screen.findByText(/found by words, not yet by meaning/),
    ).toBeInTheDocument();
  });

  it("re-indexes on request a document the core says is missing its vectors", async () => {
    const { state, client } = scriptedRuntime({ documents: [document("EXTRACTED")] });
    show(client);
    await goTo("Documents");
    await screen.findByText("Delivery policy");

    await userEvent.click(screen.getByRole("button", { name: "Re-index" }));

    await waitFor(() =>
      expect(state.posted.map((call) => call.path)).toContain("/api/documents/d1/reindex"),
    );
  });
});

describe("Settings → Memory", () => {
  it("shows what the platform noted, and who a line is true of", async () => {
    const { client } = scriptedRuntime({
      memory: [note("m1", "The user prefers: always answer in Markdown", { scope: "USER" })],
    });
    show(client);
    await goTo("Memory");

    expect(await screen.findByText(/always answer in Markdown/)).toBeInTheDocument();
    expect(screen.getByText("you", { selector: ".badge" })).toBeInTheDocument();
  });

  it("searches by asking the runtime, not by filtering here", async () => {
    const { client, state } = scriptedRuntime({
      memory: [note("m1", "Invoices live in finance/2026"), note("m2", "Shipping stops at 14:00")],
    });
    show(client);
    await goTo("Memory");
    await screen.findByText(/Shipping stops/);

    await userEvent.type(screen.getByRole("searchbox", { name: "Search memory" }), "invoices");

    await waitFor(() => expect(screen.queryByText(/Shipping stops/)).toBeNull());
    expect(screen.getByText(/Invoices live/)).toBeInTheDocument();
    expect(state.searched).toContain("invoices");
  });

  it("adds a note of the person's own, in a dialog asked for on purpose", async () => {
    const { client, state } = scriptedRuntime();
    show(client);
    await goTo("Memory");
    expect(await screen.findByText("Nothing remembered yet.")).toBeInTheDocument();

    await userEvent.click(screen.getByRole("button", { name: "New note" }));
    await userEvent.type(
      screen.getByRole("textbox", { name: "What to remember" }),
      "Always answer in Russian",
    );
    await userEvent.click(screen.getByRole("checkbox", { name: "About me, in every workspace" }));
    await userEvent.click(screen.getByRole("button", { name: "Remember" }));

    expect(state.posted).toContainEqual({
      path: "/api/memory",
      body: { content: "Always answer in Russian", about_the_person: true },
    });
    expect(await screen.findByText("Always answer in Russian")).toBeInTheDocument();
    expect(screen.getByText(/noted by you/)).toBeInTheDocument();
    expect(screen.queryByRole("dialog")).toBeNull();
  });

  it("forgets one line only on a second, deliberate click", async () => {
    const { client, state } = scriptedRuntime({
      memory: [note("m1", "A wrong conclusion"), note("m2", "A right one")],
    });
    show(client);
    await goTo("Memory");
    await screen.findByText("A wrong conclusion");

    await userEvent.click(screen.getAllByRole("button", { name: "Forget" })[0]);
    expect(state.deleted).toEqual([]);
    await userEvent.click(screen.getByRole("button", { name: "Forget for good" }));

    await waitFor(() => expect(screen.queryByText("A wrong conclusion")).toBeNull());
    expect(state.deleted).toEqual(["/api/memory/m1"]);
    expect(screen.getByText("A right one")).toBeInTheDocument();
  });

  it("offers no way to forget where the runtime says this machine cannot", async () => {
    const { client } = scriptedRuntime({
      memory: [note("m1", "Kept either way")],
      canForget: false,
    });
    show(client);
    await goTo("Memory");

    expect(await screen.findByText("Kept either way")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /forget/i })).toBeNull();
  });
});
