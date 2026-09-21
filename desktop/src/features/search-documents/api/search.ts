import type { Passage } from "../../../entities/document";
import type { RuntimeClient } from "../../../shared/api";

/**
 * The same retrieval a run gets, asked directly.
 *
 * It is the difference between "the answer was wrong" and "the answer was not
 * in there" - the one question a person cannot settle from the outside, because
 * what an employee was handed is not in the answer it wrote.
 */
export async function searchDocuments(
  client: RuntimeClient,
  question: string,
  limit = 5,
): Promise<Passage[]> {
  const body = await client.get<{ passages: Passage[] }>(
    `/api/documents/search?q=${encodeURIComponent(question)}&limit=${limit}`,
  );
  return body.passages ?? [];
}
