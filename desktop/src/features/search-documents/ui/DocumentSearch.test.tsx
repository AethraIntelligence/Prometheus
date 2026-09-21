import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { DocumentSearch } from "./DocumentSearch";

const client = (passages: unknown[]) =>
  ({
    get: vi.fn(async () => ({ passages })),
  }) as never;

const passage = (extra = {}) => ({
  document_id: "d1",
  title: "Delivery policy",
  source: "/tmp/policy.md",
  content: "Domestic delivery takes five working days.",
  score: 0.82,
  lexical: 0.4,
  semantic: 1.0,
  ...extra,
});

describe("DocumentSearch", () => {
  it("shows what was retrieved, with its source and how it was found", async () => {
    render(<DocumentSearch client={client([passage()])} />);

    fireEvent.change(screen.getByLabelText("Ask these documents something"), {
      target: { value: "how long is delivery?" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Search" }));

    await waitFor(() =>
      expect(
        screen.getByText("Domestic delivery takes five working days."),
      ).toBeInTheDocument(),
    );
    expect(screen.getByText("Delivery policy")).toBeInTheDocument();
    expect(screen.getByText(/\/tmp\/policy.md/)).toBeInTheDocument();
    expect(screen.getByText("by meaning")).toBeInTheDocument();
  });

  it("says a passage was found on its words alone", async () => {
    render(<DocumentSearch client={client([passage({ semantic: 0 })])} />);

    fireEvent.change(screen.getByLabelText("Ask these documents something"), {
      target: { value: "delivery" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Search" }));

    await waitFor(() => expect(screen.getByText("by words only")).toBeInTheDocument());
  });

  it("ranks the passages and keeps a long one folded until it is asked for", async () => {
    const long = "A long passage. ".repeat(80);
    render(
      <DocumentSearch
        client={client([passage({ content: long }), passage({ document_id: "d2" })])}
      />,
    );

    fireEvent.change(screen.getByLabelText("Ask these documents something"), {
      target: { value: "delivery" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Search" }));

    await waitFor(() => expect(screen.getByLabelText("Rank 1")).toBeInTheDocument());
    expect(screen.getByLabelText("Rank 2")).toBeInTheDocument();

    // The position is the point: a passage that came back fourth is one a
    // budget of three would have cut.
    const folded = screen.getByText(long.trim());
    expect(folded.className).toContain("clamped");
    fireEvent.click(screen.getAllByRole("button", { name: "Show the whole passage" })[0]);
    expect(screen.getByText(long.trim()).className).not.toContain("clamped");
  });

  it("says plainly when an employee would have been given nothing", async () => {
    render(<DocumentSearch client={client([])} />);

    fireEvent.change(screen.getByLabelText("Ask these documents something"), {
      target: { value: "the capital of France" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Search" }));

    await waitFor(() =>
      expect(screen.getByText(/would be given no passage at all/)).toBeInTheDocument(),
    );
  });
});
