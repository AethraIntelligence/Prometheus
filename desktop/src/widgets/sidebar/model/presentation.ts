/**
 * How a thread is drawn in the list. Words and grouping, and nothing about the
 * work: the status is the runtime's and this only picks the phrase and the dot
 * for it, the way `statusLine` picks a sentence.
 */

import type { Conversation, ObjectiveStatus } from "../../../entities/conversation";

const MARK: Record<ObjectiveStatus, { tone: string; label: string }> = {
  RECEIVED: { tone: "run", label: "Working" },
  PLANNING: { tone: "run", label: "Working" },
  RUNNING: { tone: "run", label: "Working" },
  PAUSED: { tone: "wait", label: "Paused" },
  DONE: { tone: "", label: "Done" },
  FAILED: { tone: "fail", label: "Failed" },
  ESCALATED: { tone: "wait", label: "Fell short" },
  CANCELLED: { tone: "", label: "Stopped" },
};

export function markFor(status: ObjectiveStatus | null | undefined): { tone: string; label: string } {
  return (status && MARK[status]) || { tone: "", label: "Nothing asked yet" };
}

export interface ThreadGroup {
  label: string;
  threads: Conversation[];
}

const DAY = 24 * 60 * 60 * 1000;

/** Today, yesterday, this week, and the rest - by the day the thread was last touched. */
export function groupByDay(threads: Conversation[], now: Date = new Date()): ThreadGroup[] {
  const midnight = new Date(now.getFullYear(), now.getMonth(), now.getDate()).getTime();
  const order = ["Today", "Yesterday", "Previous 7 days", "Earlier"];
  const groups = new Map<string, Conversation[]>();
  for (const thread of threads) {
    const at = Date.parse(thread.updated_at);
    const label = Number.isNaN(at)
      ? "Earlier"
      : at >= midnight
        ? "Today"
        : at >= midnight - DAY
          ? "Yesterday"
          : at >= midnight - 7 * DAY
            ? "Previous 7 days"
            : "Earlier";
    groups.set(label, [...(groups.get(label) ?? []), thread]);
  }
  return order
    .filter((label) => groups.has(label))
    .map((label) => ({ label, threads: groups.get(label)! }));
}
