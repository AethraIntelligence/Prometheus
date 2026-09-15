/**
 * What a file holds, read once for as long as it is on screen.
 *
 * A PDF or an image is handed over as an object URL, and the URL is revoked
 * when the file is closed or another is opened - a panel that previews a dozen
 * files in a session would otherwise hold a dozen copies of them.
 */

import { useEffect, useState } from "react";

import { conversationApi, kindOf, type Artifact } from "../../../entities/conversation";
import type { RuntimeClient } from "../../../shared/api";
import { describe } from "../../../shared/lib";

export interface FileContent {
  loading: boolean;
  problem: string;
  /** For a file shown as itself: a PDF, an image. */
  url: string;
  /** For a file shown as text. */
  text: string;
}

const EMPTY: FileContent = { loading: true, problem: "", url: "", text: "" };

export function useFileContent(
  client: RuntimeClient,
  objectiveId: string,
  artifact: Artifact,
): FileContent {
  const [content, setContent] = useState<FileContent>(EMPTY);

  useEffect(() => {
    let cancelled = false;
    let url = "";
    setContent(EMPTY);
    const kind = kindOf(artifact);
    void (async () => {
      try {
        const blob = await conversationApi.file(client, objectiveId, artifact.path);
        if (cancelled) return;
        if (kind === "pdf" || kind === "image") {
          // The runtime's content type is kept: WebKit decides whether to draw
          // a PDF by the blob's type, not by what the file is called.
          url = URL.createObjectURL(
            blob.type ? blob : new Blob([blob], { type: artifact.media_type }),
          );
          setContent({ loading: false, problem: "", url, text: "" });
        } else if (kind === "other") {
          setContent({ loading: false, problem: "", url: "", text: "" });
        } else {
          setContent({ loading: false, problem: "", url: "", text: await blob.text() });
        }
      } catch (error) {
        if (!cancelled) setContent({ loading: false, problem: describe(error), url: "", text: "" });
      }
    })();
    return () => {
      cancelled = true;
      if (url) URL.revokeObjectURL(url);
    };
  }, [client, objectiveId, artifact]);

  return content;
}
