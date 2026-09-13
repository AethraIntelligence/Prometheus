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

import { useEffect, useRef } from "react";

import { ApprovalCard } from "../../../entities/approval";
import { DirectionChips } from "../../../features/choose-directions";
import { ApprovalDecision } from "../../../features/decide-approval";
import { RequestComposer } from "../../../features/send-request";
import { StopButton } from "../../../features/stop-run";
import { useRuntime } from "../../../shared/api";
import { PageHead } from "../../../shared/ui";
import { ConversationView } from "../../../widgets/conversation";
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
    stop,
    decide,
  } = useChat(client, conversationId, { onOpened, onChanged, refresh });
  const { active } = useWorkspaces(client);

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

  const composer = (
    <>
      <RequestComposer
        onSend={send}
        disabled={!ready}
        extras={
          <>
            <WorkspaceBar onSwitched={onSwitched} />
            <DirectionChips directions={directions} models={models} onChange={setDirections} />
          </>
        }
        stop={busy ? <StopButton onStop={stop} /> : undefined}
      />
      <p className="hint">Irreversible actions wait for you. Everything runs on this machine.</p>
    </>
  );

  return (
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
  );
}
