/**
 * Scheduled work against a scripted runtime.
 *
 * What is asserted is what the window does not do: it never decides a schedule
 * is valid, never shows a next run the runtime will not keep, and never runs
 * anything itself - "Run now" is a request to the runtime, and its answer is a
 * thread the frame is told to open.
 */

import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import type { Schedule } from "../../../entities/schedule";
import { RuntimeClient, RuntimeProvider } from "../../../shared/api";
import { SchedulesPage } from "./SchedulesPage";

const BASE = "http://127.0.0.1:9999";

function made(overrides: Partial<Schedule> = {}): Schedule {
  return {
    id: "s1",
    name: "Morning digest",
    request: "Summarise yesterday's notes",
    enabled: true,
    every_seconds: null,
    daily_at_utc: "06:30",
    on_event: "",
    next_due_at: "2026-09-15T06:30:00+00:00",
    last_run_at: null,
    last_status: null,
    runs: 0,
    conversation_id: "t1",
    model: "",
    approvals: "ASK",
    created_at: "2026-09-14T09:00:00+00:00",
    ...overrides,
  };
}

function scriptedRuntime({ running = true, saved = false, locked = "", existing = [] as unknown[] } = {}) {
  const state = {
    schedules: [...existing] as Schedule[],
    running,
    saved,
    posted: [] as { path: string; body: unknown }[],
    put: [] as { path: string; body: Record<string, unknown> }[],
  };

  const fetchImpl = vi.fn(async (url: string, init?: RequestInit) => {
    const path = url.replace(BASE, "");
    const body = init?.body ? JSON.parse(String(init.body)) : undefined;
    const method = init?.method ?? "GET";
    if (method === "POST" && path === "/api/schedules") {
      state.posted.push({ path, body });
      const created = made({ id: "s2", name: body.name, request: body.request });
      state.schedules.push(created);
      return json(created);
    }
    if (method === "POST" && path.endsWith("/run")) {
      state.posted.push({ path, body });
      return json({ id: "o1", conversation_id: "t1" });
    }
    if (method === "PUT" && path.startsWith("/api/schedules/")) {
      state.put.push({ path, body });
      state.schedules = state.schedules.map((one) => ({
        ...one,
        request: body.request,
        name: body.name,
        model: body.model,
        every_seconds: body.every_minutes ? body.every_minutes * 60 : null,
        daily_at_utc: body.daily_at ? one.daily_at_utc : null,
      }));
      return json(state.schedules[0]);
    }
    if (method === "PATCH") {
      state.schedules = state.schedules.map((one) => ({ ...one, enabled: body.enabled }));
      return json(state.schedules[0]);
    }
    if (method === "DELETE") {
      state.schedules = [];
      return json({ removed: true });
    }
    if (method === "PUT" && path === "/api/settings") {
      state.saved = true;
      return json({ settings: [] });
    }
    if (path === "/api/providers") {
      return json({
        models: [
          { name: "reliable", model: "some-lab/paid-model", generates_text: true },
          { name: "embedder", model: "some-embedder", generates_text: false },
        ],
      });
    }
    if (path === "/api/schedules") {
      return json({ available: true, running: state.running, schedules: state.schedules });
    }
    if (path === "/api/settings") {
      return json({
        settings: [{ key: "flags.scheduler", value: state.saved, locked_by: locked }],
      });
    }
    if (path === "/api/conversations/t9") {
      return json({
        id: "t9",
        title: "Weekly report",
        messages: [{ id: "m1", text: "Write the weekly report" }],
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

function show(client: RuntimeClient, props: Parameters<typeof SchedulesPage>[0] = {}) {
  render(
    <RuntimeProvider client={client}>
      <SchedulesPage {...props} />
    </RuntimeProvider>,
  );
}

describe("Scheduled", () => {
  it("offers a first schedule when there is none", async () => {
    const { client } = scriptedRuntime();
    show(client);

    expect(await screen.findByText("Nothing is scheduled yet.")).toBeInTheDocument();
    expect(screen.queryByRole("status")).not.toBeInTheDocument();
  });

  it("makes a daily schedule on the person's clock and lists it", async () => {
    const { client, state } = scriptedRuntime();
    show(client);

    await userEvent.click((await screen.findAllByRole("button", { name: "New schedule" }))[0]);
    const dialog = await screen.findByRole("dialog");
    await userEvent.type(within(dialog).getByLabelText("What to ask"), "Summarise yesterday's notes");
    await userEvent.type(within(dialog).getByLabelText(/Name/), "Morning digest");
    await userEvent.click(within(dialog).getByRole("button", { name: "Schedule" }));

    await waitFor(() => expect(state.posted).toHaveLength(1));
    const sent = state.posted[0].body as Record<string, unknown>;
    expect(sent).toMatchObject({ request: "Summarise yesterday's notes", daily_at: "09:00" });
    expect(sent.utc_offset_minutes).toBe(0 - new Date().getTimezoneOffset());
    expect(sent).not.toHaveProperty("every_minutes");
    expect(await screen.findByText("Morning digest")).toBeInTheDocument();
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  });

  it("runs now into the schedule's thread and opens it", async () => {
    const { client, state } = scriptedRuntime({ existing: [made()] });
    const onOpenThread = vi.fn();
    show(client, { onOpenThread });

    await userEvent.click(await screen.findByRole("button", { name: "Run Morning digest now" }));

    await waitFor(() => expect(onOpenThread).toHaveBeenCalledWith("t1"));
    expect(state.posted.map((one) => one.path)).toEqual(["/api/schedules/s1/run"]);
  });

  it("pauses, and deletes only on the second click", async () => {
    const { client, state } = scriptedRuntime({ existing: [made()] });
    show(client);

    await userEvent.click(await screen.findByRole("button", { name: "Pause Morning digest" }));
    expect(await screen.findByText("Paused")).toBeInTheDocument();

    await userEvent.click(screen.getByRole("button", { name: "Delete Morning digest" }));
    expect(state.schedules).toHaveLength(1);
    await userEvent.click(screen.getByRole("button", { name: "Delete schedule" }));
    expect(await screen.findByText("Nothing is scheduled yet.")).toBeInTheDocument();
  });

  it("says when nothing will fire, hides the next run, and turns the scheduler on", async () => {
    const { client, state } = scriptedRuntime({ running: false, existing: [made()] });
    show(client);

    const notice = await screen.findByRole("status");
    expect(notice).toHaveTextContent("Scheduled work is off");
    expect(screen.queryByText(/· next /)).not.toBeInTheDocument();

    await userEvent.click(within(notice).getByRole("button", { name: "Turn on" }));

    await waitFor(() => expect(state.saved).toBe(true));
    expect(await screen.findByText(/Restart it to begin/)).toBeInTheDocument();
  });

  it("does not offer a switch the environment holds", async () => {
    const { client } = scriptedRuntime({ running: false, locked: "PROMETHEUS_FLAGS__SCHEDULER" });
    show(client);

    const notice = await screen.findByRole("status");
    expect(notice).toHaveTextContent("PROMETHEUS_FLAGS__SCHEDULER");
    expect(within(notice).queryByRole("button")).not.toBeInTheDocument();
  });

  it("repeats a thread: its first request and title arrive filled in", async () => {
    const { client, state } = scriptedRuntime();
    const onRepeatTaken = vi.fn();
    show(client, { repeat: "t9", onRepeatTaken });

    const dialog = await screen.findByRole("dialog", { name: "Repeat on a schedule" });
    expect(within(dialog).getByLabelText("What to ask")).toHaveValue("Write the weekly report");
    expect(onRepeatTaken).toHaveBeenCalled();

    await userEvent.click(within(dialog).getByRole("button", { name: "Schedule" }));
    await waitFor(() =>
      expect(state.posted[0]?.body).toMatchObject({ conversation_id: "t9" }),
    );
  });

  it("chooses the model a new schedule's runs prefer, from models that can write", async () => {
    const { client, state } = scriptedRuntime();
    show(client);

    await userEvent.click((await screen.findAllByRole("button", { name: "New schedule" }))[0]);
    const dialog = await screen.findByRole("dialog");
    const choice = within(dialog).getByLabelText("Model");
    await waitFor(() =>
      expect(within(choice).getByRole("option", { name: /reliable/ })).toBeInTheDocument(),
    );
    expect(within(choice).queryByRole("option", { name: /embedder/ })).not.toBeInTheDocument();

    await userEvent.type(within(dialog).getByLabelText("What to ask"), "Check the news");
    await userEvent.selectOptions(choice, "reliable");
    await userEvent.click(within(dialog).getByRole("button", { name: "Schedule" }));

    await waitFor(() => expect(state.posted[0]?.body).toMatchObject({ model: "reliable" }));
  });

  it("edits a schedule in place: its own words, timing and model arrive filled in", async () => {
    const { client, state } = scriptedRuntime({
      existing: [made({ every_seconds: 10800, daily_at_utc: null, model: "reliable" })],
    });
    show(client);

    await userEvent.click(await screen.findByRole("button", { name: "Edit Morning digest" }));
    const dialog = await screen.findByRole("dialog", { name: "Edit schedule" });
    expect(within(dialog).getByLabelText("What to ask")).toHaveValue("Summarise yesterday's notes");
    expect(within(dialog).getByLabelText("How many")).toHaveValue(3);
    expect(within(dialog).getByLabelText("Unit")).toHaveValue("hours");
    await waitFor(() => expect(within(dialog).getByLabelText("Model")).toHaveValue("reliable"));

    const words = within(dialog).getByLabelText("What to ask");
    await userEvent.clear(words);
    await userEvent.type(words, "Check 10 news in IT and AI");
    await userEvent.selectOptions(within(dialog).getByLabelText("Model"), "");
    expect(within(dialog).getByLabelText("Approvals")).toHaveValue("ASK");
    await userEvent.selectOptions(within(dialog).getByLabelText("Approvals"), "DENY");
    await userEvent.click(within(dialog).getByRole("button", { name: "Save changes" }));

    await waitFor(() => expect(state.put).toHaveLength(1));
    expect(state.put[0].path).toBe("/api/schedules/s1");
    expect(state.put[0].body).toMatchObject({
      request: "Check 10 news in IT and AI",
      every_minutes: 180,
      model: "",
      approvals: "DENY",
    });
    expect(await screen.findByText("Check 10 news in IT and AI")).toBeInTheDocument();
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  });
});
