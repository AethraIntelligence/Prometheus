/**
 * The conversation, and what is happening because of it.
 *
 * A widget composes; it fetches nothing and decides nothing. The live trail
 * belongs to the turn that is running, folded behind its status line, because
 * an interface that keeps a log open is a log viewer, which is the thing the
 * local surface was built to stop anybody needing. A finished turn keeps the
 * trail this window saw behind its "Worked for" line.
 */

import type { ReactNode } from "react";

import { ActivityTrail, type ActivityEvent } from "../../../entities/activity";
import { MessageTurn, type Artifact, type Message } from "../../../entities/conversation";
import { MemoryUsedButton } from "../../../features/explain-memory";
import { Greeting } from "./Greeting";

interface Props {
  messages: Message[];
  /** What the running turn is doing now. */
  activity: ActivityEvent[];
  busy: boolean;
  /** What each finished turn was seen doing, by objective, where this window watched it. */
  trails?: Record<string, ActivityEvent[]>;
  /** Shown under the greeting while nothing has been asked. */
  empty?: ReactNode;
  /** A file a turn produced was chosen. */
  onOpenFile?: (message: Message, artifact: Artifact) => void;
  /** Offer "Memory used" under each answer. Off where no runtime is behind the view. */
  explainMemory?: boolean;
}

export function ConversationView({
  messages,
  activity,
  busy,
  trails = {},
  empty,
  onOpenFile,
  explainMemory = false,
}: Props) {
  if (messages.length === 0) {
    return (
      <>
        <Greeting />
        {empty}
      </>
    );
  }
  const running = messages.find((message) => !message.answered)?.id;
  return (
    <ol className="turns">
      {messages.map((message) => {
        const events = message.id === running ? (busy ? activity : []) : (trails[message.id] ?? []);
        return (
          <MessageTurn
            key={message.id}
            message={message}
            work={events.length > 0 ? <ActivityTrail events={events} /> : undefined}
            memory={
              explainMemory && message.answered ? (
                <MemoryUsedButton objectiveId={message.id} />
              ) : undefined
            }
            onOpenFile={onOpenFile}
          />
        );
      })}
    </ol>
  );
}
