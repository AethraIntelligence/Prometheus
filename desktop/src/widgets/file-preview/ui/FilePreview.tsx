/**
 * A file the work produced, open beside the conversation.
 *
 * Beside rather than over it: the answer that names the file is the reason to
 * look at the file, and a preview that covered it would make the person choose
 * between the two. The file is shown the way it would be read - a PDF as pages,
 * Markdown rendered, text as text - and anything else is one button away from
 * the application the system opens it with.
 */

import { kindOf, type Artifact } from "../../../entities/conversation";
import { canOpenFiles, openFile, useRuntime } from "../../../shared/api";
import { CloseIcon, Markdown } from "../../../shared/ui";
import { useFileContent } from "../model/useFileContent";

interface Props {
  objectiveId: string;
  artifact: Artifact;
  onClose: () => void;
}

export function FilePreview({ objectiveId, artifact, onClose }: Props) {
  const client = useRuntime();
  const content = useFileContent(client, objectiveId, artifact);
  const kind = kindOf(artifact);

  return (
    <aside className="preview" aria-label={`Preview: ${artifact.name}`}>
      <header className="preview-head">
        <span className="preview-title">
          <b>{artifact.name}</b>
          <span>{artifact.path}</span>
        </span>
        {canOpenFiles() && artifact.location && (
          <button
            type="button"
            className="btn btn-line"
            onClick={() => void openFile(artifact.location)}
          >
            Open
          </button>
        )}
        <button type="button" className="icobtn" aria-label="Close preview" onClick={onClose}>
          <CloseIcon />
        </button>
      </header>
      <div className={`preview-body ${kind}`}>
        {content.loading ? (
          <p className="working">
            <span className="spin" aria-hidden="true" />
            Opening…
          </p>
        ) : content.problem ? (
          <p className="problem" role="alert">
            {content.problem}
          </p>
        ) : kind === "pdf" ? (
          <iframe title={artifact.name} src={content.url} />
        ) : kind === "image" ? (
          <img src={content.url} alt={artifact.name} />
        ) : kind === "markdown" ? (
          <div className="preview-doc">
            <Markdown text={content.text} />
          </div>
        ) : kind === "text" ? (
          <pre className="preview-text">{content.text}</pre>
        ) : (
          <p className="preview-none">
            There is no preview for this kind of file
            {canOpenFiles() ? " - open it in its own application." : "."}
          </p>
        )}
      </div>
    </aside>
  );
}
