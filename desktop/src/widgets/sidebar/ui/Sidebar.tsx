/**
 * The sidebar: a new task, the threads of this workspace, and the way to
 * everything that is not a conversation.
 *
 * It lists and navigates; it opens nothing itself. Which thread is shown and
 * which page is up are the frame's state, told to it and told back through the
 * callbacks, so the list and the page cannot disagree about where the person is.
 *
 * Laid out the way the chat windows people already use are: the name on its
 * own line, a short list of places as flat rows, and the threads under a quiet
 * heading - one line each, with their state as a dot only when there is one
 * worth noticing. A second line of "Done" under every finished thread was a
 * list that said the same word twenty times.
 *
 * Ordered by what it costs to press. Ask is first because it is the cheapest
 * thing here - a question, answered - and New task is the one that sets work
 * going; then a clock, then what work can act through, then what came back.
 * The inbox is last because it is where a person arrives rather than where
 * they set out, and it says "Inbox" - what is in it is already approvals, and
 * the badge beside it is what makes anybody look.
 *
 * Only what a person acts on from here is listed. The Work Center, the
 * workforce and the traces are read rather than acted on - they answer "what
 * has been going on", not "what happens next" - so they live under settings,
 * where the rest of the machine is inspected. A place in this list is a place
 * somebody starts work from.
 *
 * A thread nobody has said anything in is not listed. Those exist - a window
 * used to open one every time it started - and a list of blank rows is a list
 * that teaches a person to stop reading it.
 */

import { useMemo, useRef, useState } from "react";

import {
  ThreadActions,
  ThreadTitleField,
  deleteThread,
  renameThread,
} from "../../../features/manage-thread";
import { report, useRuntime } from "../../../shared/api";
import { avatarUrl } from "../../../shared/assets";
import { describe } from "../../../shared/lib";
import {
  ClockIcon,
  ComposeIcon,
  GearIcon,
  InboxIcon,
  PanelIcon,
  PlugIcon,
  SearchIcon,
  SparkIcon,
} from "../../../shared/ui";
import { groupByDay, markFor } from "../model/presentation";
import { useThreads } from "../model/useThreads";
import { useApprovalBadge } from "../model/useApprovalBadge";

interface Props {
  selected: string | null;
  settingsOpen: boolean;
  /** Whether the schedules page is the one shown. */
  schedulesOpen?: boolean;
  approvalsOpen?: boolean;
  /** Changes when the page did something the list should show at once. */
  refresh: number;
  onSelect: (conversationId: string) => void;
  onNew: () => void;
  onAsk: () => void;
  onSettings: (section?: "plugins") => void;
  onSchedules?: () => void;
  onApprovals?: () => void;
  /** Make a schedule out of this thread's request. */
  onRepeat?: (conversationId: string) => void;
  onClose: () => void;
  /** A thread was renamed or deleted from the list. */
  onThreadChanged?: (conversationId: string, change: "renamed" | "deleted") => void;
}

export function Sidebar({
  selected,
  settingsOpen,
  schedulesOpen = false,
  approvalsOpen = false,
  refresh,
  onSelect,
  onNew,
  onAsk,
  onSettings,
  onSchedules,
  onApprovals,
  onRepeat,
  onClose,
  onThreadChanged,
}: Props) {
  const client = useRuntime();
  const approvalBadge = useApprovalBadge(client);
  const [changed, setChanged] = useState(0);
  const { threads, workspace, spent } = useThreads(client, refresh + changed);
  const [search, setSearch] = useState("");
  const [searching, setSearching] = useState(false);
  const [menuFor, setMenuFor] = useState<string | null>(null);
  const [renaming, setRenaming] = useState<string | null>(null);
  // Shown in place of the list's answer until the list is read again, so a
  // rename does not flicker back to the old name for one poll.
  const [titles, setTitles] = useState<Record<string, string>>({});
  const [gone, setGone] = useState<Set<string>>(new Set());
  const field = useRef<HTMLInputElement>(null);

  const groups = useMemo(() => {
    const wanted = search.trim().toLowerCase();
    const shown = threads
      .filter((thread) => thread.messages > 0 && !gone.has(thread.id))
      .map((thread) => (titles[thread.id] ? { ...thread, title: titles[thread.id] } : thread))
      .filter((thread) => !wanted || thread.title.toLowerCase().includes(wanted));
    return groupByDay(shown);
  }, [threads, search, titles, gone]);

  const openSearch = () => {
    setSearching(true);
    setTimeout(() => field.current?.focus(), 0);
  };

  const rename = async (id: string, title: string | null) => {
    setRenaming(null);
    if (!title) return;
    setTitles((known) => ({ ...known, [id]: title }));
    try {
      await renameThread(client, id, title);
      setChanged((count) => count + 1);
      onThreadChanged?.(id, "renamed");
    } catch (error) {
      setTitles(({ [id]: _dropped, ...rest }) => rest);
      void report(describe(error));
    }
  };

  const remove = async (id: string) => {
    try {
      await deleteThread(client, id);
      setGone((known) => new Set(known).add(id));
      setChanged((count) => count + 1);
      onThreadChanged?.(id, "deleted");
    } catch (error) {
      void report(describe(error));
    }
  };

  return (
    <aside className="rail" aria-label="Tasks">
      <div className="rail-top" data-tauri-drag-region>
        <button type="button" className="icobtn" aria-label="Collapse sidebar" onClick={onClose}>
          <PanelIcon />
        </button>
      </div>

      <div className="rail-brand" data-tauri-drag-region>
        <span className="logo" data-tauri-drag-region>
          Prometheus
        </span>
        <button type="button" className="icobtn" aria-label="Search tasks" onClick={openSearch}>
          <SearchIcon />
        </button>
      </div>

      <nav className="rail-nav" aria-label="Places">
        <button type="button" className="navrow" onClick={onAsk}>
          <SparkIcon />
          Ask
        </button>
        <button type="button" className="navrow" onClick={onNew}>
          <ComposeIcon />
          New task
        </button>
        {onSchedules && (
          <button
            type="button"
            className={schedulesOpen ? "navrow on" : "navrow"}
            aria-current={schedulesOpen ? "page" : undefined}
            onClick={onSchedules}
          >
            <ClockIcon />
            Scheduled
          </button>
        )}
        <button type="button" className="navrow" onClick={() => onSettings("plugins")}>
          <PlugIcon />
          Plugins
        </button>
        {onApprovals && (
          <button
            type="button"
            className={approvalsOpen ? "navrow on" : "navrow"}
            aria-current={approvalsOpen ? "page" : undefined}
            title="Approvals waiting for you"
            onClick={onApprovals}
          >
            <InboxIcon />
            Inbox
            {approvalBadge.count > 0 && <span className="nav-count">{approvalBadge.count}</span>}
            {approvalBadge.fresh > 0 && <span className="sr-only" role="status">{approvalBadge.fresh} new approval request(s)</span>}
          </button>
        )}
      </nav>

      {searching && (
        <label className="search">
          <SearchIcon />
          <input
            ref={field}
            value={search}
            placeholder="Search tasks"
            aria-label="Search tasks"
            onChange={(event) => setSearch(event.target.value)}
            onKeyDown={(event) => {
              if (event.key === "Escape") {
                setSearch("");
                setSearching(false);
              }
            }}
            onBlur={() => {
              if (!search) setSearching(false);
            }}
          />
        </label>
      )}

      <nav className="threads" aria-label="Threads">
        {groups.length === 0 && (
          <>
            <p className="tgroup">Recents</p>
            <p className="tempty">{search ? "Nothing by that name." : "No tasks yet"}</p>
          </>
        )}
        {groups.map((group) => (
          <div key={group.label}>
            <p className="tgroup">{group.label}</p>
            {group.threads.map((thread) => {
              const mark = markFor(thread.status);
              const on = thread.id === selected;
              const classes = ["thread", on && "on", menuFor === thread.id && "menu-open"]
                .filter(Boolean)
                .join(" ");
              if (renaming === thread.id) {
                return (
                  <div key={thread.id} className={`${classes} editing`}>
                    <ThreadTitleField
                      initial={thread.title}
                      onDone={(title) => void rename(thread.id, title)}
                    />
                  </div>
                );
              }
              return (
                <div key={thread.id} className={classes}>
                  <button
                    type="button"
                    className="thread-open"
                    aria-current={on ? "page" : undefined}
                    title={mark.label}
                    onClick={() => onSelect(thread.id)}
                    onDoubleClick={() => setRenaming(thread.id)}
                  >
                    <span className="thread-title">
                      {thread.kind === "ASK" && <small className="thread-kind">Ask</small>}
                      {thread.title || "Untitled"}
                    </span>
                    {mark.tone && (
                      <i className={`pip ${mark.tone}`} aria-label={mark.label} role="img" />
                    )}
                  </button>
                  <ThreadActions
                    title={thread.title}
                    open={menuFor === thread.id}
                    onOpen={(open) => setMenuFor(open ? thread.id : null)}
                    onRename={() => setRenaming(thread.id)}
                    onDelete={() => remove(thread.id)}
                    onRepeat={onRepeat ? () => onRepeat(thread.id) : undefined}
                  />
                </div>
              );
            })}
          </div>
        ))}
      </nav>

      <div className="rail-foot">
        <button
          type="button"
          className={settingsOpen ? "acct on" : "acct"}
          aria-label="Settings"
          onClick={() => onSettings()}
        >
          <img className="avatar" src={avatarUrl} alt="" />
          <span className="acct-meta">
            <b>{workspace || "Workspace"}</b>
            <span>
              Settings
              {spent !== null && ` · $${spent.toFixed(2)} spent`}
            </span>
          </span>
          <GearIcon className="acct-gear" />
        </button>
      </div>
    </aside>
  );
}
