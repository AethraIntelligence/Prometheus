import { SOURCE, type RuntimeClient } from "../../../shared/api";
import {
  NO_DIRECTIONS,
  type Conversation,
  type ConversationList,
  type Directions,
  type Message,
  type Thread,
} from "../model/types";

/** Opening a thread, reading it, naming it, removing it, and saying one thing in it. */
export const conversationApi = {
  open(client: RuntimeClient, title = ""): Promise<Conversation> {
    return client.post<Conversation>("/api/conversations", { title });
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
    });
  },
};
