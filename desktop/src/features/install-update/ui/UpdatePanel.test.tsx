/**
 * Installing an update against a scripted runtime and a scripted shell.
 *
 * The runtime decides when it is safe; the shell decides whether the package is
 * genuine. The window only carries both answers, and never installs past a
 * "not ready".
 */

import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { RuntimeClient, RuntimeProvider, type UpdateCheck } from "../../../shared/api";
import { UpdatePanel } from "./UpdatePanel";

const BASE = "http://127.0.0.1:9999";

function json(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

function runtime(readyAfter: number) {
  const calls = { prepares: 0, cancels: 0 };
  const fetchImpl = vi.fn(async (url: string, init?: RequestInit) => {
    const path = url.replace(BASE, "");
    if (path === "/api/runtime/prepare-update" && init?.method === "POST") {
      calls.prepares += 1;
      const ready = calls.prepares >= readyAfter;
      return json({
        safe: ready,
        held: ready,
        ready,
        carrying: 1,
        resumes_after_restart: true,
        in_flight: ready
          ? []
          : [{ task_id: "t", tool: "mail.send", effect: "SEND", since: "now" }],
      });
    }
    if (path === "/api/runtime/prepare-update" && init?.method === "DELETE") {
      calls.cancels += 1;
      return json({ safe: true, held: false, in_flight: [], carrying: 0 });
    }
    return json({});
  });
  return { calls, client: new RuntimeClient(BASE, fetchImpl as never) };
}

function shell(install: () => Promise<void>): () => Promise<UpdateCheck> {
  return async () => ({
    kind: "available",
    update: { version: "0.2.0", currentVersion: "0.1.0", notes: "", install },
  });
}

function show(client: RuntimeClient, props: Parameters<typeof UpdatePanel>[0]) {
  render(
    <RuntimeProvider client={client}>
      <UpdatePanel {...props} />
    </RuntimeProvider>,
  );
}

describe("UpdatePanel", () => {
  it("does not install while an effect is in flight, and names it", async () => {
    const { client } = runtime(2);
    const install = vi.fn(async () => undefined);
    const restart = vi.fn(async () => undefined);
    show(client, { find: shell(install), restart });

    await userEvent.click(await screen.findByRole("button", { name: "Check for updates" }));
    await userEvent.click(await screen.findByRole("button", { name: "Install and restart" }));

    expect(await screen.findByText("mail.send (send)")).toBeInTheDocument();
    expect(install).not.toHaveBeenCalled();
    expect(restart).not.toHaveBeenCalled();

    await userEvent.click(screen.getByRole("button", { name: "Install and restart" }));
    await waitFor(() => expect(restart).toHaveBeenCalledTimes(1));
    expect(install).toHaveBeenCalledTimes(1);
  });

  it("keeps the running version when the package fails verification", async () => {
    const { client, calls } = runtime(1);
    const install = vi.fn(async () => {
      throw new Error("signature verification failed");
    });
    const restart = vi.fn(async () => undefined);
    show(client, { find: shell(install), restart });

    await userEvent.click(await screen.findByRole("button", { name: "Check for updates" }));
    await userEvent.click(await screen.findByRole("button", { name: "Install and restart" }));

    expect(await screen.findByRole("alert")).toHaveTextContent("signature verification failed");
    expect(restart).not.toHaveBeenCalled();
    expect(calls.cancels).toBe(1);
  });

  it("says when this build has no update channel", async () => {
    const { client } = runtime(1);
    show(client, {
      find: async () => ({ kind: "unsupported", reason: "This build was made without an update channel." }),
    });

    await userEvent.click(await screen.findByRole("button", { name: "Check for updates" }));

    expect(await screen.findByText(/without an update channel/)).toBeInTheDocument();
  });
});
