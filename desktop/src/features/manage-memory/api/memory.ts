import type { MemoryItem } from "../../../entities/memory";
import type { RuntimeClient } from "../../../shared/api";

/**
 * A note of the person's own, and forgetting one line.
 *
 * Whether a line may be forgotten is the core's question, answered against
 * what the listing shows; this sends the id and reports what came back.
 */

export async function rememberNote(
  client: RuntimeClient,
  content: string,
  aboutThePerson: boolean,
): Promise<MemoryItem> {
  return client.post<MemoryItem>("/api/memory", {
    content,
    about_the_person: aboutThePerson,
  });
}

export async function forgetMemory(client: RuntimeClient, id: string): Promise<boolean> {
  const body = await client.del<{ forgotten: boolean }>(`/api/memory/${id}`);
  return body.forgotten;
}

/** Replace what a line says. The core keeps the old wording as replaced. */
export async function correctMemory(
  client: RuntimeClient,
  id: string,
  content: string,
): Promise<MemoryItem> {
  return client.put<MemoryItem>(`/api/memory/${id}`, { content });
}

/** Keep a line for a number of days, or - with null - until something replaces it. */
export async function keepMemory(
  client: RuntimeClient,
  id: string,
  days: number | null,
): Promise<MemoryItem> {
  return client.put<MemoryItem>(`/api/memory/${id}/retention`, { days });
}
