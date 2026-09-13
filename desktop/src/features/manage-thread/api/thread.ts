import type { Conversation } from "../../../entities/conversation";
import type { RuntimeClient } from "../../../shared/api";

/**
 * Naming a thread and taking it out of the list.
 *
 * Removing a thread is not removing the work. What was asked, what ran and
 * what it did stays in the record; the runtime stops anything still running in
 * the thread first, and says so by answering - this layer assumes neither.
 */

export function renameThread(
  client: RuntimeClient,
  conversationId: string,
  title: string,
): Promise<Conversation> {
  return client.patch<Conversation>(`/api/conversations/${conversationId}`, {
    title,
  });
}

export async function deleteThread(
  client: RuntimeClient,
  conversationId: string,
): Promise<boolean> {
  const body = await client.del<{ deleted: boolean }>(`/api/conversations/${conversationId}`);
  return body.deleted;
}
