import { useCallback, useState } from "react";

import type { ConversationKind } from "../entities/conversation";
import { ApprovalInboxPage } from "../pages/approval-inbox";
import { ChatPage } from "../pages/chat";
import { SchedulesPage } from "../pages/schedules";
import { SettingsPage } from "../pages/settings";
import { WorkCenterPage } from "../pages/work-center";
import { RuntimeProvider, type RuntimeClient } from "../shared/api";
import { Sidebar } from "../widgets/sidebar";

/** Below this the sidebar is a drawer over the page rather than a column beside it. */
const NARROW = 900;

const narrow = () => typeof window !== "undefined" && window.innerWidth < NARROW;

/**
 * The application: one provider, a sidebar, a page, and which workspace they
 * are of.
 *
 * The app layer holds state and no rules: which page is shown, which thread is
 * open (none is a new task), whether the sidebar is out, and two counters - one
 * that changes when the workspace does and one that changes when the page did
 * something the sidebar should show at once. The first remounts everything,
 * because the thread, the history and the documents all belong to a workspace,
 * and a screen left over from the previous one would be showing another
 * context's work under the new context's name.
 *
 * Scheduled work is a page beside the conversation, not inside settings: it
 * is read the way the thread list is - what is coming, what happened - and
 * its results open as the threads they were written into.
 *
 * Settings take the whole window and bring their own menu. A list of threads
 * beside a screen that has nothing to do with any of them is a column of dead
 * weight, and the way back is one button that says so.
 */
export function App({ client, baseUrl }: { client?: RuntimeClient; baseUrl?: string }) {
  const [showing, setShowing] = useState<"work" | "settings" | "schedules" | "center" | "approvals">("work");
  const [workspace, setWorkspace] = useState(0);
  const [open, setOpen] = useState<string | null>(null);
  const [newKind, setNewKind] = useState<ConversationKind>("TASK");
  const [railOpen, setRailOpen] = useState(() => !narrow());
  const [heard, setHeard] = useState(0);
  const [renamed, setRenamed] = useState(0);
  const [section, setSection] = useState<"plugins" | "general" | undefined>(undefined);
  // A thread somebody asked to repeat, held until the schedules page has read it.
  const [repeat, setRepeat] = useState<string | null>(null);
  const [centerObjective, setCenterObjective] = useState<string | null>(null);
  const repeatTaken = useCallback(() => setRepeat(null), []);

  const switched = () => {
    setWorkspace((count) => count + 1);
    setOpen(null);
  };
  const go = (page: "work" | "settings" | "schedules" | "center" | "approvals", thread: string | null = open) => {
    setShowing(page);
    setOpen(thread);
    if (narrow()) setRailOpen(false);
  };

  if (showing === "settings") {
    return (
      <RuntimeProvider client={client} baseUrl={baseUrl}>
        <SettingsPage
          key={workspace}
          initial={section}
          onSwitched={switched}
          onBack={() => go("work")}
        />
      </RuntimeProvider>
    );
  }

  return (
    <RuntimeProvider client={client} baseUrl={baseUrl}>
      <div className={railOpen ? "app" : "app rail-closed"}>
        <Sidebar
          key={`rail-${workspace}`}
          selected={showing === "work" ? open : null}
          settingsOpen={false}
          schedulesOpen={showing === "schedules"}
          workCenterOpen={showing === "center"}
          approvalsOpen={showing === "approvals"}
          onWorkCenter={() => {
            setCenterObjective(null);
            go("center", null);
          }}
          onApprovals={() => go("approvals", null)}
          onSchedules={() => go("schedules")}
          onRepeat={(thread) => {
            setRepeat(thread);
            go("schedules");
          }}
          refresh={heard}
          onSelect={(thread) => go("work", thread)}
          onNew={() => {
            setNewKind("TASK");
            go("work", null);
          }}
          onAsk={() => {
            setNewKind("ASK");
            go("work", null);
          }}
          onSettings={(to) => {
            setSection(to);
            go("settings");
          }}
          onClose={() => setRailOpen(false)}
          onThreadChanged={(thread, change) => {
            if (thread !== open) return;
            if (change === "deleted") setOpen(null);
            else setRenamed((count) => count + 1);
          }}
        />
        <div
          className={railOpen ? "scrim on" : "scrim"}
          aria-hidden="true"
          onClick={() => setRailOpen(false)}
        />
        {showing === "schedules" ? (
          <SchedulesPage
            key={`schedules-${workspace}`}
            railOpen={railOpen}
            onOpenRail={() => setRailOpen(true)}
            onOpenThread={(thread) => go("work", thread)}
            repeat={repeat}
            onRepeatTaken={repeatTaken}
            onOpenSettings={() => {
              setSection("general");
              go("settings");
            }}
          />
        ) : showing === "center" ? (
          <WorkCenterPage
            key={`center-${workspace}`}
            initialObjectiveId={centerObjective}
            railOpen={railOpen}
            onOpenRail={() => setRailOpen(true)}
            onOpenThread={(thread) => go("work", thread)}
          />
        ) : showing === "approvals" ? (
          <ApprovalInboxPage
            key={`approvals-${workspace}`}
            railOpen={railOpen}
            onOpenRail={() => setRailOpen(true)}
            onOpenThread={(thread) => go("work", thread)}
            onOpenWork={(objective) => {
              setCenterObjective(objective);
              go("center", null);
            }}
          />
        ) : (
          <ChatPage
            key={`${workspace}:${open ?? newKind}`}
            conversationId={open}
            newKind={newKind}
            onOpened={setOpen}
            onChanged={() => setHeard((count) => count + 1)}
            refresh={renamed}
            onSwitched={switched}
            railOpen={railOpen}
            onOpenRail={() => setRailOpen(true)}
          />
        )}
      </div>
    </RuntimeProvider>
  );
}
