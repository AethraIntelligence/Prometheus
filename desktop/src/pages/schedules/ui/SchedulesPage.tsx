/**
 * Scheduled work: what Prometheus is asked to do without being asked again.
 *
 * A page rather than a setting, because it is read like the list of threads -
 * what is coming, what happened last time - and a page beside New task because
 * that is the other half of the same question: now, or regularly.
 *
 * Every schedule made here has a thread, and its runs are written into it, so
 * "what did this morning's run say" is one click to a conversation the person
 * already knows how to read. The page never shows a next run the runtime will
 * not keep: with the scheduler off it says so first, and offers the switch.
 *
 * It opens on what the person set up, and on nothing else. A catalog of
 * declared processes used to sit above the list, with two shipped examples in
 * it, so the first thing on a page called Scheduled was something nobody here
 * had scheduled. Whether a schedule runs in a settled order is now a line on
 * that schedule's own card, offered once its own runs have settled; the
 * declarations themselves are read in Settings, where the rest of what this
 * machine holds is read.
 */

import { useEffect, useState } from "react";

import { conversationApi } from "../../../entities/conversation";
import { ScheduleCard, type Schedule } from "../../../entities/schedule";
import { NewScheduleForm, ScheduleActions } from "../../../features/manage-schedules";
import { ScheduleOrder } from "../../../features/settle-schedule-order";
import { RestartButton } from "../../../features/restart-runtime";
import { useRuntime } from "../../../shared/api";
import { ClockIcon, Modal, PageHead, PlusIcon } from "../../../shared/ui";
import { useSchedules } from "../model/useSchedules";

interface Draft {
  request: string;
  name: string;
  conversationId?: string;
}

interface Props {
  railOpen?: boolean;
  onOpenRail?: () => void;
  onOpenThread?: (conversationId: string) => void;
  /** A thread to repeat: the dialog opens with its first request filled in. */
  repeat?: string | null;
  onRepeatTaken?: () => void;
  onOpenSettings?: () => void;
  onOpenTrace?: (objectiveId: string) => void;
}

export function SchedulesPage({
  railOpen = true,
  onOpenRail,
  onOpenThread,
  repeat = null,
  onRepeatTaken,
  onOpenSettings,
  onOpenTrace,
}: Props = {}) {
  const client = useRuntime();
  const page = useSchedules(client);
  const [draft, setDraft] = useState<Draft | null>(null);
  const [editing, setEditing] = useState<Schedule | null>(null);

  useEffect(() => {
    if (!repeat) return;
    let current = true;
    void conversationApi
      .thread(client, repeat)
      .then((thread) => {
        if (!current) return;
        setDraft({
          request: thread.messages[0]?.text ?? "",
          name: thread.title,
          conversationId: thread.id,
        });
      })
      .catch(() => current && setDraft({ request: "", name: "" }))
      .finally(() => onRepeatTaken?.());
    return () => {
      current = false;
    };
  }, [client, repeat, onRepeatTaken]);

  const off = page.ready && page.available && !page.running;

  return (
    <main className="main">
      <PageHead title="Scheduled" railOpen={railOpen} onOpenRail={onOpenRail}>
        <button
          type="button"
          className="addbtn"
          onClick={() => setDraft({ request: "", name: "" })}
          disabled={!page.available}
        >
          <PlusIcon />
          New schedule
        </button>
      </PageHead>
      <div className="stream">
        <section className="col settings schedules" aria-label="Scheduled">
          <p className="lede">
            Requests Prometheus makes on its own - every morning, every few hours, or after
            something happens. Each one keeps its results in its own thread.
          </p>

          {off && (
            <div className="restart-note scheduler-off" role="status">
              {page.switch?.on ? (
                <>
                  <p>
                    Scheduled work is turned on and starts the next time Prometheus starts.
                    Restart it to begin.
                  </p>
                  <RestartButton label="Restart now" primary />
                </>
              ) : page.switch?.lockedBy ? (
                <p>
                  Scheduled work is off, set by <code>{page.switch.lockedBy}</code> in the
                  environment. Nothing below runs until that changes.
                </p>
              ) : (
                <>
                  <p>
                    Scheduled work is off, so nothing below runs on its own yet. Run now still
                    works.
                  </p>
                  <div className="actions">
                    <button type="button" className="primary" onClick={() => void page.turnOn()}>
                      Turn on
                    </button>
                    {onOpenSettings && (
                      <button type="button" onClick={onOpenSettings}>
                        Open settings
                      </button>
                    )}
                  </div>
                </>
              )}
            </div>
          )}

          {page.problem && (
            <p className="problem" role="alert">
              {page.problem}
            </p>
          )}
          {!page.available && page.ready && (
            <p className="note">This runtime was started without schedules.</p>
          )}

          {page.available && (
            <div className="card">
              {page.ready && page.schedules.length === 0 ? (
                <div className="card-empty schedules-empty">
                  <ClockIcon />
                  <p>Nothing is scheduled yet.</p>
                  <button
                    type="button"
                    className="addbtn"
                    onClick={() => setDraft({ request: "", name: "" })}
                  >
                    <PlusIcon />
                    New schedule
                  </button>
                </div>
              ) : (
                page.schedules.map((schedule) => (
                  <ScheduleCard
                    key={schedule.id}
                    schedule={schedule}
                    running={page.running}
                    order={<ScheduleOrder schedule={schedule} onChanged={() => void page.refresh()} />}
                    actions={
                      <ScheduleActions
                        schedule={schedule}
                        onRunNow={async () => {
                          const thread = await page.runNow(schedule);
                          if (thread) onOpenThread?.(thread);
                        }}
                        onEdit={() => setEditing(schedule)}
                        onOpen={
                          onOpenThread && schedule.conversation_id
                            ? () => onOpenThread(schedule.conversation_id!)
                            : undefined
                        }
                        onOpenTrace={
                          onOpenTrace && schedule.recent_runs?.[0]?.objective_id
                            ? () => onOpenTrace(schedule.recent_runs![0].objective_id)
                            : undefined
                        }
                        onToggle={() => page.toggle(schedule)}
                        onDelete={() => page.remove(schedule)}
                      />
                    }
                  />
                ))
              )}
            </div>
          )}

          {draft && (
            <Modal
              title={draft.conversationId ? "Repeat on a schedule" : "New schedule"}
              note="Asked in your words each time, and answered in its own thread."
              wide
              onClose={() => setDraft(null)}
            >
              <NewScheduleForm
                models={page.models}
                initialRequest={draft.request}
                initialName={draft.name}
                onCreate={async (schedule) => {
                  const made = await page.create({
                    ...schedule,
                    conversation_id: draft.conversationId,
                  });
                  if (made) setDraft(null);
                }}
              />
            </Modal>
          )}
          {editing && (
            <Modal
              title="Edit schedule"
              note="Its thread and what earlier runs did stay as they are."
              wide
              onClose={() => setEditing(null)}
            >
              <NewScheduleForm
                key={editing.id}
                schedule={editing}
                models={page.models}
                onCreate={async (changed) => {
                  if (await page.update(editing.id, changed)) setEditing(null);
                }}
              />
            </Modal>
          )}
        </section>
      </div>

    </main>
  );
}
