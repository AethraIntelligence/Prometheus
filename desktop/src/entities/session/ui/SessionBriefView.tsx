import type { SessionBrief } from "../model/types";

interface Props {
  brief: SessionBrief;
  /** Marking an open question settled. Without it the questions are only listed. */
  onResolve?: (question: string) => void;
}

/** What a long thread has established, section by section, as the core sent it. */
export function SessionBriefView({ brief, onResolve }: Props) {
  return (
    <div className="session-brief">
      {brief.goal && (
        <section aria-label="Goal">
          <h3>Goal</h3>
          <p>{brief.goal}</p>
        </section>
      )}
      <section aria-label="Decisions">
        <h3>Decisions</h3>
        {brief.decisions.length === 0 ? (
          <p className="card-empty">No decisions recorded yet.</p>
        ) : (
          <ul>
            {brief.decisions.map((note) => (
              <li key={note.text}>{note.text}</li>
            ))}
          </ul>
        )}
      </section>
      <section aria-label="Open questions">
        <h3>Open questions</h3>
        {brief.open_questions.length === 0 ? (
          <p className="card-empty">Nothing is waiting on an answer.</p>
        ) : (
          <ul>
            {brief.open_questions.map((note) => (
              <li key={note.text}>
                <span>{note.text}</span>
                {onResolve && (
                  <button type="button" className="linkish" onClick={() => onResolve(note.text)}>
                    Settled
                  </button>
                )}
              </li>
            ))}
          </ul>
        )}
      </section>
      {brief.artifacts.length > 0 && (
        <section aria-label="Files">
          <h3>Files</h3>
          <ul className="mono">
            {brief.artifacts.map((item) => (
              <li key={item.path}>{item.path}</li>
            ))}
          </ul>
        </section>
      )}
      {brief.stages.length > 0 && (
        <section aria-label="Earlier stages">
          <h3>Earlier stages</h3>
          <ol>
            {brief.stages.map((stage) => (
              <li key={stage.index}>
                <p>{stage.summary}</p>
                <p className="note">
                  {stage.objective_ids.length} request(s) · {stage.started_at.slice(0, 10)}
                  {stage.summarised ? "" : " · short summary, no model was available"}
                </p>
              </li>
            ))}
          </ol>
        </section>
      )}
      <p className="note">
        {brief.total_turns} request(s) in this thread · {brief.compacted_turns} compacted ·{" "}
        {brief.recent_turns} shown to Prometheus word for word
      </p>
    </div>
  );
}
