import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { Markdown, siteOf } from "./Markdown";

describe("Markdown", () => {
  it("renders what the answer wrote, not the punctuation it wrote it with", () => {
    const { container } = render(
      <Markdown
        text={"Here are two:\n\n1. **Gemini on iPhone**: launched.\n2. **AlphaQubit**: decoder."}
      />,
    );

    expect(container.querySelectorAll("ol > li")).toHaveLength(2);
    expect(screen.getByText("Gemini on iPhone").tagName).toBe("STRONG");
    expect(container.textContent).not.toContain("**");
  });

  it("draws a source by its name, and an address by its site", () => {
    render(
      <Markdown
        text={
          "One [Reuters](https://www.reuters.com/a). Two [https://blog.google/x/](https://blog.google/x/)."
        }
      />,
    );

    expect(screen.getByRole("link", { name: "Reuters" })).toHaveClass("source");
    expect(screen.getByRole("link", { name: "blog.google" })).toHaveAttribute(
      "href",
      "https://blog.google/x/",
    );
  });

  it("does not put a page's markup into the window", () => {
    const { container } = render(<Markdown text={'Quoted <img src="x" onerror="alert(1)">'} />);

    expect(container.querySelector("img")).toBeNull();
  });

  it("names a site the way a person does", () => {
    expect(siteOf("https://www.zeta-alpha.com/post/1")).toBe("zeta-alpha.com");
  });
});
