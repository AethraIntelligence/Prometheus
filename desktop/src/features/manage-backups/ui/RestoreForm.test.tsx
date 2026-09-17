/**
 * Restoring against a scripted runtime: nothing is restored that the runtime
 * has not checked first, a backup with sealed credentials asks for its
 * passphrase, and the window waits for a new process before it reloads.
 */

import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { RuntimeClient } from "../../../shared/api";
import { useBackups } from "../model/useBackups";
import { RestoreForm } from "./RestoreForm";

const BASE = "http://127.0.0.1:9999";

function json(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

function scripted({ secrets = false, corrupt = false } = {}) {
  const seen: string[] = [];
  let restarted = false;
  const summary = {
    path: "/backups/one.zip",
    created_at: "2026-09-17T12:00:00Z",
    app_version: "0.1.0",
    schema_revision: "042",
    includes_secrets: secrets,
    bytes: 1024,
    entries: { DATABASE: 1 },
  };
  const fetchImpl = vi.fn(async (url: string, init?: RequestInit) => {
    const path = url.replace(BASE, "");
    seen.push(`${init?.method ?? "GET"} ${path}`);
    if (path === "/api/backups/verify") {
      if (corrupt) return json({ detail: "data/settings.json in the backup does not match its checksum." }, 400);
      return json(summary);
    }
    if (path === "/api/runtime/restore") {
      restarted = true;
      return json({ restoring: true, stopping: 0, backup: summary }, 202);
    }
    if (path === "/api/health") return json({ status: "ok", started_at: restarted ? "new" : "old" });
    return json({});
  });
  return { seen, client: new RuntimeClient(BASE, fetchImpl as never) };
}

function Harness({ client, onRestored }: { client: RuntimeClient; onRestored: () => void }) {
  const backups = useBackups(client, { onRestored, pollMs: 5 });
  return (
    <>
      {backups.problem && <p role="alert">{backups.problem}</p>}
      <RestoreForm
        checked={backups.checked}
        disabled={backups.busy}
        onCheck={backups.check}
        onRestore={backups.restore}
      />
    </>
  );
}

describe("RestoreForm", () => {
  it("checks the whole backup before offering to restore it", async () => {
    const { client, seen } = scripted();
    const onRestored = vi.fn();
    render(<Harness client={client} onRestored={onRestored} />);

    await userEvent.type(screen.getByRole("textbox"), "/backups/one.zip");
    await userEvent.click(screen.getByRole("button", { name: "Check backup" }));
    expect(await screen.findByRole("status")).toHaveTextContent("Complete and unchanged");
    expect(seen.some((line) => line.includes("/api/runtime/restore"))).toBe(false);

    await userEvent.click(screen.getByRole("button", { name: "Restore and restart" }));
    await waitFor(() => expect(onRestored).toHaveBeenCalledTimes(1));
  });

  it("asks for the passphrase of sealed credentials", async () => {
    const { client } = scripted({ secrets: true });
    render(<Harness client={client} onRestored={vi.fn()} />);

    await userEvent.type(screen.getByRole("textbox"), "/backups/one.zip");
    await userEvent.click(screen.getByRole("button", { name: "Check backup" }));

    const restore = await screen.findByRole("button", { name: "Restore and restart" });
    expect(restore).toBeDisabled();
    await userEvent.click(screen.getByRole("checkbox"));
    expect(restore).toBeEnabled();
  });

  it("shows the runtime's refusal and offers no restore for a damaged backup", async () => {
    const { client, seen } = scripted({ corrupt: true });
    render(<Harness client={client} onRestored={vi.fn()} />);

    await userEvent.type(screen.getByRole("textbox"), "/backups/one.zip");
    await userEvent.click(screen.getByRole("button", { name: "Check backup" }));

    expect(await screen.findByRole("alert")).toHaveTextContent("does not match its checksum");
    expect(screen.queryByRole("button", { name: "Restore and restart" })).not.toBeInTheDocument();
    expect(seen.some((line) => line.includes("/api/runtime/restore"))).toBe(false);
  });
});
