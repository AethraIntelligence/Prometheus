/**
 * Settings: one screen per thing, and a menu down the side.
 *
 * General is the switches the runtime starts with - capabilities, approvals,
 * timeouts - saved to one file beside the database that the runtime reads
 * ahead of `.env` (ADR 0023). Not every field: keys, paths, the store and the
 * address this window talks to stay where changing them cannot lock the window
 * out. The rest is what has no file: a workspace, a document somebody brought,
 * what the platform has remembered, the models it may reach and the plugins it
 * can act through.
 *
 * It also holds what is read rather than changed - the Work Center, the
 * workforce and the traces. Those are analytics: they answer what has been
 * going on, which is a question about this installation and not a place work
 * starts from, and in the main list they sat between the two rows people
 * actually press. They are handed in as panels rather than imported, because a
 * page importing another page is the one import this frontend's layering does
 * not allow - the frame that already owns both puts them side by side.
 *
 * It used to be all five of those in one column, every form standing open,
 * navigated by jump links. That is a document, not a settings screen: the thing
 * a person came to change was always somewhere below the thing they did not.
 * One section is shown at a time and each one asks for its own state, so the
 * screen also stops reading five parts of the runtime to show one.
 */

import { useEffect, useState, type ReactNode } from "react";

import {
  BackIcon,
  BookIcon,
  BoxIcon,
  BriefcaseIcon,
  ClockIcon,
  FolderIcon,
  GearIcon,
  PeopleIcon,
  PlugIcon,
  PulseIcon,
  SparkIcon,
  StopIcon,
  WarningIcon,
} from "../../../shared/ui";
import { BackupsSection } from "./sections/BackupsSection";
import { DocumentsSection } from "./sections/DocumentsSection";
import { GeneralSection } from "./sections/GeneralSection";
import { MemorySection } from "./sections/MemorySection";
import { ModelsSection } from "./sections/ModelsSection";
import { PluginsSection } from "./sections/PluginsSection";
import { PermissionsSection } from "./sections/PermissionsSection";
import { ProcessesSection } from "./sections/ProcessesSection";
import { StopSection } from "./sections/StopSection";
import { WorkspacesSection } from "./sections/WorkspacesSection";

export type SectionId =
  | "general"
  | "permissions"
  | "stop"
  | "workspaces"
  | "documents"
  | "memory"
  | "backups"
  | "models"
  | "plugins"
  | "work-center"
  | "workforce"
  | "observability"
  | "processes";

interface Section {
  id: SectionId;
  label: string;
  group: string;
  icon: typeof FolderIcon;
}

/**
 * The order they are read in: how the platform behaves at all, what work
 * happens inside, then what it happens with.
 */
const SECTIONS: Section[] = [
  { id: "general", label: "General", group: "Prometheus", icon: GearIcon },
  { id: "permissions", label: "Permissions", group: "Prometheus", icon: WarningIcon },
  { id: "stop", label: "Emergency stop", group: "Prometheus", icon: StopIcon },
  { id: "workspaces", label: "Workspaces", group: "This machine", icon: FolderIcon },
  { id: "documents", label: "Documents", group: "This machine", icon: BookIcon },
  { id: "memory", label: "Memory", group: "This machine", icon: SparkIcon },
  { id: "backups", label: "Backups and updates", group: "This machine", icon: BoxIcon },
  { id: "models", label: "Providers and models", group: "Capabilities", icon: BoxIcon },
  { id: "plugins", label: "Plugins", group: "Capabilities", icon: PlugIcon },
  { id: "work-center", label: "Work Center", group: "Analytics", icon: BriefcaseIcon },
  { id: "workforce", label: "Workforce", group: "Analytics", icon: PeopleIcon },
  { id: "observability", label: "Observability", group: "Analytics", icon: PulseIcon },
  { id: "processes", label: "Processes", group: "Analytics", icon: ClockIcon },
];

interface Props {
  onSwitched?: () => void;
  onBack?: () => void;
  /** The section to open on, when the way in was a place rather than "Settings". */
  initial?: SectionId;
  /**
   * Screens the frame owns, shown in place of this page's own. Each brings its
   * own header, so a panel fills the column the way a section does.
   */
  panels?: Partial<Record<SectionId, ReactNode>>;
}

export function SettingsPage({ onSwitched, onBack, initial, panels }: Props = {}) {
  const [open, setOpen] = useState<SectionId>(initial ?? "general");
  // A section reached from somewhere else - a workforce reason that names where
  // to fix it - moves this screen without remounting it.
  useEffect(() => {
    if (initial) setOpen(initial);
  }, [initial]);
  const current = SECTIONS.find((section) => section.id === open) ?? SECTIONS[0];
  const panel = panels?.[open];

  return (
    <div className="settings-shell">
      <nav className="settings-rail" aria-label="Settings">
        {onBack && (
          <button type="button" className="settings-back" onClick={onBack}>
            <BackIcon />
            Back to app
          </button>
        )}
        {SECTIONS.map((section, index) => (
          <div key={section.id}>
            {SECTIONS[index - 1]?.group !== section.group && (
              <p className="settings-group">{section.group}</p>
            )}
            <button
              type="button"
              className={section.id === open ? "settings-tab on" : "settings-tab"}
              aria-current={section.id === open ? "page" : undefined}
              onClick={() => setOpen(section.id)}
            >
              <section.icon />
              {section.label}
            </button>
          </div>
        ))}
      </nav>

      {panel ?? (
      <main className="main settings-main">
        <header className="head" data-tauri-drag-region>
          <h1 data-tauri-drag-region>{current.label}</h1>
        </header>
        <div className="stream">
          <section className="col settings" aria-label={current.label}>
            {open === "general" && <GeneralSection />}
            {open === "permissions" && <PermissionsSection />}
            {open === "stop" && <StopSection />}
            {open === "processes" && <ProcessesSection />}
            {open === "workspaces" && <WorkspacesSection onSwitched={onSwitched} />}
            {open === "documents" && <DocumentsSection />}
            {open === "memory" && <MemorySection />}
            {open === "backups" && <BackupsSection />}
            {open === "models" && <ModelsSection />}
            {open === "plugins" && <PluginsSection />}
          </section>
        </div>
      </main>
      )}
    </div>
  );
}
