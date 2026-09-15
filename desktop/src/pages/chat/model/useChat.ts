/**
 * The page's state, and the only place that holds any.
 *
 * Thin on purpose: the thread on screen, what each run in it was seen doing,
 * the questions waiting on the person, and who is available. Every one of those
 * is *read* from the runtime rather than derived here - there is no local model
 * of a task's lifecycle, no optimistic status and no rule about when work is
 * finished. The runtime says; this remembers what it last said.
 *
 * Which thread is on screen is the frame's to say (`conversationId`), because
 * the sidebar lists them. A new task has none until its first request: a thread
 * is opened when there is something to put in it, not when a window starts -
 * the version that opened one per start filled the list with blank rows.
 */

import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import { watchObjective, type ActivityEvent } from "../../../entities/activity";
import { approvalApi, type Approval } from "../../../entities/approval";
import {
  NO_DIRECTIONS,
  conversationApi,
  type Directions,
  type Message,
  type Thread,
} from "../../../entities/conversation";
import { employeeApi, type Employee } from "../../../entities/employee";
import { providerApi, type ModelEntry } from "../../../entities/provider";
import { decideApproval } from "../../../features/decide-approval";
import { stopObjective } from "../../../features/stop-run";
import type { RuntimeClient } from "../../../shared/api";
import { report } from "../../../shared/api";
import { describe } from "../../../shared/lib";

/** How often the questions waiting on a person are re-read. */
const APPROVAL_POLL_MS = 3000;
/** How often a thread is re-read while something in it is still running. */
const THREAD_POLL_MS = 2000;

export interface ChatState {
  ready: boolean;
  problem: string;
  thread: Thread | null;
  messages: Message[];
  /** What the running turn is doing now. */
  activity: ActivityEvent[];
  /** What each turn was seen doing, by objective, for as long as this page is up. */
  trails: Record<string, ActivityEvent[]>;
  approvals: Approval[];
  employees: Employee[];
  /** What the next request will say about how to go about it. */
  directions: Directions;
  setDirections: (next: Directions) => void;
  /** The models a person may prefer: the catalog's, minus what cannot hold a conversation. */
  models: ModelEntry[];
  busy: boolean;
  send: (request: string) => Promise<void>;
  /** Point this thread - or the next new one - at a folder. Empty: a folder of its own. */
  chooseFolder: (folder: string) => Promise<void>;
  stop: () => Promise<void>;
  decide: (approvalId: string, approved: boolean) => Promise<void>;
}

export interface ChatHooks {
  /** A thread was opened for the first request of a new task. */
  onOpened?: (conversationId: string) => void;
  /** Something the sidebar lists changed: a request was made, or a run ended. */
  onChanged?: () => void;
  /** Changes when the thread on screen was changed from elsewhere - renamed in the list. */
  refresh?: number;
}

export function useChat(
  client: RuntimeClient,
  conversationId: string | null = null,
  hooks: ChatHooks = {},
): ChatState {
  const [ready, setReady] = useState(false);
  const [problem, setProblem] = useState("");
  const [thread, setThread] = useState<Thread | null>(null);
  const [trails, setTrails] = useState<Record<string, ActivityEvent[]>>({});
  const [approvals, setApprovals] = useState<Approval[]>([]);
  const [employees, setEmployees] = useState<Employee[]>([]);
  const [directions, setDirections] = useState<Directions>(NO_DIRECTIONS);
  const [models, setModels] = useState<ModelEntry[]>([]);
  const watching = useRef<string | null>(null);
  const unwatch = useRef<(() => void) | null>(null);
  // Which thread is on screen, readable from callbacks that outlive a render: a
  // re-read that arrives after the person opened another thread must not put
  // the old one back.
  const shown = useRef<string | null>(null);
  const told = useRef(hooks);
  told.current = hooks;

  const adopt = useCallback((next: Thread | null) => {
    shown.current = next?.id ?? null;
    setThread(next);
  }, []);

  const running = useMemo(
    () => thread?.messages.find((message) => !message.answered) ?? null,
    [thread],
  );
  const busy = running !== null;

  // Shown to the person, and told to the shell. A window is often started from
  // a terminal, and a failure only the window knows about is one nobody can
  // diagnose without taking a photograph of the screen.
  const fail = useCallback((error: unknown) => {
    const text = describe(error);
    setProblem(text);
    void report(text);
  }, []);

  const reread = useCallback(
    async (id: string) => {
      try {
        const read = await conversationApi.thread(client, id);
        if (shown.current === id) setThread(read);
      } catch (error) {
        fail(error);
      }
    },
    [client, fail],
  );

  // Ready once the engine answers. Taking a request before then would take one
  // the window cannot deliver.
  useEffect(() => {
    let cancelled = false;
    void (async () => {
      try {
        const workforce = await employeeApi.all(client);
        if (cancelled) return;
        setEmployees(workforce);
        setReady(true);
      } catch (error) {
        if (!cancelled) fail(error);
        return;
      }
      try {
        const catalog = await providerApi.all(client);
        if (cancelled) return;
        // An embedding model cannot answer anybody. Which entries can is the
        // catalog's own declaration, read rather than guessed at.
        setModels(
          (catalog.models ?? []).filter((entry) => entry.capabilities.includes("TEXT_REASONING")),
        );
      } catch {
        // Without a list the chip is not shown and the router chooses, which is
        // what every request did before there was a chip.
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [client, fail]);

  // A new task starts from the defaults; a thread keeps what was chosen in it
  // for as long as it is on screen.
  useEffect(() => {
    if (!conversationId) setDirections(NO_DIRECTIONS);
  }, [conversationId]);

  const refresh = hooks.refresh ?? 0;
  useEffect(() => {
    if (refresh && shown.current) void reread(shown.current);
  }, [refresh, reread]);

  // The thread the frame names. Nothing is loaded for one this page opened
  // itself - it is already on screen.
  useEffect(() => {
    if (conversationId === shown.current) return;
    setProblem("");
    if (!conversationId) {
      adopt(null);
      return;
    }
    let cancelled = false;
    void (async () => {
      try {
        const read = await conversationApi.thread(client, conversationId);
        if (cancelled) return;
        adopt(read);
        // Opened on what the thread is set to. Only when it is opened: a
        // re-read while it is on screen must not undo a chip somebody just
        // changed.
        setDirections(read.directions ?? NO_DIRECTIONS);
      } catch (error) {
        if (!cancelled) fail(error);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [client, conversationId, adopt, fail]);

  // Follow whatever is running. One subscription at a time: the trace shown is
  // the trace of the request the person is waiting on.
  useEffect(() => {
    const objectiveId = running?.id;
    const threadId = thread?.id;
    if (!objectiveId || !threadId || watching.current === objectiveId) return;
    unwatch.current?.();
    watching.current = objectiveId;
    unwatch.current = watchObjective(client, objectiveId, (event) => {
      setTrails((seen) => ({ ...seen, [objectiveId]: [...(seen[objectiveId] ?? []), event] }));
      // The answer is written to the store by the runtime, not carried in the
      // stream. The final event says when it is worth reading again.
      if (event.kind === "RESULT" && event.objective_id === objectiveId) {
        void reread(threadId);
        told.current.onChanged?.();
      }
    });
    return () => {
      unwatch.current?.();
      unwatch.current = null;
      watching.current = null;
    };
  }, [client, running?.id, thread?.id, reread]);

  // While something is running the thread is re-read as well as watched. A
  // stream is lossy by design - a dropped connection must not be the difference
  // between seeing the answer and not seeing it.
  useEffect(() => {
    const threadId = thread?.id;
    if (!threadId || !busy) return;
    const timer = setInterval(() => void reread(threadId), THREAD_POLL_MS);
    return () => clearInterval(timer);
  }, [thread?.id, busy, reread]);

  useEffect(() => {
    if (!ready) return;
    const read = async () => {
      try {
        setApprovals(await approvalApi.pending(client));
      } catch {
        // A failed poll is not worth a message on screen: the next one is
        // three seconds away, and an approval nobody answers is refused.
      }
    };
    void read();
    const timer = setInterval(() => void read(), APPROVAL_POLL_MS);
    return () => clearInterval(timer);
  }, [client, ready]);

  const send = useCallback(
    async (request: string) => {
      setProblem("");
      try {
        let current = thread;
        let fresh = false;
        if (!current) {
          const opened = await conversationApi.open(client);
          current = { ...opened, messages: [] };
          adopt(current);
          told.current.onOpened?.(opened.id);
          fresh = true;
        }
        const into = current.id;
        // A folder travels with the first request only. After that it is the
        // thread's, changed through `chooseFolder`; sending it again would be
        // choosing it again, every time.
        const message = await conversationApi.send(
          client,
          into,
          request,
          fresh ? directions : { ...directions, folder: "" },
        );
        setThread((now) =>
          now && now.id === into ? { ...now, messages: [...now.messages, message] } : now,
        );
        if (fresh) void reread(into);
        told.current.onChanged?.();
      } catch (error) {
        fail(error);
      }
    },
    [client, thread, directions, adopt, fail, reread],
  );

  const chooseFolder = useCallback(
    async (folder: string) => {
      setProblem("");
      if (!thread) {
        setDirections((now) => ({ ...now, folder }));
        return;
      }
      try {
        const read = await conversationApi.setFolder(client, thread.id, folder);
        if (shown.current !== thread.id) return;
        setThread(read);
        setDirections((now) => ({ ...now, folder: read.folder ?? folder }));
      } catch (error) {
        fail(error);
      }
    },
    [client, thread, fail],
  );

  const stop = useCallback(async () => {
    if (!running || !thread) return;
    try {
      await stopObjective(client, running.id);
      await reread(thread.id);
      told.current.onChanged?.();
    } catch (error) {
      fail(error);
    }
  }, [client, running, thread, reread, fail]);

  // A question is shown in the thread whose work asked it, never in whichever
  // one is on screen: a scheduled run that asked while the person was starting
  // a new task put its question in the new task. One no thread holds is shown
  // where nothing is open, which is the only place it can still be answered.
  const threadId = thread?.id ?? null;
  const asked = useMemo(
    () => approvals.filter((item) => (item.conversation_id ?? null) === threadId),
    [approvals, threadId],
  );

  const decide = useCallback(
    async (approvalId: string, approved: boolean) => {
      try {
        await decideApproval(client, approvalId, approved);
        setApprovals((waiting) => waiting.filter((item) => item.id !== approvalId));
      } catch (error) {
        fail(error);
      }
    },
    [client, fail],
  );

  return {
    ready,
    problem,
    thread,
    messages: thread?.messages ?? [],
    activity: running ? (trails[running.id] ?? []) : [],
    trails,
    approvals: asked,
    employees,
    directions,
    setDirections,
    models,
    busy,
    send,
    chooseFolder,
    stop,
    decide,
  };
}
