import { useId, useState, type FormEvent } from "react";

import { EmployeeChoice, type Employee } from "../../../entities/employee";
import type { Plugin, PluginSetting, RuntimeState } from "../../../entities/integration";
import { openExternal } from "../../../shared/api";

/**
 * Everything installing one plugin asks, on one form.
 *
 * The plugin's own settings, and who may use it - preselected with the core's
 * suggestion, because a plugin nobody may use is a plugin that looks broken.
 * A token already kept on this machine is not asked for again. Where the
 * machine lacks what the server needs, that is said before anything is typed.
 */
export function InstallPluginForm({
  plugin,
  runtime,
  employees,
  onInstall,
}: {
  plugin: Plugin;
  runtime?: RuntimeState;
  employees: Employee[];
  onInstall: (values: Record<string, string>, employees: string[]) => Promise<void>;
}) {
  const [values, setValues] = useState<Record<string, string>>({});
  const [chosen, setChosen] = useState<string[]>(plugin.suggested_employees);
  const [busy, setBusy] = useState(false);
  const formId = useId();

  const missing = plugin.settings.some(
    (setting) =>
      setting.required &&
      !setting.stored &&
      !setting.provided &&
      !(values[setting.key] ?? "").trim(),
  );

  const submit = async (event: FormEvent) => {
    event.preventDefault();
    if (missing || busy) return;
    setBusy(true);
    try {
      const sent = Object.fromEntries(
        Object.entries(values).filter(([, value]) => value.trim() !== ""),
      );
      await onInstall(sent, chosen);
    } finally {
      setBusy(false);
    }
  };

  // A setting this installation supplies moves out of the way, with the steps
  // for making one's own, so the ordinary path is a button and nothing else.
  const own = plugin.settings.filter((setting) => setting.provided);
  const steps =
    plugin.setup.length > 0 ? (
      <ol className="plugin-setup">
        {plugin.setup.map((step, index) => (
          <li key={index}>
            {step.text}
            {step.url && (
              <>
                {" "}
                <button type="button" className="linkish" onClick={() => void openExternal(step.url)}>
                  Open
                </button>
              </>
            )}
          </li>
        ))}
      </ol>
    ) : null;
  const renderSetting = (setting: PluginSetting) => (
    <div key={setting.key} className="setting">
          <span className="setting-label">
            <label htmlFor={`${formId}-${setting.key}`}>{setting.label}</label>
            {setting.help_url && (
              <button
                type="button"
                className="linkish"
                onClick={() => void openExternal(setting.help_url)}
              >
                Get one
              </button>
            )}
          </span>
          <input
            id={`${formId}-${setting.key}`}
            type={setting.kind === "SECRET" ? "password" : "text"}
            autoComplete="off"
            spellCheck={false}
            value={values[setting.key] ?? ""}
            placeholder={
              setting.stored
                ? "Already saved on this machine - leave empty to keep it"
                : setting.provided
                  ? "Supplied by Prometheus - leave empty to use it"
                  : setting.placeholder
            }
            onChange={(event) => setValues({ ...values, [setting.key]: event.target.value })}
            disabled={busy}
          />
          {setting.help && <small>{setting.help}</small>}
        </div>
  );

  return (
    <form className="install-plugin" onSubmit={submit} aria-label={`Install ${plugin.name}`}>
      {plugin.about && <p className="plugin-about">{plugin.about}</p>}
      <p className="meta">
        {[plugin.publisher && `by ${plugin.publisher}`, plugin.category].filter(Boolean).join(" · ")}
        {plugin.homepage && (
          <>
            {" · "}
            <button type="button" className="linkish" onClick={() => void openExternal(plugin.homepage)}>
              Learn more
            </button>
          </>
        )}
      </p>

      {!plugin.runtime_ready && runtime && (
        <p className="problem" role="alert">
          {runtime.hint}{" "}
          <button type="button" className="linkish" onClick={() => void openExternal(runtime.url)}>
            How to install it
          </button>
        </p>
      )}

      {own.length > 0 && (
        <p className="note">
          Prometheus supplies the app credentials.
          {plugin.sign_in ? " Press Install and sign in in the browser page that opens." : ""}
        </p>
      )}

      {plugin.settings.filter((setting) => !own.includes(setting)).map(renderSetting)}

      {own.length > 0 ? (
        <details className="own-credentials">
          <summary>Use your own credentials instead</summary>
          {steps}
          {own.map(renderSetting)}
        </details>
      ) : (
        steps
      )}

      {employees.length > 0 && (
        <EmployeeChoice employees={employees} chosen={chosen} onChange={setChosen} disabled={busy} />
      )}

      <p className="note">
        Reading runs on its own. Anything that changes, sends or deletes something asks you first.
      </p>
      <button type="submit" className="install-button" disabled={busy || missing || !plugin.runtime_ready}>
        {busy ? "Installing…" : "Install"}
      </button>
    </form>
  );
}
