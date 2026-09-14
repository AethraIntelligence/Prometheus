/**
 * The settings screen against a scripted runtime.
 *
 * The same shape as the workspace test: everything below the client is real,
 * and only the HTTP is replaced. What is being asserted is mostly what the
 * window does *not* do - it never decides whether a capability is safe, never
 * shows a credential, and never assumes an operation worked.
 *
 * Since the screen became one section at a time, getting to the thing under
 * test is part of the test. That is the point of the rewrite rather than an
 * inconvenience of it: what a person has to do to reach a control is exactly
 * what changed.
 */

import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { RuntimeClient, RuntimeProvider } from "../../../shared/api";
import { SettingsPage } from "./SettingsPage";

const BASE = "http://127.0.0.1:9999";

const TOOLS = [
  {
    name: "search_notes",
    qualified_name: "notes.search_notes",
    description: "Search the notes.",
    effect: "READ",
    risk: "LOW",
    requires_approval: false,
    classified: true,
  },
  {
    name: "send_note",
    qualified_name: "notes.send_note",
    description: "Send a note.",
    effect: "SEND",
    risk: "HIGH",
    requires_approval: true,
    classified: true,
  },
];

const GITHUB = {
  id: "github",
  name: "GitHub",
  description: "Repositories, issues and pull requests",
  about: "GitHub's own MCP server.",
  category: "Developer tools",
  publisher: "GitHub",
  homepage: "https://github.com/github/github-mcp-server",
  popular: true,
  runtime: "DOCKER",
  runtime_ready: true,
  icon: { view_box: "0 0 24 24", paths: ["M0 0h24v24H0z"], color: "#181717", background: "#FFFFFF" },
  capabilities: ["CODE"],
  settings: [
    {
      key: "GITHUB_PERSONAL_ACCESS_TOKEN",
      label: "Personal access token",
      kind: "SECRET",
      required: true,
      placeholder: "github_pat_...",
      help: "",
      help_url: "https://github.com/settings/personal-access-tokens/new",
      stored: false,
    },
  ],
  setup: [],
  sign_in: null,
  suggested_employees: ["analyst"],
  installed: "",
  status: "",
};

const TIME = {
  ...GITHUB,
  id: "time",
  name: "Time and time zones",
  description: "Current time anywhere",
  category: "Utilities",
  popular: false,
  runtime: "PYTHON",
  icon: null,
  settings: [],
  suggested_employees: ["analyst", "writer"],
};

const GMAIL = {
  ...GITHUB,
  id: "gmail",
  name: "Gmail",
  description: "Search, read and draft email",
  category: "Productivity",
  runtime: "PYTHON",
  settings: [
    {
      key: "GOOGLE_OAUTH_CLIENT_ID",
      label: "OAuth client ID",
      kind: "SECRET",
      required: true,
      placeholder: "",
      help: "",
      help_url: "",
      stored: false,
      provided: true,
    },
  ],
  setup: [{ text: "Create an OAuth client of type Desktop app.", url: "https://console.cloud.google.com/auth/clients" }],
  sign_in: { label: "Sign in with Google", help: "A Google page opens in your browser." },
};

const EMPLOYEES = [
  { id: "e1", name: "analyst", title: "Analyst", description: "", tools: [], integrations: [], limits: {} },
  { id: "e2", name: "writer", title: "Writer", description: "", tools: [], integrations: [], limits: {} },
];

function installedFrom(name: string, plugin: string, extra: Record<string, unknown> = {}) {
  return {
    id: "i1",
    name,
    kind: "MCP",
    status: "READY",
    enabled: true,
    usable: true,
    capabilities: [],
    secrets: [],
    tool_count: 2,
    tools: TOOLS,
    plugin,
    holders: ["analyst"],
    granted_to: ["analyst"],
    ...extra,
  };
}

function scriptedRuntime({ available = true, dockerReady = true } = {}) {
  const state = {
    installed: [] as Record<string, unknown>[],
    posted: [] as { path: string; body: unknown }[],
    put: [] as { path: string; body: unknown }[],
    deleted: [] as string[],
    signIns: 0,
  };

  const fetchImpl = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const path = String(input).replace(BASE, "");
    const body = init?.body ? JSON.parse(String(init.body)) : undefined;

    if (init?.method === "DELETE") {
      state.deleted.push(path);
      state.installed = [];
      return json({ removed: true });
    }
    if (init?.method === "PUT") {
      state.put.push({ path, body });
      state.installed = state.installed.map((one) => ({
        ...one,
        granted_to: body.employees,
        holders: body.employees,
      }));
      return json(state.installed[0]);
    }
    if (init?.method === "POST") {
      state.posted.push({ path, body });
      if (path === "/api/credentials") return json({ name: body.name, stored: true });
      if (path === "/api/plugins/gmail/install") {
        state.installed = [installedFrom("gmail", "gmail", { id: "g1" })];
        return json(state.installed[0]);
      }
      if (path.endsWith("/sign-in")) {
        state.signIns += 1;
        return json({ signed_in: state.signIns > 1 });
      }
      if (path === "/api/plugins/github/install") {
        state.installed = [installedFrom("github", "github", { granted_to: body.employees, holders: body.employees })];
        return json(state.installed[0]);
      }
      if (path === "/api/integrations") {
        state.installed = [installedFrom("notes", "", { status: "CONFIGURING", tool_count: 0, tools: [] })];
        return json(state.installed[0]);
      }
      if (path.endsWith("/connect")) {
        state.installed = state.installed.map((one) => ({ ...one, status: "READY", tool_count: 2, tools: TOOLS }));
        return json(state.installed[0]);
      }
      if (path.endsWith("/disable")) {
        state.installed = state.installed.map((one) => ({ ...one, enabled: false, usable: false, status: "DISABLED" }));
        return json(state.installed[0]);
      }
      if (path.endsWith("/enable")) {
        state.installed = state.installed.map((one) => ({ ...one, enabled: true, usable: true, status: "READY" }));
        return json(state.installed[0]);
      }
    }
    if (path === "/api/plugins") {
      if (!available) return json({ available: false, runtimes: {}, plugins: [], installed: [] });
      const githubInstalled = state.installed.find((one) => one.plugin === "github");
      return json({
        available: true,
        runtimes: {
          DOCKER: { ready: dockerReady, hint: "Needs Docker Desktop, running.", url: "https://www.docker.com/" },
          PYTHON: { ready: true, hint: "Needs uv.", url: "https://docs.astral.sh/uv/" },
        },
        plugins: [
          {
            ...GITHUB,
            runtime_ready: dockerReady,
            installed: githubInstalled ? String(githubInstalled.id) : "",
            status: githubInstalled ? String(githubInstalled.status) : "",
          },
          TIME,
          {
            ...GMAIL,
            installed: state.installed.find((one) => one.plugin === "gmail") ? "g1" : "",
          },
        ],
        installed: state.installed,
      });
    }
    if (path === "/api/employees") return json({ employees: EMPLOYEES });
    if (path === "/api/tools") return json({ tools: [] });
    if (path === "/api/workspaces") return json({ workspaces: [] });
    if (path === "/api/documents") return json({ available: true, documents: [] });
    if (path.startsWith("/api/memory")) return json({ available: true, can_forget: true, items: [] });
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

/** Render, then walk to Plugins - one click, as in the window. */
async function showPlugins(client: RuntimeClient) {
  render(
    <RuntimeProvider client={client}>
      <SettingsPage />
    </RuntimeProvider>,
  );
  await userEvent.click(await screen.findByRole("button", { name: "Plugins" }));
}

describe("Settings → Plugins", () => {
  it("lists what can be installed, grouped, with nothing connected yet", async () => {
    const { client } = scriptedRuntime();
    await showPlugins(client);

    expect(await screen.findByText("Nothing is connected yet.")).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Popular" })).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Developer tools" })).toBeInTheDocument();
    expect(screen.getAllByRole("button", { name: "Install GitHub" }).length).toBeGreaterThan(0);
  });

  it("searches the list without asking the runtime anything new", async () => {
    const { client } = scriptedRuntime();
    await showPlugins(client);
    await screen.findByText("Nothing is connected yet.");

    await userEvent.type(screen.getByRole("searchbox", { name: "Search plugins" }), "time zones");

    expect(screen.getByRole("heading", { name: /Results for/ })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Install GitHub" })).toBeNull();
    expect(screen.getByRole("button", { name: "Install Time and time zones" })).toBeInTheDocument();
  });

  it("installs a plugin from a token and the core's suggestion of who may use it", async () => {
    const { state, client } = scriptedRuntime();
    await showPlugins(client);
    await screen.findByText("Nothing is connected yet.");

    await userEvent.click(screen.getAllByRole("button", { name: "Install GitHub" })[0]);
    const dialog = screen.getByRole("dialog", { name: "GitHub" });
    const install = within(dialog).getByRole("button", { name: "Install" });
    expect(install).toBeDisabled();
    // The suggestion arrives ticked; this window did not work it out.
    expect(within(dialog).getByRole("checkbox", { name: /Analyst/ })).toBeChecked();
    expect(within(dialog).getByRole("checkbox", { name: /Writer/ })).not.toBeChecked();

    await userEvent.type(within(dialog).getByLabelText(/Personal access token/), "ghp_s3cret");
    await userEvent.click(within(dialog).getByRole("checkbox", { name: /Writer/ }));
    await userEvent.click(install);

    await waitFor(() =>
      expect(state.posted.find((call) => call.path === "/api/plugins/github/install")?.body).toEqual({
        values: { GITHUB_PERSONAL_ACCESS_TOKEN: "ghp_s3cret" },
        employees: ["analyst", "writer"],
      }),
    );
    expect(await screen.findByRole("button", { name: "Installed: GitHub" })).toBeInTheDocument();
    expect(screen.queryByRole("dialog")).toBeNull();
    expect(document.body.textContent).not.toContain("ghp_s3cret");
  });

  it("says what the machine lacks before anything is typed", async () => {
    const { client } = scriptedRuntime({ dockerReady: false });
    await showPlugins(client);
    await screen.findByText("Nothing is connected yet.");

    await userEvent.click(screen.getAllByRole("button", { name: "Install GitHub" })[0]);

    const dialog = screen.getByRole("dialog", { name: "GitHub" });
    expect(within(dialog).getByRole("alert")).toHaveTextContent("Needs Docker Desktop");
    expect(within(dialog).getByRole("button", { name: "Install" })).toBeDisabled();
  });

  it("opens an installed plugin: what it offers, who may use it, and what can be done", async () => {
    const { state, client } = scriptedRuntime();
    await showPlugins(client);
    await screen.findByText("Nothing is connected yet.");
    await userEvent.click(screen.getAllByRole("button", { name: "Install GitHub" })[0]);
    await userEvent.type(screen.getByLabelText(/Personal access token/), "ghp_x");
    await userEvent.click(screen.getByRole("button", { name: "Install" }));

    await userEvent.click(await screen.findByRole("button", { name: "Installed: GitHub" }));
    const dialog = screen.getByRole("dialog", { name: "GitHub" });
    expect(within(dialog).getByText("send_note")).toBeInTheDocument();
    expect(within(dialog).getAllByText("asks first")).toHaveLength(1);

    await userEvent.click(within(dialog).getByRole("checkbox", { name: /Writer/ }));
    await userEvent.click(within(dialog).getByRole("button", { name: "Save who can use it" }));
    await waitFor(() =>
      expect(state.put).toContainEqual({
        path: "/api/integrations/i1/grants",
        body: { employees: ["analyst", "writer"] },
      }),
    );

    await userEvent.click(within(screen.getByRole("dialog")).getByRole("button", { name: "Disable" }));
    expect(await screen.findByRole("button", { name: "Enable" })).toBeInTheDocument();
  });

  it("asks before removing, and says what removal keeps", async () => {
    const { state, client } = scriptedRuntime();
    await showPlugins(client);
    await screen.findByText("Nothing is connected yet.");
    await userEvent.click(screen.getAllByRole("button", { name: "Install GitHub" })[0]);
    await userEvent.type(screen.getByLabelText(/Personal access token/), "ghp_x");
    await userEvent.click(screen.getByRole("button", { name: "Install" }));
    await userEvent.click(await screen.findByRole("button", { name: "Installed: GitHub" }));

    await userEvent.click(screen.getByRole("button", { name: "Remove" }));
    expect(screen.getByText(/What it already did stays in the history/)).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Remove" }));

    await waitFor(() => expect(state.deleted).toHaveLength(1));
  });

  it("still adds any MCP server by hand, and never shows its credential again", async () => {
    const { state, client } = scriptedRuntime();
    await showPlugins(client);
    await screen.findByText("Nothing is connected yet.");

    await userEvent.click(screen.getByRole("button", { name: /Custom server/ }));
    await userEvent.type(screen.getByLabelText("Name"), "notes");
    await userEvent.type(screen.getByLabelText("Command"), "npx -y some-server");
    await userEvent.click(screen.getByText("Needs a credential"));
    await userEvent.type(screen.getByLabelText("Credential name"), "NOTES_TOKEN");
    await userEvent.type(screen.getByLabelText("Credential value"), "s3cret");
    await userEvent.click(screen.getByRole("button", { name: "Add" }));

    await waitFor(() =>
      expect(state.posted.some((call) => call.path.endsWith("/connect"))).toBe(true),
    );
    expect(state.posted.find((call) => call.path === "/api/integrations")?.body).toMatchObject({
      name: "notes",
      configuration: { command: "npx", args: ["-y", "some-server"] },
    });
    expect(await screen.findByRole("button", { name: "Installed: notes" })).toBeInTheDocument();
    expect(document.body.textContent).not.toContain("s3cret");
  });

  it("installs a Google plugin with one button and goes straight on to signing in", async () => {
    const { state, client } = scriptedRuntime();
    await showPlugins(client);
    await screen.findByText("Nothing is connected yet.");

    await userEvent.click(screen.getAllByRole("button", { name: "Install Gmail" })[0]);
    const dialog = screen.getByRole("dialog", { name: "Gmail" });
    // What Prometheus supplies is out of the way, with the Cloud Console steps.
    expect(within(dialog).getByText("Use your own credentials instead")).toBeInTheDocument();
    expect(within(dialog).getByLabelText("OAuth client ID")).not.toBeVisible();
    await userEvent.click(within(dialog).getByRole("button", { name: "Install" }));

    // No second button to find: the sign-in has started, and the window waits.
    expect(await screen.findByRole("button", { name: "Check" })).toBeInTheDocument();
    expect(screen.getByText(/Finish signing in/)).toBeInTheDocument();
    expect(state.signIns).toBe(1);

    await userEvent.click(screen.getByRole("button", { name: "Check" }));
    expect(await screen.findByText("Signed in")).toBeInTheDocument();
    expect(state.signIns).toBe(2);
    expect(state.posted.find((call) => call.path === "/api/plugins/gmail/install")?.body).toEqual({
      values: {},
      employees: ["analyst"],
    });
  });

  it("says so when the machine has integrations switched off", async () => {
    const { client } = scriptedRuntime({ available: false });
    await showPlugins(client);

    expect(await screen.findByText(/switched off on this machine/)).toBeInTheDocument();
  });
});

describe("Settings → Plugins → Built in", () => {
  it("shows what the machine can do and who is allowed to ask for it", async () => {
    const fetchImpl = vi.fn(async (input: RequestInfo | URL) => {
      const path = String(input).replace(BASE, "");
      if (path === "/api/tools") {
        return json({
          tools: [
            {
              name: "fs.write",
              description: "Write a file.",
              effect: "WRITE",
              risk: "HIGH",
              requires_approval: true,
              interface: "API",
              reversible: false,
              capabilities: ["FILE_ACCESS"],
              used_by: ["organizer", "writer"],
            },
          ],
        });
      }
      if (path === "/api/plugins") return json({ available: true, runtimes: {}, plugins: [], installed: [] });
      if (path === "/api/employees") return json({ employees: [] });
      return json({});
    });

    render(
      <RuntimeProvider client={new RuntimeClient(BASE, fetchImpl as never)}>
        <SettingsPage />
      </RuntimeProvider>,
    );
    await userEvent.click(await screen.findByRole("button", { name: "Plugins" }));
    await userEvent.click(await screen.findByRole("tab", { name: /Built in/ }));

    const row = await screen.findByLabelText("Tool: fs.write");
    // Who may call it is read off the declarations, and there is no switch
    // here to disagree with them.
    expect(within(row).getByText(/listed by organizer, writer/)).toBeInTheDocument();
    expect(screen.queryByRole("switch")).toBeNull();
  });
});

describe("Settings", () => {
  it("shows the runtime's own words when something fails", async () => {
    const failing = new RuntimeClient(
      BASE,
      (async () =>
        new Response(JSON.stringify({ detail: "The local database has no schema yet." }), {
          status: 503,
        })) as never,
    );
    render(
      <RuntimeProvider client={failing}>
        <SettingsPage />
      </RuntimeProvider>,
    );

    const said = await screen.findAllByRole("alert");
    expect(said[0]).toHaveTextContent("no schema yet");
  });
});
