import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import type { Document, Indexing } from "../model/types";
import { DocumentCard } from "./DocumentCard";
import { PendingDocumentCard } from "./PendingDocumentCard";

const document = (extra: Partial<Document> = {}): Document => ({
  id: "d1",
  title: "Delivery policy",
  source: "/tmp/policy.md",
  media_type: "text/markdown",
  status: "INDEXED",
  searchable: true,
  needs_indexing: false,
  chunks: 12,
  size_bytes: 100,
  error: "",
  created_at: "2026-09-13T10:00:00+00:00",
  updated_at: "2026-09-13T10:00:00+00:00",
  ...extra,
});

const indexing = (extra: Partial<Indexing> = {}): Indexing => ({
  key: "/tmp/policy.md",
  document_id: "d1",
  title: "Delivery policy",
  stage: "EMBEDDING",
  done: 30,
  total: 120,
  fraction: 0.25,
  finished: false,
  error: "",
  at: "2026-09-13T10:00:00+00:00",
  ...extra,
});

describe("DocumentCard", () => {
  it("says when it arrived, and says so again only where it was changed later", () => {
    const { rerender } = render(<DocumentCard document={document()} />);
    expect(screen.getByText(/^Added /)).toBeInTheDocument();
    expect(screen.queryByText(/updated/)).toBeNull();

    rerender(
      <DocumentCard document={document({ updated_at: "2026-09-20T09:00:00+00:00" })} />,
    );
    expect(screen.getByText(/updated/)).toBeInTheDocument();
  });

  it("draws the core's fraction while indexing, and the passage count once it is over", () => {
    const { rerender } = render(
      <DocumentCard document={document()} indexing={indexing()} />,
    );

    const bar = screen.getByRole("progressbar");
    expect(bar).toHaveAttribute("aria-valuenow", "25");
    expect(screen.getByText(/passage 31 of 120/)).toBeInTheDocument();
    expect(screen.queryByText(/12 passage\(s\)/)).toBeNull();

    rerender(<DocumentCard document={document()} />);
    expect(screen.queryByRole("progressbar")).toBeNull();
    expect(screen.getByText(/12 passage\(s\)/)).toBeInTheDocument();
  });

  it("shows a moving bar with no number where the runtime cannot count", () => {
    render(
      <DocumentCard
        document={document()}
        indexing={indexing({ stage: "READING", done: 0, total: 0, fraction: 0 })}
      />,
    );

    // No number rather than a fabricated one: a file being read reports no
    // percentage anybody should believe.
    expect(screen.getByRole("progressbar")).not.toHaveAttribute("aria-valuenow");
    expect(screen.getByText("Reading the file")).toBeInTheDocument();
  });

  it("gives a file with no record yet a row of its own", () => {
    render(
      <PendingDocumentCard
        indexing={indexing({ key: "/tmp/new.pdf", document_id: "", title: "" })}
      />,
    );
    expect(screen.getByText("new.pdf")).toBeInTheDocument();
    expect(screen.getByRole("progressbar")).toBeInTheDocument();
  });
});
