import { useState, type FormEvent } from "react";

import type { Passage } from "../../../entities/document";
import { report, type RuntimeClient } from "../../../shared/api";
import { describe } from "../../../shared/lib";
import { searchDocuments } from "../api/search";

/**
 * What an employee would be given for this question.
 *
 * Deliberately not a document browser. It asks the runtime's own retrieval and
 * shows what came back, in the order it came back, with the citation each
 * passage carries - so "the answer was wrong" can be told apart from "the
 * answer was not in there", which is the one thing a person cannot work out
 * from the outside.
 *
 * The result is a ranked list, and it is drawn as one: the position is the
 * point, because a passage that came back fourth is a passage a budget of
 * three would have cut. Each row says how it was found, because a machine with
 * no embedding model retrieves on words alone and answers slightly worse -
 * which belongs on the screen rather than in an inference from worse answers.
 *
 * A passage is a page of text, so it is clamped and opened on request. The
 * first version showed every one in full, and two results filled the window
 * with a wall nobody reads - which hides the ranking, the thing this exists to
 * show.
 */
export function DocumentSearch({ client }: { client: RuntimeClient }) {
  const [question, setQuestion] = useState("");
  const [asked, setAsked] = useState("");
  const [passages, setPassages] = useState<Passage[] | null>(null);
  const [busy, setBusy] = useState(false);
  const [problem, setProblem] = useState("");

  const submit = async (event: FormEvent) => {
    event.preventDefault();
    const text = question.trim();
    if (!text) return;
    setBusy(true);
    try {
      setPassages(await searchDocuments(client, text));
      setAsked(text);
      setProblem("");
    } catch (error) {
      const said = describe(error);
      setProblem(said);
      report(said);
      setPassages(null);
    } finally {
      setBusy(false);
    }
  };

  return (
    <section className="panel">
      <div className="panel-head">
        <h2>What an employee would find</h2>
        {passages !== null && !problem && (
          <span className="note">
            {passages.length} passage(s) for “{asked}”
          </span>
        )}
      </div>

      <form className="doc-search" onSubmit={submit}>
        <input
          value={question}
          placeholder="Ask these documents something"
          aria-label="Ask these documents something"
          onChange={(event) => setQuestion(event.target.value)}
        />
        <button type="submit" disabled={busy || !question.trim()}>
          {busy ? "Looking…" : "Search"}
        </button>
      </form>

      {problem && (
        <p className="problem" role="alert">
          {problem}
        </p>
      )}

      {/* Nothing found is an answer, and the one worth saying plainly: it means
          an employee asked this would have been handed nothing. */}
      {passages !== null && passages.length === 0 && !problem && (
        <div className="card">
          <p className="card-empty">
            Nothing here answers “{asked}”. An employee asked this would be
            given no passage at all.
          </p>
        </div>
      )}

      {passages !== null && passages.length > 0 && (
        <div className="card">
          {passages.map((passage, index) => (
            <PassageRow
              key={`${passage.document_id}-${index}`}
              passage={passage}
              rank={index + 1}
            />
          ))}
        </div>
      )}
    </section>
  );
}

function PassageRow({ passage, rank }: { passage: Passage; rank: number }) {
  const [open, setOpen] = useState(false);
  const byMeaning = passage.semantic > 0;

  return (
    <article className="passage">
      <header>
        <span className="passage-rank" aria-label={`Rank ${rank}`}>
          {rank}
        </span>
        <strong>{passage.title}</strong>
        <span className={byMeaning ? "badge" : "badge quiet"}>
          {byMeaning ? "by meaning" : "by words only"}
        </span>
      </header>

      <p className={open ? "quotation" : "quotation clamped"}>{passage.content}</p>
      <button type="button" className="linkish" onClick={() => setOpen(!open)}>
        {open ? "Show less" : "Show the whole passage"}
      </button>

      <footer className="passage-facts">
        <span className="passage-source" title={passage.source}>
          {passage.source || "no source recorded"}
        </span>
        <span className="passage-scores">
          <b>{passage.score.toFixed(2)}</b> · words {passage.lexical.toFixed(2)} ·
          meaning {passage.semantic.toFixed(2)}
        </span>
      </footer>
    </article>
  );
}
