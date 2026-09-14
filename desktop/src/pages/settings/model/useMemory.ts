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

import { memoryApi, type MemoryItem } from "../../../entities/memory";
import { forgetMemory, rememberNote } from "../../../features/manage-memory";
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
  add: (content: string, aboutThePerson: boolean) => Promise<void>;
  forget: (id: string) => Promise<void>;
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

  useEffect(() => {
    const timer = setTimeout(() => setAsked(search), SEARCH_DELAY_MS);
    return () => clearTimeout(timer);
  }, [search]);

  const reload = useCallback(async () => {
    try {
      const body = await memoryApi.all(client, asked);
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
  }, [client, asked]);

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
    add: (content, aboutThePerson) =>
      perform(() => rememberNote(client, content, aboutThePerson)),
    forget: (id) => perform(() => forgetMemory(client, id)),
    reload,
  };
}
