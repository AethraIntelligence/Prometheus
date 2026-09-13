import type { Document } from "../../../entities/document";
import type { RuntimeClient } from "../../../shared/api";

/**
 * Adding a document, re-indexing one, removing one.
 *
 * A document is added *by path*. The platform is local-first: the file is
 * already on this machine, and uploading it through the window would make the
 * interface a second copy of something the person already has - the same
 * reasoning the interface boundary applies to an attachment.
 */

export async function addDocument(
  client: RuntimeClient,
  path: string,
  title = "",
): Promise<Document> {
  return client.post<Document>("/api/documents", { path, title });
}

export async function replaceDocumentFile(
  client: RuntimeClient,
  id: string,
  path: string,
): Promise<Document> {
  return client.post<Document>(`/api/documents/${id}/file`, { path });
}

export async function reindexDocument(
  client: RuntimeClient,
  id: string,
): Promise<Document> {
  return client.post<Document>(`/api/documents/${id}/reindex`);
}

export async function removeDocument(client: RuntimeClient, id: string): Promise<boolean> {
  const body = await client.del<{ removed: boolean }>(`/api/documents/${id}`);
  return body.removed;
}
