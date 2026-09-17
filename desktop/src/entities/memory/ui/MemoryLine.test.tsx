import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import type { MemoryItem } from "../model/types";
import { MemoryLine, provenance } from "./MemoryLine";

function item(overrides: Partial<MemoryItem> = {}): MemoryItem {
  return {
    id: "m1",
    kind: "EPISODIC",
    scope: "WORKSPACE",
    content: "Invoices live in finance/2026",
    importance: 0.7,
    created_at: "2026-09-10T08:00:00Z",
    expires_at: "",
    stated: false,
    basis: "REPORTED",
    factual: false,
    confidence: 0.75,
    status: "ACTIVE",
    source: { kind: "TASK", ref: "t1", label: "Sort the invoices", derived_from: [] },
    ...overrides,
  };
}

describe("a remembered line", () => {
  it("says what it rests on: basis, source, confidence", () => {
    render(
      <ul>
        <MemoryLine item={item()} />
      </ul>,
    );

    expect(screen.getByText("Invoices live in finance/2026")).toBeInTheDocument();
    expect(
      screen.getByText("reported · a task: Sort the invoices · confidence 75%"),
    ).toBeInTheDocument();
  });

  it("marks what was replaced or is disputed, and when it will be dropped", () => {
    expect(provenance(item({ status: "SUPERSEDED" }))).toContain("replaced");
    expect(provenance(item({ status: "CONTESTED", basis: "INFERRED" }))).toBe(
      "assumption · a task: Sort the invoices · confidence 75% · disputed",
    );
    expect(provenance(item({ expires_at: "2026-10-01T00:00:00Z" }))).toContain(
      "kept until 2026-10-01",
    );
  });

  it("renders a line from a runtime that predates provenance", () => {
    const old = item();
    delete old.basis;
    render(
      <ul>
        <MemoryLine item={old} />
      </ul>,
    );
    expect(screen.queryByText(/confidence/)).not.toBeInTheDocument();
  });
});
