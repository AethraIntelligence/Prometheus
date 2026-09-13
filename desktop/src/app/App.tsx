import { useState } from "react";

import { ChatPage } from "../pages/chat";
import { SettingsPage } from "../pages/settings";
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
 * Settings take the whole window and bring their own menu. A list of threads
 * beside a screen that has nothing to do with any of them is a column of dead
 * weight, and the way back is one button that says so.
 */
export function App({ client, baseUrl }: { client?: RuntimeClient; baseUrl?: string }) {
  const [showing, setShowing] = useState<"work" | "settings">("work");
  const [workspace, setWorkspace] = useState(0);
  const [open, setOpen] = useState<string | null>(null);
  const [railOpen, setRailOpen] = useState(() => !narrow());
  const [heard, setHeard] = useState(0);
  const [renamed, setRenamed] = useState(0);
  const [section, setSection] = useState<"plugins" | undefined>(undefined);

  const switched = () => {
    setWorkspace((count) => count + 1);
    setOpen(null);
  };
  const go = (page: "work" | "settings", thread: string | null = open) => {
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
          selected={open}
          settingsOpen={false}
          refresh={heard}
          onSelect={(thread) => go("work", thread)}
          onNew={() => go("work", null)}
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
        <ChatPage
          key={workspace}
          conversationId={open}
          onOpened={setOpen}
          onChanged={() => setHeard((count) => count + 1)}
          refresh={renamed}
          onSwitched={switched}
          railOpen={railOpen}
          onOpenRail={() => setRailOpen(true)}
        />
      </div>
    </RuntimeProvider>
  );
}
