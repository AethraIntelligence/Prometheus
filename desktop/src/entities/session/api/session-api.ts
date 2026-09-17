import type { RuntimeClient } from "../../../shared/api";
import type { SessionBrief } from "../model/types";

export const sessionApi = {
  async brief(client: RuntimeClient, conversationId: string): Promise<SessionBrief> {
    return client.get<SessionBrief>(`/api/conversations/${conversationId}/session`);
  },

  /** A person saying an open question is settled. */
  async resolve(
    client: RuntimeClient,
    conversationId: string,
    question: string,
  ): Promise<SessionBrief> {
    return client.post<SessionBrief>(`/api/conversations/${conversationId}/session/resolved`, {
      question,
    });
  },
};
