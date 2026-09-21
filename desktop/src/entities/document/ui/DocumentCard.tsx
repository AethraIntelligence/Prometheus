import type { ReactNode } from "react";

import { moment } from "../../../shared/lib";
import { describeStage } from "../model/stage";
import type { Document, Indexing } from "../model/types";

/**
 * One document: whether it can be found by meaning or only by its words, when
 * it arrived, and - while something is being done to it - how far that has got.
 *
 * The progress line replaces the passage count rather than sitting beside it.
 * A count that belongs to the previous version of a file, shown next to a bar
 * saying the new one is half read, is two answers to one question.
 *
 * Both dates are shown only where they differ. "Added today, updated today" is
 * a row that spends a line saying nothing.
 */
export function DocumentCard({
  document,
  indexing,
  actions,
}: {
  document: Document;
  indexing?: Indexing;
  actions?: ReactNode;
}) {
  const working = indexing !== undefined && !indexing.finished;
  const changed = document.updated_at.slice(0, 16) !== document.created_at.slice(0, 16);

  return (
    <article className="document">
      <header>
        <strong>{document.title}</strong>
        {working ? (
          <span className="badge quiet">working</span>
        ) : (
          <span className={document.status === "INDEXED" ? "badge" : "badge quiet"}>
            {document.status.toLowerCase()}
          </span>
        )}
        {actions}
      </header>
      {working ? (
        <IndexingLine indexing={indexing} />
      ) : (
        <p className="note">
          {document.chunks} passage(s)
          {document.status === "EXTRACTED" && " - found by words, not yet by meaning"}
        </p>
      )}
      <p className="note">
        Added {moment(document.created_at)}
        {changed && ` · updated ${moment(document.updated_at)}`}
        {document.source && ` · ${document.source}`}
      </p>
      {(document.error || (indexing?.stage === "FAILED" && indexing.error)) && (
        <p className="problem" role="alert">
          {document.error || indexing?.error}
        </p>
      )}
    </article>
  );
}

/**
 * A bar and a sentence saying what the bar is waiting for.
 *
 * Where there is no fraction the bar still moves, because "working" and "stuck"
 * have to look different and reading a file reports no percentage anybody could
 * believe. Where there is one, it is the core's (`fraction`), never arithmetic
 * repeated here.
 */
export function IndexingLine({ indexing }: { indexing: Indexing }) {
  const known = indexing.fraction > 0;
  return (
    <div className="indexing">
      <div
        className={known ? "indexing-bar" : "indexing-bar unknown"}
        role="progressbar"
        aria-label={`Indexing ${indexing.title}`}
        aria-valuemin={0}
        aria-valuemax={known ? 100 : undefined}
        aria-valuenow={known ? Math.round(indexing.fraction * 100) : undefined}
      >
        <span style={known ? { width: `${Math.round(indexing.fraction * 100)}%` } : undefined} />
      </div>
      <p className="note">{describeStage(indexing)}</p>
    </div>
  );
}
