import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import type { Message } from "../../../entities/conversation";
import { RuntimeClient, RuntimeProvider } from "../../../shared/api";
import { ConversationView } from "./ConversationView";
import { greetingFor } from "./Greeting";

const message: Message = {
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
};

describe("ConversationView", () => {
  it("greets when nothing has been asked yet", () => {
    render(<ConversationView messages={[]} activity={[]} busy={false} />);

    expect(
      screen.getByText("What would you like me to do?"),
    ).toBeInTheDocument();
  });

  it("greets by the hour", () => {
    expect(greetingFor(9)).toBe("Good morning.");
    expect(greetingFor(15)).toBe("Good afternoon.");
    expect(greetingFor(21)).toBe("Good evening.");
  });

  it("offers the recollection only where there was one", () => {
    const answered: Message = {
      ...message,
      status: "DONE",
      thinking: false,
      answer: "Sorted.",
      answered: true,
    };

    const none = render(
      <ConversationView
        messages={[answered]}
        activity={[]}
        busy={false}
        explainMemory
      />,
    );
    expect(
      none.queryByRole("button", { name: "Memory used" }),
    ).not.toBeInTheDocument();
    none.unmount();

    render(
      <RuntimeProvider
        client={
          new RuntimeClient(
            "http://127.0.0.1:9999",
            (async () => new Response("{}")) as never,
          )
        }
      >
        <ConversationView
          messages={[{ ...answered, memory_used: true }]}
          activity={[]}
          busy={false}
          explainMemory
        />
      </RuntimeProvider>,
    );
    expect(
      screen.getByRole("button", { name: "Memory used" }),
    ).toBeInTheDocument();
  });

  it("has a trail only while something is running, folded until asked for", () => {
    const events = [
      {
        task_id: "t1",
        objective_id: "o1",
        kind: "TOOL_CALL" as const,
        message: "files.write",
        step: 1,
        payload: {},
        at: null,
      },
    ];

    const idle = render(
      <ConversationView messages={[message]} activity={events} busy={false} />,
    );
    expect(idle.queryByText("files.write")).not.toBeInTheDocument();
    idle.unmount();

    render(<ConversationView messages={[message]} activity={events} busy />);
    expect(screen.queryByText("files.write")).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: /Working on it/ }));
    expect(screen.getByText("files.write")).toBeInTheDocument();
  });
});
