import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { statusLine } from "../model/status";
import type { Message } from "../model/types";
import { MessageTurn } from "./MessageTurn";

const message = (extra: Partial<Message> = {}): Message => ({
  id: "m1",
  text: "Sort these files",
  status: "RUNNING",
  thinking: true,
  answer: "",
  missing: [],
  answered: false,
  cost_usd: 0,
  created_at: "2026-09-08T09:00:00+00:00",
  finished_at: null,
  ...extra,
});

describe("MessageTurn", () => {
  it("shows what was asked, and that work is still happening", () => {
    render(<MessageTurn message={message()} />);

    expect(screen.getByText("Sort these files")).toBeInTheDocument();
    expect(screen.getByText("Working on it…")).toBeInTheDocument();
  });

  it("keeps the steps of a running turn folded until they are asked for", () => {
    render(<MessageTurn message={message()} work={<p>Planned — read the folder</p>} />);

    expect(screen.queryByText("Planned — read the folder")).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: /Working on it/ }));
    expect(screen.getByText("Planned — read the folder")).toBeInTheDocument();
  });

  it("shows the answer once there is one", () => {
    render(
      <MessageTurn
        message={message({ answered: true, status: "DONE", answer: "Sorted into four folders." })}
      />,
    );

    expect(screen.getByText("Sorted into four folders.")).toBeInTheDocument();
    expect(screen.queryByText("Working on it…")).not.toBeInTheDocument();
  });

  it("names what was missing when the work fell short", () => {
    render(
      <MessageTurn
        message={message({
          answered: true,
          status: "ESCALATED",
          answer: "I could not finish this.",
          missing: ["No access to the sales folder"],
        })}
      />,
    );

    expect(screen.getByText("No access to the sales folder")).toBeInTheDocument();
  });

  it("describes the stage the run is actually in", () => {
    expect(statusLine(message({ status: "PLANNING" }))).toBe("Working out what this takes…");
    expect(statusLine(message({ answered: true }))).toBe("");
  });
});
