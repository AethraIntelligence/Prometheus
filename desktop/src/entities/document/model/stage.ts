/**
 * What a stage is called on the screen, and what it says underneath.
 *
 * Words only. Which stages exist, how far along one is and whether it ended are
 * the core's answers (`domain/knowledge/models.py`); this turns them into
 * English and derives nothing - a stage the window has never heard of is shown
 * by its own name rather than hidden, because a silent row is worse than an
 * unfamiliar word.
 */

import type { Indexing } from "./types";

export function stageLabel(indexing: Indexing): string {
  switch (indexing.stage) {
    case "READING":
      return "Reading the file";
    case "CHUNKING":
      return "Cutting it into passages";
    case "EMBEDDING":
      return "Indexing by meaning";
    case "DONE":
      return "Done";
    case "FAILED":
      return "Failed";
    default:
      return indexing.stage;
  }
}

export function describeStage(indexing: Indexing): string {
  if (indexing.stage === "FAILED") return indexing.error || "Failed";
  if (indexing.stage === "EMBEDDING" && indexing.total > 0) {
    return `${stageLabel(indexing)} - passage ${Math.min(indexing.done + 1, indexing.total)} of ${indexing.total}`;
  }
  return stageLabel(indexing);
}
