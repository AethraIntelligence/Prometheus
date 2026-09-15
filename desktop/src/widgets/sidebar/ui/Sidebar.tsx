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
  PanelIcon,
  PlugIcon,
  SearchIcon,
} from "../../../shared/ui";
import { groupByDay, markFor } from "../model/presentation";
import { useThreads } from "../model/useThreads";

interface Props {
  selected: string | null;
  settingsOpen: boolean;
  /** Whether the schedules page is the one shown. */
  schedulesOpen?: boolean;
  /** Changes when the page did something the list should show at once. */
  refresh: number;
  onSelect: (conversationId: string) => void;
  onNew: () => void;
  onSettings: (section?: "plugins") => void;
  onSchedules?: () => void;
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
  refresh,
  onSelect,
  onNew,
  onSettings,
  onSchedules,
  onRepeat,
  onClose,
  onThreadChanged,
}: Props) {
  const client = useRuntime();
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
                    <span className="thread-title">{thread.title || "Untitled"}</span>
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
