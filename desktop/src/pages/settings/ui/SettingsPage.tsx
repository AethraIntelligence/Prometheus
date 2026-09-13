/**
 * Settings: one screen per thing, and a menu down the side.
 *
 * Deliberately not an administration console. Everything else configurable is a
 * file on this machine that the runtime already reads, and a screen that
 * mirrored those files would be a second place to change them - the one that
 * goes stale. What is here is what has no file: a workspace, a document
 * somebody brought, what the platform has remembered, the models it may reach
 * and the plugins it can act through.
 *
 * It used to be all five of those in one column, every form standing open,
 * navigated by jump links. That is a document, not a settings screen: the thing
 * a person came to change was always somewhere below the thing they did not.
 * One section is shown at a time and each one asks for its own state, so the
 * screen also stops reading five parts of the runtime to show one.
 */

import { useState } from "react";

import { BackIcon, BookIcon, BoxIcon, FolderIcon, PlugIcon, SparkIcon } from "../../../shared/ui";
import { DocumentsSection } from "./sections/DocumentsSection";
import { MemorySection } from "./sections/MemorySection";
import { ModelsSection } from "./sections/ModelsSection";
import { PluginsSection } from "./sections/PluginsSection";
import { WorkspacesSection } from "./sections/WorkspacesSection";

type SectionId = "workspaces" | "documents" | "memory" | "models" | "plugins";

interface Section {
  id: SectionId;
  label: string;
  group: string;
  icon: typeof FolderIcon;
}

/** The order they are read in: what work happens inside, then what it happens with. */
const SECTIONS: Section[] = [
  { id: "workspaces", label: "Workspaces", group: "This machine", icon: FolderIcon },
  { id: "documents", label: "Documents", group: "This machine", icon: BookIcon },
  { id: "memory", label: "Memory", group: "This machine", icon: SparkIcon },
  { id: "models", label: "Providers and models", group: "Capabilities", icon: BoxIcon },
  { id: "plugins", label: "Plugins", group: "Capabilities", icon: PlugIcon },
];

interface Props {
  onSwitched?: () => void;
  onBack?: () => void;
  /** The section to open on, when the way in was a place rather than "Settings". */
  initial?: SectionId;
}

export function SettingsPage({ onSwitched, onBack, initial }: Props = {}) {
  const [open, setOpen] = useState<SectionId>(initial ?? "workspaces");
  const current = SECTIONS.find((section) => section.id === open) ?? SECTIONS[0];

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

      <main className="main settings-main">
        <header className="head" data-tauri-drag-region>
          <h1 data-tauri-drag-region>{current.label}</h1>
        </header>
        <div className="stream">
          <section className="col settings" aria-label={current.label}>
            {open === "workspaces" && <WorkspacesSection onSwitched={onSwitched} />}
            {open === "documents" && <DocumentsSection />}
            {open === "memory" && <MemorySection />}
            {open === "models" && <ModelsSection />}
            {open === "plugins" && <PluginsSection />}
          </section>
        </div>
      </main>
    </div>
  );
}
