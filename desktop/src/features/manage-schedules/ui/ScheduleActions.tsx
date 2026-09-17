import { useState } from "react";

import type { Schedule } from "../../../entities/schedule";

/** Run now, open its thread, pause or resume, and delete in two clicks. */
export function ScheduleActions({
  schedule,
  onRunNow,
  onEdit,
  onOpen,
  onOpenTrace,
  onToggle,
  onDelete,
}: {
  schedule: Schedule;
  onRunNow: () => Promise<void>;
  onEdit?: () => void;
  onOpen?: () => void;
  onOpenTrace?: () => void;
  onToggle: () => Promise<void>;
  onDelete: () => Promise<void>;
}) {
  const [busy, setBusy] = useState(false);
  const [confirming, setConfirming] = useState(false);
  const label = schedule.name || schedule.request;

  const act = async (action: () => Promise<void>) => {
    setBusy(true);
    try {
      await action();
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="actions">
      <button
        type="button"
        disabled={busy}
        aria-label={`Run ${label} now`}
        onClick={() => void act(onRunNow)}
      >
        Run now
      </button>
      {onEdit && (
        <button type="button" aria-label={`Edit ${label}`} onClick={onEdit}>
          Edit
        </button>
      )}
      {onOpen && schedule.conversation_id && (
        <button type="button" aria-label={`Open results of ${label}`} onClick={onOpen}>
          Open results
        </button>
      )}
      {onOpenTrace && (
        <button type="button" aria-label={`Open last trace of ${label}`} onClick={onOpenTrace}>
          Open trace
        </button>
      )}
      <button
        type="button"
        disabled={busy}
        aria-label={`${schedule.enabled ? "Pause" : "Resume"} ${label}`}
        onClick={() => void act(onToggle)}
      >
        {schedule.enabled ? "Pause" : "Resume"}
      </button>
      {confirming ? (
        <>
          <button type="button" onClick={() => setConfirming(false)}>
            Keep
          </button>
          <button
            type="button"
            className="danger"
            disabled={busy}
            onClick={() => void act(onDelete).finally(() => setConfirming(false))}
          >
            Delete schedule
          </button>
        </>
      ) : (
        <button
          type="button"
          className="danger"
          aria-label={`Delete ${label}`}
          onClick={() => setConfirming(true)}
        >
          Delete
        </button>
      )}
    </div>
  );
}
