/**
 * What this workspace remembers, searched, added to and forgotten.
 *
 * Search is the core's `recall` with words in it, asked a moment after the
 * person stops typing rather than filtered here: ranking what memory holds is
 * the domain's decision, and a second ranking in the window would disagree
 * with the one every run reads.
 *
 * Re-read after every change, including a refused one, so the listing is what
 * the store holds rather than what the window expected it to hold.
 */

import { useCallback, useEffect, useState } from "react";

import { memoryApi, type MemoryItem, type MemoryTrace } from "../../../entities/memory";
import {
  correctMemory,
  forgetMemory,
  keepMemory,
  rememberNote,
} from "../../../features/manage-memory";
import { report, type RuntimeClient } from "../../../shared/api";
import { describe } from "../../../shared/lib";

/** Long enough not to ask per keystroke, short enough to feel like typing into a filter. */
const SEARCH_DELAY_MS = 250;

export interface MemoryState {
  ready: boolean;
  available: boolean;
  canForget: boolean;
  problem: string;
  items: MemoryItem[];
  search: string;
  setSearch: (words: string) => void;
  /** Whether lines replaced by a correction are listed too. */
  history: boolean;
  setHistory: (shown: boolean) => void;
  add: (content: string, aboutThePerson: boolean) => Promise<void>;
  correct: (id: string, content: string) => Promise<void>;
  keep: (id: string, days: number | null) => Promise<void>;
  forget: (id: string) => Promise<void>;
  trace: (id: string) => Promise<MemoryTrace | null>;
  reload: () => Promise<void>;
}

export function useMemory(client: RuntimeClient): MemoryState {
  const [ready, setReady] = useState(false);
  const [available, setAvailable] = useState(true);
  const [canForget, setCanForget] = useState(false);
  const [problem, setProblem] = useState("");
  const [items, setItems] = useState<MemoryItem[]>([]);
  const [search, setSearch] = useState("");
  const [asked, setAsked] = useState("");
  const [history, setHistory] = useState(false);

  useEffect(() => {
    const timer = setTimeout(() => setAsked(search), SEARCH_DELAY_MS);
    return () => clearTimeout(timer);
  }, [search]);

  const reload = useCallback(async () => {
    try {
      const body = await memoryApi.all(client, asked, history);
      setAvailable(body.available ?? true);
      setCanForget(body.can_forget ?? false);
      setItems(body.items ?? []);
      setProblem("");
    } catch (error) {
      const said = describe(error);
      setProblem(said);
      report(said);
    } finally {
      setReady(true);
    }
  }, [client, asked, history]);

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
    canForget,
    problem,
    items,
    search,
    setSearch,
    history,
    setHistory,
    add: (content, aboutThePerson) =>
      perform(() => rememberNote(client, content, aboutThePerson)),
    correct: (id, content) => perform(() => correctMemory(client, id, content)),
    keep: (id, days) => perform(() => keepMemory(client, id, days)),
    forget: (id) => perform(() => forgetMemory(client, id)),
    trace: async (id) => {
      try {
        return await memoryApi.trace(client, id);
      } catch (error) {
        const said = describe(error);
        setProblem(said);
        report(said);
        return null;
      }
    },
    reload,
  };
}
