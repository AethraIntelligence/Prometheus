/**
 * The documents this workspace has, and the three things a person does to them.
 *
 * Re-read after every operation, including a failed one: adding a document that
 * could not be embedded still leaves a record - text stored, meaning missing -
 * and that is exactly the state somebody needs to see rather than a rollback.
 */

import { useCallback, useEffect, useState } from "react";

import { documentApi, type Document } from "../../../entities/document";
import {
  addDocument,
  reindexDocument,
  removeDocument,
  replaceDocumentFile,
} from "../../../features/manage-documents";
import { report, type RuntimeClient } from "../../../shared/api";
import { describe } from "../../../shared/lib";

export interface DocumentsState {
  ready: boolean;
  available: boolean;
  problem: string;
  documents: Document[];
  add: (path: string) => Promise<void>;
  /** Several files at once, as the system dialog hands them over. */
  addAll: (paths: string[]) => Promise<void>;
  replace: (id: string, path: string) => Promise<void>;
  /** Show a failure that happened before the runtime was asked - a dialog that would not open. */
  fail: (error: unknown) => void;
  reindex: (id: string) => Promise<void>;
  remove: (id: string) => Promise<void>;
  reload: () => Promise<void>;
}

export function useDocuments(client: RuntimeClient): DocumentsState {
  const [ready, setReady] = useState(false);
  const [available, setAvailable] = useState(true);
  const [problem, setProblem] = useState("");
  const [documents, setDocuments] = useState<Document[]>([]);

  const reload = useCallback(async () => {
    try {
      const body = await documentApi.all(client);
      setAvailable(body.available ?? false);
      setDocuments(body.documents ?? []);
      setProblem("");
    } catch (error) {
      const said = describe(error);
      setProblem(said);
      report(said);
    } finally {
      setReady(true);
    }
  }, [client]);

  useEffect(() => {
    void reload();
  }, [reload]);

  const perform = useCallback(
    async (action: () => Promise<unknown>) => {
      try {
        await action();
        setProblem("");
      } catch (error) {
        const said = describe(error);
        setProblem(said);
        report(said);
      }
      await reload();
    },
    [reload],
  );

  return {
    ready,
    available,
    problem,
    documents,
    add: (path) => perform(() => addDocument(client, path)),
    addAll: (paths) =>
      perform(async () => {
        // One at a time: each is a model call per passage on this machine, and
        // the first refusal is reported rather than buried under the rest.
        for (const path of paths) await addDocument(client, path);
      }),
    replace: (id, path) => perform(() => replaceDocumentFile(client, id, path)),
    fail: (error) => {
      const said = describe(error);
      setProblem(said);
      report(said);
    },
    reindex: (id) => perform(() => reindexDocument(client, id)),
    remove: (id) => perform(() => removeDocument(client, id)),
    reload,
  };
}
