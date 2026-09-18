/**
 * What a schedule settled into, asked about where the schedule is read.
 *
 * The platform has always noticed when it kept planning the same thing; what
 * was missing was a place to say so that means anything to the person. It used
 * to be a "Process" list in the new-schedule form - a choice made before there
 * was any evidence, by somebody with no way to judge it. It is now a question
 * about this schedule, asked after its own runs have answered it, in the words
 * of that schedule.
 *
 * Neither the word "workflow" nor a version number appears here. What is being
 * offered is that the work stops being re-planned from scratch every morning;
 * which declaration that becomes is the core's business.
 *
 * Keeping it is reversible, and the line says so: the old route stays on disk,
 * so unpinning is a way back rather than a loss.
 */

import { useEffect, useState } from "react";

import type { Schedule } from "../../../entities/schedule";
import { report, useRuntime } from "../../../shared/api";
import { describe } from "../../../shared/lib";
import { declineOrder, dropOrder, keepOrder, offeredOrders, type SettledOrder } from "../api/order";

export function ScheduleOrder({
  schedule,
  onChanged,
}: {
  schedule: Schedule;
  /** The schedule as the runtime returned it after keeping or dropping an order. */
  onChanged?: (schedule: Schedule) => void;
}) {
  const client = useRuntime();
  const [offered, setOffered] = useState<SettledOrder[]>([]);
  const [busy, setBusy] = useState(false);
  const [problem, setProblem] = useState("");
  const pinned = Boolean(schedule.workflow_name);

  useEffect(() => {
    let current = true;
    // Only for a schedule that has run: a pattern is read off successful runs,
    // so asking before there are any is a request that can only answer "none".
    if (pinned || schedule.runs === 0) {
      setOffered([]);
      return;
    }
    void offeredOrders(client, schedule.id)
      .then((body) => current && setOffered(body.suggestions ?? []))
      .catch(() => current && setOffered([]));
    return () => {
      current = false;
    };
  }, [client, schedule.id, schedule.runs, pinned]);

  const act = async (what: () => Promise<unknown>) => {
    setBusy(true);
    setProblem("");
    try {
      return await what();
    } catch (error) {
      const said = describe(error);
      setProblem(said);
      void report(said);
      return null;
    } finally {
      setBusy(false);
    }
  };

  if (pinned) {
    return (
      <p className="note schedule-order">
        Runs in the order it settled into, rather than being worked out again each time.
        <button
          type="button"
          className="mini bordered"
          disabled={busy}
          onClick={async () => {
            const changed = await act(() => dropOrder(client, schedule.id));
            if (changed) onChanged?.(changed as Schedule);
          }}
        >
          Work it out each time
        </button>
        {problem && <span className="problem-inline">{problem}</span>}
      </p>
    );
  }

  const [first] = offered;
  if (!first) return null;

  return (
    <div className="schedule-order offered" role="status">
      <p>
        This ran the same way {first.occurrences} times. Keep that order, so the result stops
        changing between runs?
      </p>
      <div className="actions">
        <button
          type="button"
          className="primary"
          disabled={busy}
          onClick={async () => {
            const changed = await act(() => keepOrder(client, schedule.id, first.id));
            if (changed) onChanged?.(changed as Schedule);
          }}
        >
          Keep it
        </button>
        <button
          type="button"
          disabled={busy}
          onClick={async () => {
            await act(() => declineOrder(client, first.id));
            setOffered((known) => known.filter((item) => item.id !== first.id));
          }}
        >
          Not now
        </button>
      </div>
      <small>You can go back to working it out each time whenever you like.</small>
      {problem && <p className="problem">{problem}</p>}
    </div>
  );
}
