import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import type { Connection, InstalledModels } from "../../../entities/provider";
import { AddModelForm } from "./AddModelForm";

const KNOWN = ["TEXT_REASONING", "TOOL_CALLING", "DECISION"];

const connection = (name: string, kind: string): Connection => ({
  id: name,
  name,
  kind,
  base_url: "",
  description: "",
  needs_credential: true,
  has_key: true,
  usable: true,
});

const listing = (models: string[], runner: string): InstalledModels => ({
  models,
  supported: true,
  reachable: true,
  from_disk: false,
  runner,
  address: "https://api.typesafe.ai",
});

function form(connections: Connection[], installed: InstalledModels) {
  const onAdd = vi.fn().mockResolvedValue(undefined);
  render(
    <AddModelForm
      connections={connections}
      installed={async () => installed}
      onAdd={onAdd}
      known={KNOWN}
    />,
  );
  return onAdd;
}

describe("AddModelForm", () => {
  it("offers the decision service's own models instead of a text field", async () => {
    form(
      [connection("Jev", "typesafe")],
      listing(["jev-latest", "jev-preview"], "TypeSafe"),
    );

    const model = await screen.findByRole("combobox", { name: "Model" });
    expect(
      within(model).getByRole("option", { name: "jev-latest" }),
    ).toBeInTheDocument();
    expect(
      within(model).getByRole("option", { name: "jev-preview" }),
    ).toBeInTheDocument();
  });

  it("ticks what a decision service can be picked for, and nothing it cannot", async () => {
    form([connection("Jev", "typesafe")], listing(["jev-latest"], "TypeSafe"));

    await waitFor(() =>
      expect(screen.getByRole("checkbox", { name: "decision" })).toBeChecked(),
    );
    expect(
      screen.getByRole("checkbox", { name: "text reasoning" }),
    ).not.toBeChecked();
    expect(
      screen.getByRole("checkbox", { name: "tool calling" }),
    ).not.toBeChecked();
  });

  it("leaves a text connection with the text capabilities", async () => {
    form([connection("OpenRouter", "openrouter")], listing([], "OpenRouter"));

    await waitFor(() =>
      expect(
        screen.getByRole("checkbox", { name: "text reasoning" }),
      ).toBeChecked(),
    );
    expect(
      screen.getByRole("checkbox", { name: "decision" }),
    ).not.toBeChecked();
  });

  it("keeps a person's own ticks when the form re-renders", async () => {
    form([connection("Jev", "typesafe")], listing(["jev-latest"], "TypeSafe"));
    const decision = await screen.findByRole("checkbox", { name: "decision" });

    await userEvent.click(decision);

    await waitFor(() => expect(decision).not.toBeChecked());
  });
});
