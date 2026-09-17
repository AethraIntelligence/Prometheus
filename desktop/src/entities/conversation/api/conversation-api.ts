import { SOURCE, type RuntimeClient } from "../../../shared/api";
import {
  NO_DIRECTIONS,
  type Conversation,
  type ConversationKind,
  type ConversationList,
  type Directions,
  type Message,
  type Thread,
} from "../model/types";

/** Opening a thread, reading it, naming it, removing it, and saying one thing in it. */
export const conversationApi = {
  open(
    client: RuntimeClient,
    title = "",
    kind: ConversationKind = "TASK",
  ): Promise<Conversation> {
    return client.post<Conversation>("/api/conversations", { title, kind });
  },

  /** The threads of this workspace, most recent first. */
  list(client: RuntimeClient): Promise<ConversationList> {
    return client.get<ConversationList>("/api/conversations");
  },

  thread(client: RuntimeClient, conversationId: string): Promise<Thread> {
    return client.get<Thread>(`/api/conversations/${conversationId}`);
  },

  /** The contents of a file a turn produced. Only files the turn is recorded as writing are served. */
  file(client: RuntimeClient, objectiveId: string, path: string): Promise<Blob> {
    return client.blob(`/api/objectives/${objectiveId}/file?path=${encodeURIComponent(path)}`);
  },

  /** Say one thing. It becomes an objective; Prometheus decides what it takes. */
  send(
    client: RuntimeClient,
    conversationId: string,
    request: string,
    directions: Directions = NO_DIRECTIONS,
  ): Promise<Message> {
    return client.post<Message>(`/api/conversations/${conversationId}/messages`, {
      request,
      source: SOURCE,
      input_type: "text",
      approvals: directions.approvals,
      model: directions.model,
      folder: directions.folder ?? "",
    });
  },

  /** Point a thread at a folder for the requests that follow. Empty: its own again. */
  setFolder(client: RuntimeClient, conversationId: string, folder: string): Promise<Thread> {
    return client.put<Thread>(`/api/conversations/${conversationId}/folder`, { folder });
  },

  /** Keep the approval mode on the thread, including before another request is sent. */
  setApprovals(
    client: RuntimeClient,
    conversationId: string,
    approvals: Directions["approvals"],
  ): Promise<Thread> {
    return client.put<Thread>(`/api/conversations/${conversationId}/approvals`, { approvals });
  },

  /** Keep the preferred model on the thread, including before another request is sent. */
  setModel(client: RuntimeClient, conversationId: string, model: string): Promise<Thread> {
    return client.put<Thread>(`/api/conversations/${conversationId}/model`, { model });
  },
};
