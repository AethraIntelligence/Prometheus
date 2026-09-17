/**
 * One screen: a conversation with Prometheus, and what it is doing about it.
 *
 * Not a dashboard. It shows the thing the person came to do - say what they
 * need - and shows the work only while there is work. The column is narrow and
 * the composer floats over its foot, so the request is always the nearest thing
 * to the person's hands.
 *
 * Until something has been asked there is no foot to float over: an empty
 * thread puts the greeting and the field in the middle of the window, which is
 * where the eye already is and where the one action on the screen belongs. The
 * composer docks the moment there is a conversation to keep above it.
 *
 * Called `chat` since Phase 15, when the word "workspace" stopped meaning three
 * things at once. It is the conversation; a workspace is the context the
 * conversation happens in, named in the header and chosen under the field.
 */

import { useEffect, useRef, useState } from "react";

import { ApprovalCard } from "../../../entities/approval";
import type { Artifact } from "../../../entities/conversation";
import { DirectionChips } from "../../../features/choose-directions";
import { ComposerMenu, FolderTray } from "../../../features/choose-folder";
import { ApprovalDecision } from "../../../features/decide-approval";
import { RequestComposer } from "../../../features/send-request";
import { StopButton } from "../../../features/stop-run";
import { useRuntime } from "../../../shared/api";
import { PageHead } from "../../../shared/ui";
import { ConversationView } from "../../../widgets/conversation";
import { FilePreview } from "../../../widgets/file-preview";
import { WorkforcePanel } from "../../../widgets/workforce";
import { WorkspaceBar, useWorkspaces } from "../../../widgets/workspace-bar";
import { useChat } from "../model/useChat";

interface Props {
  conversationId?: string | null;
  onOpened?: (conversationId: string) => void;
  onChanged?: () => void;
  /** Changes when the open thread was changed from the list. */
  refresh?: number;
  onSwitched?: () => void;
  railOpen?: boolean;
  onOpenRail?: () => void;
}

export function ChatPage({
  conversationId = null,
  onOpened,
  onChanged,
  refresh,
  onSwitched,
  railOpen = true,
  onOpenRail,
}: Props = {}) {
  const client = useRuntime();
  const {
    ready,
    problem,
    thread,
    messages,
    activity,
    trails,
    approvals,
    employees,
    directions,
    setDirections,
    models,
    busy,
    send,
    chooseFolder,
    stop,
    decide,
  } = useChat(client, conversationId, { onOpened, onChanged, refresh });
  const { active } = useWorkspaces(client);

  // Which file is open beside the conversation. It belongs to a turn in this
  // thread, so opening another thread closes it.
  const [preview, setPreview] = useState<{ objectiveId: string; artifact: Artifact } | null>(null);
  useEffect(() => setPreview(null), [conversationId]);

  // Keep the newest thing in view. The stream is the page's own scroll, so a
  // turn arriving below the fold would otherwise arrive unseen.
  const stream = useRef<HTMLDivElement>(null);
  const lastActivity = activity.length;
  useEffect(() => {
    const element = stream.current;
    if (element) element.scrollTop = element.scrollHeight;
  }, [messages.length, lastActivity, approvals.length]);

  const blank = messages.length === 0;

  // Built once and placed twice. The two layouts differ in where these sit, not
  // in what they are - a second copy would be the empty window quietly drifting
  // away from the one people spend their day in.
  const conversation = (
    <>
      <ConversationView
        messages={messages}
        activity={activity}
        trails={trails}
        busy={busy}
        // The workforce goes under the composer when the composer is in the
        // middle of the screen, so the greeting and the field stay together.
        empty={blank ? null : <WorkforcePanel employees={employees} />}
        onOpenFile={(message, artifact) => setPreview({ objectiveId: message.id, artifact })}
      />
      {approvals.map((approval) => (
        <ApprovalCard
          key={approval.id}
          approval={approval}
          actions={<ApprovalDecision approvalId={approval.id} onDecide={decide} />}
        />
      ))}
      {problem && (
        <p className="problem" role="alert">
          {problem}
        </p>
      )}
      {!ready && !problem && (
        <p className="working waking">
          <span className="spin" aria-hidden="true" />
          Starting Prometheus…
        </p>
      )}
    </>
  );

  // One choice, shown in two places: the + menu and the tray under the field.
  const folderChoice = {
    folder: thread?.folder || directions.folder || "",
    fileRoot: active?.file_root,
    saved: active?.folders ?? [],
    onChoose: chooseFolder,
    disabled: !ready,
  };

  const composer = (
    <>
      <RequestComposer
        onSend={send}
        disabled={!ready}
        extras={
          <>
            <ComposerMenu {...folderChoice} />
            {!thread && <WorkspaceBar onSwitched={onSwitched} />}
            <DirectionChips
              directions={directions}
              models={models}
              onChange={setDirections}
              disabled={!!conversationId && !thread}
            />
          </>
        }
        stop={busy ? <StopButton onStop={stop} /> : undefined}
      />
      <FolderTray {...folderChoice} />
      <p className="hint">Irreversible actions wait for you. Everything runs on this machine.</p>
    </>
  );

  return (
    <div className={preview ? "split previewing" : "split"}>
      <main className="main">
        <PageHead
          title={thread?.title || "New task"}
          chip={active?.name}
          railOpen={railOpen}
          onOpenRail={onOpenRail}
        />

        {blank ? (
          <div className="stream blank">
            <div className="col">
              {conversation}
              {composer}
              <WorkforcePanel employees={employees} />
            </div>
          </div>
        ) : (
          <>
            <div className="stream" ref={stream}>
              <div className="col">{conversation}</div>
            </div>
            <div className="dock">
              <div className="col">{composer}</div>
            </div>
          </>
        )}
      </main>
      {preview && (
        <FilePreview
          key={`${preview.objectiveId}:${preview.artifact.path}`}
          objectiveId={preview.objectiveId}
          artifact={preview.artifact}
          onClose={() => setPreview(null)}
        />
      )}
    </div>
  );
}
