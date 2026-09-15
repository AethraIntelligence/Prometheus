/**
 * A file the work produced, shown as the file rather than as its name in a sentence.
 *
 * What kind of file it is decides the label and nothing else - the runtime
 * said what the file is (`media_type`); this only says it the way a person
 * reads it.
 */

import { BookIcon } from "../../../shared/ui";
import type { Artifact } from "../model/types";

export type FileKind = "pdf" | "image" | "markdown" | "text" | "other";

export function kindOf(artifact: Pick<Artifact, "media_type" | "name">): FileKind {
  const type = artifact.media_type;
  if (type === "application/pdf") return "pdf";
  if (type.startsWith("image/")) return "image";
  if (type === "text/markdown") return "markdown";
  if (type.startsWith("text/") || type === "application/json") return "text";
  return "other";
}

const LABEL: Record<FileKind, string> = {
  pdf: "PDF",
  image: "Image",
  markdown: "Markdown",
  text: "Text",
  other: "File",
};

function extension(name: string): string {
  const dot = name.lastIndexOf(".");
  return dot > 0
    ? name
        .slice(dot + 1)
        .toUpperCase()
        .slice(0, 4)
    : "";
}

function sizeOf(bytes: number | null): string {
  if (bytes === null) return "";
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${Math.round(bytes / 1024)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

interface Props {
  artifact: Artifact;
  onOpen?: (artifact: Artifact) => void;
}

export function FileCard({ artifact, onOpen }: Props) {
  const kind = kindOf(artifact);
  const detail = artifact.exists
    ? [LABEL[kind], sizeOf(artifact.size)].filter(Boolean).join(" · ")
    : "No longer where it was written";
  return (
    <button
      type="button"
      className={`filecard ${kind}${artifact.exists ? "" : " gone"}`}
      disabled={!artifact.exists}
      title={artifact.path}
      onClick={() => onOpen?.(artifact)}
    >
      <span className="filecard-ico" aria-hidden="true">
        {extension(artifact.name) || <BookIcon />}
      </span>
      <span className="filecard-meta">
        <b>{artifact.name}</b>
        <span>{detail}</span>
      </span>
    </button>
  );
}
