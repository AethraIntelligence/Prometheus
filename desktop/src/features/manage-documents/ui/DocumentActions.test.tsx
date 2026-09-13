import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import type { Document } from "../../../entities/document";
import { DocumentActions } from "./DocumentActions";

const document = (extra: Partial<Document> = {}): Document => ({
  id: "d1",
  title: "Delivery policy",
  source: "/tmp/policy.md",
  media_type: "text/markdown",
  status: "INDEXED",
  searchable: true,
  needs_indexing: false,
  chunks: 1,
  size_bytes: 100,
  error: "",
  created_at: "2026-09-13T10:00:00+00:00",
  updated_at: "2026-09-13T10:00:00+00:00",
  ...extra,
});

const noop = async () => {};

describe("DocumentActions", () => {
  it("updates from a newer file, and offers re-index only where the core says it is needed", () => {
    const onUpdate = vi.fn(noop);
    const { rerender } = render(
      <DocumentActions document={document()} onUpdate={onUpdate} onReindex={noop} onRemove={noop} />,
    );

    expect(screen.queryByRole("button", { name: "Re-index" })).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "Update" }));
    expect(onUpdate).toHaveBeenCalledWith("d1");

    rerender(
      <DocumentActions
        document={document({ status: "EXTRACTED", needs_indexing: true })}
        onUpdate={onUpdate}
        onReindex={noop}
        onRemove={noop}
      />,
    );
    expect(screen.getByRole("button", { name: "Re-index" })).toBeInTheDocument();
  });
});
