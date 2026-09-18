import { useState, type FormEvent } from "react";

import type { ModelEntry } from "../../../entities/provider";
import { localClock, type Schedule } from "../../../entities/schedule";
import { Hint } from "../../../shared/ui";
import type { NewSchedule } from "../api/schedules";

type When = "every" | "daily" | "event";
type Unit = "minutes" | "hours" | "days";
type Approvals = "ASK" | "AUTO" | "DENY";

/** What each choice means for a run nobody may be watching. */
const APPROVAL_CHOICES: { value: Approvals; label: string; hint: string }[] = [
  {
    value: "ASK",
    label: "Ask me",
    hint: "The question waits in the window; if nobody answers in time, the action is refused.",
  },
  {
    value: "AUTO",
    label: "Go ahead without asking",
    hint: "Actions that would ask you are done. Policies that forbid an action still forbid it.",
  },
  {
    value: "DENY",
    label: "Never",
    hint: "Anything that needs approval is refused, and the run goes on without it.",
  },
];

const UNIT_MINUTES: Record<Unit, number> = { minutes: 1, hours: 60, days: 60 * 24 };

interface Initial {
  when: When;
  count: string;
  unit: Unit;
  time: string;
  event: string;
}

/** What the form shows for a schedule that exists: its own timing, on this clock. */
function initialFrom(schedule?: Schedule): Initial {
  const fresh: Initial = {
    when: "daily",
    count: "1",
    unit: "hours",
    time: "09:00",
    event: "objective.finished",
  };
  if (!schedule) return fresh;
  if (schedule.every_seconds) {
    const minutes = Math.round(schedule.every_seconds / 60);
    const unit: Unit =
      minutes % UNIT_MINUTES.days === 0 ? "days" : minutes % UNIT_MINUTES.hours === 0 ? "hours" : "minutes";
    return { ...fresh, when: "every", unit, count: String(minutes / UNIT_MINUTES[unit]) };
  }
  if (schedule.daily_at && schedule.timezone) {
    return { ...fresh, when: "daily", time: schedule.daily_at };
  }
  if (schedule.daily_at_utc) {
    return { ...fresh, when: "daily", time: localClock(schedule.daily_at_utc) };
  }
  return { ...fresh, when: "event", event: schedule.on_event };
}

/**
 * A request, when to ask it again, and which model to ask.
 *
 * Four fields and no fifth. There used to be a "Process" list above the
 * request, offering the declarations this machine holds - a choice nobody
 * setting work on a clock is equipped to make, sitting above the one field
 * they came to fill. A schedule now reaches a fixed order the other way: it
 * runs its request the long way, and once the platform has watched the same
 * order settle out of its own runs it offers to keep it, on the schedule's own
 * card, in the words of that schedule. A pinned order is carried through here
 * untouched, because editing the timing of a schedule is not a decision about
 * how it runs.
 *
 * Four rows, in the order the questions are actually asked: what to call this,
 * when it goes, what it runs with, and what it asks for. Each row is the full
 * width of a dialog that is wide rather than tall, so the timing block lays its
 * three choices out on one line each and the request has room to be read.
 *
 * It used to be one narrow column with a paragraph under the model list and
 * another under the approvals list, which made the field somebody opened the
 * dialog to fill the last thing below two pages of prose. Those paragraphs are
 * worth keeping and not worth reading twice, so they moved into the mark beside
 * each field.
 *
 * Three ways of saying when, and exactly one is sent - the core refuses a
 * schedule that could mean two things. A time of day is typed on the person's
 * clock and goes with that clock's offset, because "every morning at nine" was
 * said about the wall, not about UTC.
 *
 * The same form edits a schedule that exists: it opens on that schedule's own
 * words, timing and model, and sends the whole of it back.
 */
export function NewScheduleForm({
  onCreate,
  initialRequest = "",
  initialName = "",
  schedule,
  models = [],
  disabled,
}: {
  onCreate: (schedule: NewSchedule) => Promise<void>;
  initialRequest?: string;
  initialName?: string;
  /** A schedule to edit. Absent: a new one. */
  schedule?: Schedule;
  /** Models a run may prefer, as the catalog lists them. */
  models?: ModelEntry[];
  disabled?: boolean;
}) {
  const start = initialFrom(schedule);
  const [request, setRequest] = useState(schedule?.request ?? initialRequest);
  const [name, setName] = useState(schedule?.name ?? initialName);
  const [when, setWhen] = useState<When>(start.when);
  const [count, setCount] = useState(start.count);
  const [unit, setUnit] = useState<Unit>(start.unit);
  const [time, setTime] = useState(start.time);
  const [event, setEvent] = useState(start.event);
  const [model, setModel] = useState(schedule?.model ?? "");
  const [approvals, setApprovals] = useState<Approvals>(schedule?.approvals ?? "ASK");
  const [busy, setBusy] = useState(false);
  // A model the catalog no longer has is still shown, so saving does not
  // silently change it; the core accepts it or says why not.
  const listed = models.some((entry) => entry.name === model);

  const amount = Number(count);
  const ready =
    request.trim() !== "" &&
    (when !== "every" || (Number.isInteger(amount) && amount > 0)) &&
    (when !== "daily" || /^\d{2}:\d{2}$/.test(time)) &&
    (when !== "event" || event.trim() !== "");

  const submit = async (submitted: FormEvent) => {
    submitted.preventDefault();
    if (!ready || busy) return;
    const chosen: NewSchedule = {
      request: request.trim(),
      name: name.trim(),
      model,
      approvals,
    };
    // An order this schedule already settled on is carried, not re-chosen: the
    // whole schedule is sent back on an edit, and leaving these out would
    // quietly unpin it.
    if (schedule?.workflow_name) {
      chosen.workflow_name = schedule.workflow_name;
      chosen.workflow_version = schedule.workflow_version ?? undefined;
      chosen.workflow_inputs = schedule.workflow_inputs;
    }
    if (when === "every") chosen.every_minutes = amount * UNIT_MINUTES[unit];
    if (when === "daily") {
      chosen.daily_at = time;
      // getTimezoneOffset is minutes *behind* UTC; the core wants ahead. Subtracting
      // from zero rather than negating, because negating UTC's 0 gives -0.
      chosen.utc_offset_minutes = 0 - new Date().getTimezoneOffset();
      chosen.timezone = Intl.DateTimeFormat().resolvedOptions().timeZone;
    }
    if (when === "event") chosen.on_event = event.trim();
    setBusy(true);
    try {
      await onCreate(chosen);
    } finally {
      setBusy(false);
    }
  };

  return (
    <form
      className="add-integration new-schedule"
      onSubmit={submit}
      aria-label={schedule ? "Edit schedule" : "New schedule"}
    >
      <label>
        <span className="field-name">
          Name <span className="hint">optional</span>
        </span>
        <input
          value={name}
          placeholder="Morning digest"
          onChange={(changed) => setName(changed.target.value)}
          disabled={disabled || busy}
        />
      </label>

      <fieldset className="when">
        <legend>When</legend>
        <label className="checkbox">
          <input
            type="radio"
            name="when"
            checked={when === "daily"}
            onChange={() => setWhen("daily")}
            disabled={disabled || busy}
          />
          Every day at
          <input
            type="time"
            aria-label="Time of day"
            value={time}
            onChange={(changed) => {
              setTime(changed.target.value);
              setWhen("daily");
            }}
            disabled={disabled || busy}
          />
        </label>
        <label className="checkbox">
          <input
            type="radio"
            name="when"
            checked={when === "every"}
            onChange={() => setWhen("every")}
            disabled={disabled || busy}
          />
          Every
          <input
            type="number"
            min={1}
            step={1}
            aria-label="How many"
            value={count}
            onChange={(changed) => {
              setCount(changed.target.value);
              setWhen("every");
            }}
            disabled={disabled || busy}
          />
          <select
            aria-label="Unit"
            value={unit}
            onChange={(changed) => {
              setUnit(changed.target.value as Unit);
              setWhen("every");
            }}
            disabled={disabled || busy}
          >
            <option value="minutes">minutes</option>
            <option value="hours">hours</option>
            <option value="days">days</option>
          </select>
        </label>
        <label className="checkbox">
          <input
            type="radio"
            name="when"
            checked={when === "event"}
            onChange={() => setWhen("event")}
            disabled={disabled || busy}
          />
          When this happens
          <input
            aria-label="Event"
            value={event}
            onChange={(changed) => {
              setEvent(changed.target.value);
              setWhen("event");
            }}
            disabled={disabled || busy}
          />
        </label>
        {when === "event" && (
          <p className="note">
            objective.finished is recorded each time a scheduled run ends, so one schedule can
            follow another.
          </p>
        )}
      </fieldset>

      <div className="field-pair">
        <label>
          <span className="field-name">
            Model
            <Hint label="What choosing a model changes">
              A free or busy model can be overloaded when the run starts, and the run then
              fails. For work that matters, choose a paid or local model here. A model that
              cannot do part of the work - seeing a screen, say - is passed over for that part.
            </Hint>
          </span>
          <select
            aria-label="Model"
            value={model}
            onChange={(changed) => setModel(changed.target.value)}
            disabled={disabled || busy}
          >
            <option value="">Automatic - each kind of work goes where it is routed</option>
            {model && !listed && <option value={model}>{model} (not in the catalog)</option>}
            {models.map((entry) => (
              <option key={entry.name} value={entry.name}>
                {entry.name} - {entry.model}
              </option>
            ))}
          </select>
        </label>

        <label>
          <span className="field-name">
            Approvals
            <Hint label="What each approval setting does">
              {APPROVAL_CHOICES.map((choice) => (
                <span key={choice.value}>
                  <b>{choice.label}.</b> {choice.hint}
                </span>
              ))}
              Runs usually happen with nobody at the keyboard - use Run now to see what it asks
              for first.
            </Hint>
          </span>
          <select
            aria-label="Approvals"
            value={approvals}
            onChange={(changed) => setApprovals(changed.target.value as Approvals)}
            disabled={disabled || busy}
          >
            {APPROVAL_CHOICES.map((choice) => (
              <option key={choice.value} value={choice.value}>
                {choice.label}
              </option>
            ))}
          </select>
        </label>
      </div>

      <label>
        What to ask
        <textarea
          value={request}
          rows={3}
          placeholder="Summarise what changed in my notes folder yesterday"
          onChange={(changed) => setRequest(changed.target.value)}
          disabled={disabled || busy}
        />
      </label>

      <button type="submit" disabled={disabled || busy || !ready}>
        {busy ? "Saving…" : schedule ? "Save changes" : "Schedule"}
      </button>
    </form>
  );
}
