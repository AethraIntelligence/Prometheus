import { useEffect, useState } from "react";

import type { Setting, SettingValue } from "../../../entities/setting";

/**
 * The control for one setting, chosen by its kind.
 *
 * A switch and a list save the moment they change; a field saves when it is
 * left or Enter is pressed, because saving per keystroke would send "3" on the
 * way to "30" and have the runtime refuse half a number.
 */
export function SettingControl({
  setting,
  onChange,
  disabled,
}: {
  setting: Setting;
  onChange: (value: SettingValue) => Promise<void>;
  disabled?: boolean;
}) {
  const locked = disabled || setting.locked_by !== "";
  const id = `setting-${setting.key}`;

  if (setting.kind === "BOOLEAN") {
    const on = setting.value === true;
    return (
      <button
        id={id}
        type="button"
        role="switch"
        aria-checked={on}
        aria-label={setting.label}
        className={on ? "switch on" : "switch"}
        disabled={locked}
        onClick={() => void onChange(!on)}
      >
        <span className="switch-knob" />
      </button>
    );
  }

  if (setting.kind === "CHOICE") {
    return (
      <select
        id={id}
        aria-label={setting.label}
        value={String(setting.value ?? "")}
        disabled={locked}
        onChange={(event) => void onChange(event.target.value)}
      >
        {setting.choices.map((choice) => (
          <option key={choice} value={choice}>
            {choice}
          </option>
        ))}
      </select>
    );
  }

  return <SettingField id={id} setting={setting} locked={locked} onChange={onChange} />;
}

/** The fallback value in words, for "Reset to default" to say what it resets to. */
export function shownDefault(setting: Setting): string {
  const value = setting.default;
  if (typeof value === "boolean") return value ? "on" : "off";
  if (Array.isArray(value)) return value.length ? value.join(", ") : "none";
  if (value === null || value === "") return "empty";
  return String(value);
}

function shown(setting: Setting): string {
  if (setting.value === null) return "";
  if (Array.isArray(setting.value)) return setting.value.join("\n");
  return String(setting.value);
}

function SettingField({
  id,
  setting,
  locked,
  onChange,
}: {
  id: string;
  setting: Setting;
  locked: boolean;
  onChange: (value: SettingValue) => Promise<void>;
}) {
  const [draft, setDraft] = useState(shown(setting));

  // What the runtime holds wins over a draft once it answers, including after
  // a refusal: the field then shows the value that is actually saved.
  useEffect(() => setDraft(shown(setting)), [setting]);

  const commit = () => {
    if (draft === shown(setting)) return;
    if (setting.kind === "LIST") {
      void onChange(draft.split("\n").map((line) => line.trim()).filter(Boolean));
    } else if ((setting.kind === "INTEGER" || setting.kind === "NUMBER") && draft.trim() !== "") {
      void onChange(Number(draft));
    } else {
      void onChange(draft);
    }
  };

  if (setting.kind === "LIST") {
    return (
      <textarea
        id={id}
        aria-label={setting.label}
        rows={3}
        value={draft}
        disabled={locked}
        placeholder="One per line"
        onChange={(event) => setDraft(event.target.value)}
        onBlur={commit}
      />
    );
  }

  const numeric = setting.kind === "INTEGER" || setting.kind === "NUMBER";
  return (
    <input
      id={id}
      aria-label={setting.label}
      type={numeric ? "number" : "text"}
      inputMode={numeric ? (setting.kind === "INTEGER" ? "numeric" : "decimal") : undefined}
      step={setting.kind === "INTEGER" ? 1 : "any"}
      min={setting.minimum ?? undefined}
      value={draft}
      disabled={locked}
      placeholder={setting.optional ? "Default" : undefined}
      onChange={(event) => setDraft(event.target.value)}
      onBlur={commit}
      onKeyDown={(event) => {
        if (event.key === "Enter") event.currentTarget.blur();
        if (event.key === "Escape") setDraft(shown(setting));
      }}
    />
  );
}
