import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import type { Artifact } from "../../../entities/conversation";
import { FileActions } from "./FileActions";

const artifact = (extra: Partial<Artifact> = {}): Artifact => ({
  path: "ai_news.md",
  name: "ai_news.md",
  location: "/Users/someone/Documents/Prometheus/news/ai_news.md",
  media_type: "text/markdown",
  size: 3072,
  exists: true,
  ...extra,
});

describe("FileActions", () => {
  it("offers only what this file and this surface can do", async () => {
    // In a browser there is no shell, so nothing can reach a path: what is
    // left is the text and the path themselves.
    render(<FileActions artifact={artifact()} text="# News" />);

    await userEvent.click(screen.getByRole("button", { name: /Actions for/ }));

    expect(screen.queryByRole("menuitem", { name: "Open" })).not.toBeInTheDocument();
    expect(screen.getByRole("menuitem", { name: "Copy contents" })).toBeInTheDocument();
    expect(screen.getByRole("menuitem", { name: "Copy path" })).toBeInTheDocument();
  });

  it("does not offer the contents of a file it has no text for", async () => {
    render(<FileActions artifact={artifact({ media_type: "application/pdf", name: "q3.pdf" })} />);

    await userEvent.click(screen.getByRole("button", { name: /Actions for/ }));

    expect(screen.queryByRole("menuitem", { name: "Copy contents" })).not.toBeInTheDocument();
    expect(screen.getByRole("menuitem", { name: "Copy path" })).toBeInTheDocument();
  });

  it("says what reached the clipboard, because a clipboard says nothing", async () => {
    const writeText = vi.fn(async () => undefined);
    Object.assign(navigator, { clipboard: { writeText } });
    render(<FileActions artifact={artifact()} text="# News" />);

    await userEvent.click(screen.getByRole("button", { name: /Actions for/ }));
    await userEvent.click(screen.getByRole("menuitem", { name: "Copy contents" }));

    expect(writeText).toHaveBeenCalledWith("# News");
    expect(await screen.findByText("Contents copied")).toBeInTheDocument();
  });
});
