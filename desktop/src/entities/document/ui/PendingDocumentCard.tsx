import { IndexingLine } from "./DocumentCard";
import type { Indexing } from "../model/types";

/**
 * A file the runtime is reading, before there is a document to show.
 *
 * It exists because adding one is minutes on a local machine and the record is
 * written after the text comes out: without this, choosing five files left the
 * list exactly as it was until the first one finished, which reads as nothing
 * having happened.
 */
export function PendingDocumentCard({ indexing }: { indexing: Indexing }) {
  const name = indexing.title || indexing.key.split("/").pop() || indexing.key;
  return (
    <article className="document pending">
      <header>
        <strong>{name}</strong>
        <span className={indexing.stage === "FAILED" ? "badge danger" : "badge quiet"}>
          {indexing.stage === "FAILED" ? "failed" : "working"}
        </span>
      </header>
      <IndexingLine indexing={indexing} />
    </article>
  );
}
