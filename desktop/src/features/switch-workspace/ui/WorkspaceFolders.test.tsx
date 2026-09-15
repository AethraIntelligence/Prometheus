import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { Workspace } from "../../../entities/workspace";
import { WorkspaceFolders } from "./WorkspaceFolders";

const workspace: Workspace = {
  id: "default",
  name: "Default",
  description: "",
  file_root: "/Users/me/Documents/Prometheus",
  folders: ["/Users/me/Clients"],
  is_default: true,
  active: true,
  created_at: "2026-09-15T09:00:00+00:00",
};

afterEach(() => vi.unstubAllGlobals());

describe("WorkspaceFolders", () => {
  it("adds a folder to the ones saved, and forgets one", async () => {
    vi.stubGlobal("prompt", () => "/Users/me/Reports");
    const onChange = vi.fn();
    render(<WorkspaceFolders workspace={workspace} onChange={onChange} />);

    await userEvent.click(screen.getByRole("button", { name: "Add folder" }));
    await userEvent.click(screen.getByRole("button", { name: "Forget /Users/me/Clients" }));

    expect(onChange.mock.calls).toEqual([
      [{ folders: ["/Users/me/Clients", "/Users/me/Reports"] }],
      [{ folders: [] }],
    ]);
  });

  it("moves where the workspace keeps its files", async () => {
    vi.stubGlobal("prompt", () => "/Volumes/Work");
    const onChange = vi.fn();
    render(<WorkspaceFolders workspace={workspace} onChange={onChange} />);

    await userEvent.click(screen.getByRole("button", { name: "Change…" }));

    expect(onChange).toHaveBeenCalledWith({ file_root: "/Volumes/Work" });
  });
});
