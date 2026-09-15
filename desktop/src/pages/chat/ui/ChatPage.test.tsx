/**
 * The window, end to end against a scripted runtime.
 *
 * Everything below the client is real - the provider, the page's state, the
 * widgets, the features - and the only thing replaced is the HTTP the runtime
 * would have answered. That is the frontend half of Phase 13's integration
 * test; the other half runs the same flow through FastAPI, the application
 * boundary and the employee runtime in `tests/e2e/test_the_desktop_interface.py`.
 */

import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { App } from "../../../app";
import { RuntimeClient, RuntimeProvider } from "../../../shared/api";
import { ChatPage } from "./ChatPage";

const BASE = "http://127.0.0.1:9999";

/** A runtime that answers, and can be told to finish the work it was given. */
function scriptedRuntime() {
  const state = {
    answered: false,
    cancelled: false,
    title: "Sort these files",
    deleted: false,
    approvals: [] as unknown[],
    asked: [] as string[],
    directions: [] as { approvals: string; model: string }[],
  };

  const respond = (path: string, init?: RequestInit) => {
    if (path.endsWith("/api/employees")) {
      return {
        employees: [
          {
            id: "e1",
            name: "researcher",
            title: "Researcher",
            description: "Finds things out.",
            tools: [],
            limits: { max_steps: 1, max_cost_usd: 1, max_wall_time_seconds: 1 },
          },
        ],
      };
    }
    if (path.endsWith("/api/approvals")) return { approvals: state.approvals };
    if (path.endsWith("/api/workspaces")) {
      return { workspaces: [workspace("default", "Default", true)] };
    }
    if (path.endsWith("/api/conversations") && init?.method === "POST") {
      return {
        id: "c1",
        title: "",
        messages: 0,
        created_at: "2026-09-08T09:00:00+00:00",
        updated_at: "2026-09-08T09:00:00+00:00",
      };
    }
    if (path.endsWith("/messages")) {
      const body = JSON.parse(String(init?.body));
      state.asked.push(body.request);
      state.directions.push({ approvals: body.approvals, model: body.model });
      return message(false);
    }
    if (path.endsWith("/api/objectives/o1/cancel")) {
      state.cancelled = true;
      return { id: "o1", stopped: true };
    }
    if (path.endsWith("/api/providers")) {
      return {
        kinds: [],
        connections: [],
        defaults: {},
        models: [
          { name: "balanced", capabilities: ["TEXT_REASONING"] },
          { name: "vectors", capabilities: ["EMBEDDING"] },
        ],
      };
    }
    if (path.endsWith("/api/conversations") && !init?.method) {
      return {
        conversations:
          state.asked.length && !state.deleted
            ? [
                {
                  id: "c1",
                  title: state.title,
                  messages: state.asked.length,
                  status: "RUNNING",
                  created_at: new Date().toISOString(),
                  updated_at: new Date().toISOString(),
                },
              ]
            : [],
      };
    }
    if (path.endsWith("/api/conversations/c1") && init?.method === "PATCH") {
      state.title = JSON.parse(String(init.body)).title;
      return { id: "c1", title: state.title };
    }
    if (path.endsWith("/api/conversations/c1") && init?.method === "DELETE") {
      state.deleted = true;
      return { deleted: true };
    }
    if (path.endsWith("/api/conversations/c9")) {
      // A schedule's thread, set to a model the list does not offer and to refuse.
      return {
        id: "c9",
        title: "Morning research",
        created_at: "2026-09-08T09:00:00+00:00",
        updated_at: "2026-09-08T09:00:00+00:00",
        schedule_id: "s1",
        directions: { approvals: "DENY", model: "scheduled-model" },
        messages: [],
      };
    }
    if (path.includes("/api/conversations/c1")) {
      return {
        id: "c1",
        title: state.title,
        created_at: "2026-09-08T09:00:00+00:00",
        updated_at: "2026-09-08T09:00:00+00:00",
        messages: state.asked.map(() => message(state.answered)),
      };
    }
    return {};
  };

  const workspace = (id: string, name: string, active: boolean) => ({
    id,
    name,
    description: "",
    file_root: `/tmp/${id}`,
    is_default: id === "default",
    active,
    created_at: "2026-09-08T09:00:00+00:00",
  });

  const message = (answeredNow: boolean) => {
    const answered = answeredNow || state.cancelled;
    return {
      id: "o1",
      text: "Sort these files",
      status: state.cancelled ? "CANCELLED" : answered ? "DONE" : "RUNNING",
      thinking: !answered,
      answer: state.cancelled
        ? "Stopped before it was finished."
        : answered
          ? "Sorted into four folders."
          : "",
      missing: [],
      answered,
      cost_usd: 0.01,
      created_at: "2026-09-08T09:00:00+00:00",
      finished_at: answered ? "2026-09-08T09:01:00+00:00" : null,
    };
  };

  const fetchMock = vi.fn(async (url: string, init?: RequestInit) => ({
    ok: true,
    status: 200,
    statusText: "OK",
    json: async () => respond(url, init),
  }));

  return { state, fetchMock };
}

class SilentEventSource {
  onmessage: ((event: MessageEvent) => void) | null = null;
  onerror: (() => void) | null = null;
  close() {}
}

let runtime: ReturnType<typeof scriptedRuntime>;

beforeEach(() => {
  runtime = scriptedRuntime();
  vi.stubGlobal("fetch", runtime.fetchMock);
  vi.stubGlobal("EventSource", SilentEventSource);
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.useRealTimers();
});

describe("the desktop window", () => {
  it("greets, takes a request, and shows the answer the runtime produced", async () => {
    const user = userEvent.setup();
    render(<App client={new RuntimeClient(BASE)} />);

    expect(await screen.findByText("What would you like me to do?")).toBeInTheDocument();

    await user.type(
      screen.getByLabelText("Tell Prometheus what you need"),
      "Sort these files{Enter}",
    );

    expect(
      await screen.findByText("Sort these files", { selector: ".bubble" }),
    ).toBeInTheDocument();
    expect(screen.getByText("Working on it…")).toBeInTheDocument();
    expect(runtime.state.asked).toEqual(["Sort these files"]);

    // The runtime finishes. The window learns it by re-reading the thread,
    // never by deciding for itself that enough time has passed.
    runtime.state.answered = true;
    await waitFor(
      () => expect(screen.getByText("Sorted into four folders.")).toBeInTheDocument(),
      { timeout: 4000 },
    );
  });

  it("shows the workforce it was told about, and nothing it was not", async () => {
    render(<App client={new RuntimeClient(BASE)} />);

    expect(await screen.findByText("Researcher")).toBeInTheDocument();
    expect(screen.getByText("Prometheus decides who takes what.")).toBeInTheDocument();
  });

  it("puts a question waiting on the person in front of them", async () => {
    runtime.state.approvals = [
      {
        id: "a1",
        task_id: "t1",
        action: "send an email",
        risk: "HIGH",
        reason: "Sending cannot be undone.",
        payload: { to: "client@example.com" },
        requested_at: "2026-09-08T09:00:00+00:00",
        live: true,
      },
    ];

    render(<App client={new RuntimeClient(BASE)} />);

    expect(await screen.findByText("Prometheus wants to send an email")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Approve" })).toBeInTheDocument();
  });

  it("puts a question in the thread whose work asked it, and nowhere else", async () => {
    runtime.state.approvals = [
      {
        id: "a2",
        task_id: "t2",
        action: "write raw_news.txt",
        risk: "HIGH",
        reason: "Overwrite the existing file.",
        payload: { path: "raw_news.txt" },
        requested_at: "2026-09-08T09:00:00+00:00",
        live: true,
        conversation_id: "c9",
      },
    ];

    const { unmount } = render(<App client={new RuntimeClient(BASE)} />);
    expect(await screen.findByText("Researcher")).toBeInTheDocument();
    await waitFor(() => expect(screen.queryByText(/write raw_news.txt/)).not.toBeInTheDocument());
    unmount();

    render(
      <RuntimeProvider client={new RuntimeClient(BASE)}>
        <ChatPage conversationId="c9" />
      </RuntimeProvider>,
    );
    expect(await screen.findByText("Prometheus wants to write raw_news.txt")).toBeInTheDocument();
  });

  it("says so, in the runtime's own words, when the engine is not answering", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue({
        ok: false,
        status: 503,
        statusText: "Service Unavailable",
        json: async () => ({ detail: "The local database has no schema yet." }),
      }),
    );

    render(<App client={new RuntimeClient(BASE)} />);

    expect(
      await screen.findByText("The local database has no schema yet."),
    ).toBeInTheDocument();
  });

  it("stops from where send was, and the turn then reads as stopped", async () => {
    const user = userEvent.setup();
    render(<App client={new RuntimeClient(BASE)} />);
    await screen.findByText("What would you like me to do?");

    await user.type(
      screen.getByLabelText("Tell Prometheus what you need"),
      "Sort these files{Enter}",
    );
    await user.click(await screen.findByRole("button", { name: "Stop" }));

    expect(runtime.state.cancelled).toBe(true);
    expect(await screen.findByText("Stopped before it was finished.")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Stop" })).not.toBeInTheDocument();
  });

  it("sends the approvals and model chosen under the field with the request", async () => {
    const user = userEvent.setup();
    render(<App client={new RuntimeClient(BASE)} />);
    await screen.findByText("What would you like me to do?");

    await user.selectOptions(screen.getByLabelText("Approvals"), "AUTO");
    const model = await screen.findByLabelText("Model");
    expect(screen.queryByRole("option", { name: "vectors" })).not.toBeInTheDocument();
    await user.selectOptions(model, "balanced");
    await user.type(
      screen.getByLabelText("Tell Prometheus what you need"),
      "Sort these files{Enter}",
    );

    await waitFor(() =>
      expect(runtime.state.directions).toEqual([{ approvals: "AUTO", model: "balanced" }]),
    );
  });

  it("opens a thread on what it is set to, not on the defaults", async () => {
    render(
      <RuntimeProvider client={new RuntimeClient(BASE)}>
        <ChatPage conversationId="c9" />
      </RuntimeProvider>,
    );

    await waitFor(() => expect(screen.getByLabelText("Approvals")).toHaveValue("DENY"));
    expect(await screen.findByLabelText("Model")).toHaveValue("scheduled-model");
    expect(screen.getByText("scheduled-model", { selector: "b" })).toBeInTheDocument();
  });

  it("renames a task and deletes it from the list", async () => {
    const user = userEvent.setup();
    render(<App client={new RuntimeClient(BASE)} />);
    await screen.findByText("What would you like me to do?");
    await user.type(
      screen.getByLabelText("Tell Prometheus what you need"),
      "Sort these files{Enter}",
    );

    await user.click(
      await screen.findByRole(
        "button",
        { name: "Options for Sort these files" },
        { timeout: 6000 },
      ),
    );
    await user.click(screen.getByRole("menuitem", { name: "Rename" }));
    const field = screen.getByLabelText("Task name");
    await user.clear(field);
    await user.type(field, "Supplier files{Enter}");

    await waitFor(() => expect(runtime.state.title).toBe("Supplier files"));

    await user.click(screen.getByRole("button", { name: "Options for Supplier files" }));
    await user.click(screen.getByRole("menuitem", { name: "Delete" }));
    await user.click(
      within(screen.getByRole("dialog")).getByRole("button", {
        name: "Delete",
      }),
    );

    await waitFor(() => expect(runtime.state.deleted).toBe(true));
    expect(await screen.findByText("What would you like me to do?")).toBeInTheDocument();
  });
});
