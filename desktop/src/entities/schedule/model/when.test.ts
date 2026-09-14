import { describe, expect, it } from "vitest";

import type { Schedule } from "./types";
import { describeLastRun, describeWhen } from "./when";

function schedule(overrides: Partial<Schedule>): Schedule {
  return {
    id: "s1",
    name: "",
    request: "Check",
    enabled: true,
    every_seconds: null,
    daily_at_utc: null,
    on_event: "",
    next_due_at: null,
    last_run_at: null,
    last_status: null,
    runs: 0,
    conversation_id: null,
    model: "",
    approvals: "ASK",
    created_at: "2026-09-14T09:00:00+00:00",
    ...overrides,
  };
}

describe("describeWhen", () => {
  it("says an interval in the largest whole unit", () => {
    expect(describeWhen(schedule({ every_seconds: 60 }))).toBe("Every minute");
    expect(describeWhen(schedule({ every_seconds: 90 * 60 }))).toBe("Every 90 minutes");
    expect(describeWhen(schedule({ every_seconds: 7200 }))).toBe("Every 2 hours");
    expect(describeWhen(schedule({ every_seconds: 86400 }))).toBe("Every day");
  });

  it("names the event a schedule waits for", () => {
    expect(describeWhen(schedule({ on_event: "objective.finished" }))).toBe(
      "When objective.finished happens",
    );
  });

  it("puts a time of day on the local clock", () => {
    const said = describeWhen(schedule({ daily_at_utc: "06:30" }));
    expect(said.startsWith("Every day at ")).toBe(true);
  });
});

describe("describeLastRun", () => {
  it("says when nothing has run, and what the last run came to", () => {
    expect(describeLastRun(schedule({}))).toBe("Not run yet");
    expect(
      describeLastRun(schedule({ last_run_at: "2026-09-14T06:30:00+00:00", last_status: "FAILED" })),
    ).toMatch(/^Last run failed /);
  });
});
