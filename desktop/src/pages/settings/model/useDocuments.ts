/**
 * The documents this workspace has, and the three things a person does to them.
 *
 * Re-read after every operation, including a failed one: adding a document that
 * could not be embedded still leaves a record - text stored, meaning missing -
 * and that is exactly the state somebody needs to see rather than a rollback.
 *
 * And re-read *while* one runs. Indexing happens inside the request that asked
 * for it, which on a local embedding model is minutes; the request cannot
 * report on itself, so a second one asks where it has got to. The poll exists
 * only while there is something to watch - `POLL_MS` apart, stopping the moment
 * the runtime says nothing is in flight.
 */

//: Close enough that a bar moves, far enough apart that a machine embedding
//: passages is not also answering a request every animation frame.
const POLL_MS = 900;

import { useCallback, useEffect, useRef, useState } from "react";

import { documentApi, type Document, type Indexing } from "../../../entities/document";
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
  /** What is being read or embedded right now, as the runtime last said. */
  indexing: Indexing[];
  /** Whether this window is itself waiting on an operation it started. */
  busy: boolean;
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
  const [indexing, setIndexing] = useState<Indexing[]>([]);
  const [busy, setBusy] = useState(false);

  const reload = useCallback(async () => {
    try {
      const body = await documentApi.all(client);
      setAvailable(body.available ?? false);
      setDocuments(body.documents ?? []);
      setIndexing(body.indexing ?? []);
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

  // Watching, not waiting: the poll follows what the runtime says is in flight,
  // so a document another window (or the CLI) added shows its progress here
  // too - and it stops on its own when the last one ends rather than on a
  // count this window keeps of its own requests.
  const watching = busy || indexing.some((one) => !one.finished);
  const pending = useRef(false);
  useEffect(() => {
    if (!watching) return;
    let live = true;
    const timer = window.setInterval(() => {
      // One in the air at a time. A slow answer must not queue requests behind
      // it on the process that is doing the indexing.
      if (pending.current || !live) return;
      pending.current = true;
      void reload().finally(() => {
        pending.current = false;
      });
    }, POLL_MS);
    return () => {
      live = false;
      window.clearInterval(timer);
    };
  }, [watching, reload]);

  const perform = useCallback(
    async (action: () => Promise<unknown>) => {
      setBusy(true);
      try {
        await action();
        setProblem("");
      } catch (error) {
        const said = describe(error);
        setProblem(said);
        report(said);
      } finally {
        setBusy(false);
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
    indexing,
    busy,
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
