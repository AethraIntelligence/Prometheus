/**
 * The switches the runtime starts with: capabilities, approvals, timeouts and
 * limits.
 *
 * Grouped as the runtime groups them and drawn in the order it lists them - the
 * window adds no setting of its own and decides nothing about a value. A value
 * set in the environment the runtime was started with is shown and not
 * offered, because a change here would be saved and then silently lose to it.
 */

import type { Setting } from "../../../../entities/setting";
import {
  ResetAllSettings,
  SettingControl,
  shownDefault,
} from "../../../../features/change-setting";
import { RestartButton } from "../../../../features/restart-runtime";
import { useRuntime } from "../../../../shared/api";
import { useGeneral } from "../../model/useGeneral";

function groups(settings: Setting[]): [string, Setting[]][] {
  const found = new Map<string, Setting[]>();
  for (const setting of settings) {
    found.set(setting.group, [...(found.get(setting.group) ?? []), setting]);
  }
  return [...found.entries()];
}

export function GeneralSection() {
  const client = useRuntime();
  const general = useGeneral(client);

  return (
    <>
      <p className="lede">
        What the platform may do and how long it waits. A change is saved at once and takes
        effect the next time Prometheus starts.
      </p>

      {general.restartNeeded && (
        <div className="restart-note restart-banner" role="status">
          <p>
            Saved. Restart Prometheus to apply the changes marked <em>after restart</em>.
          </p>
          <RestartButton label="Restart now" primary />
        </div>
      )}
      {general.problem && (
        <p className="problem" role="alert">
          {general.problem}
        </p>
      )}
      {!general.available && general.ready && (
        <p className="note">This runtime was started without editable settings.</p>
      )}

      {groups(general.settings).map(([group, settings]) => (
        <section className="panel" key={group} aria-label={group}>
          <div className="panel-head">
            <h2>{group}</h2>
          </div>
          <div className="card">
            {settings.map((setting) => (
              <div className="setting-row" key={setting.key}>
                  <div className="setting-text">
                    <label htmlFor={`setting-${setting.key}`}>
                      {setting.label}
                      {setting.restart_needed && (
                        <span className="badge quiet">after restart</span>
                      )}
                    </label>
                    <p className="note">{setting.help}</p>
                    {setting.saved && (
                      <button
                        type="button"
                        className="setting-reset"
                        disabled={!general.ready}
                        onClick={() => void general.reset(setting.key)}
                        aria-label={`Reset ${setting.label} to default`}
                      >
                        Reset to default · {shownDefault(setting)}
                      </button>
                    )}
                    {setting.locked_by && (
                      <p className="note">
                        Set by <code>{setting.locked_by}</code> in the environment.
                      </p>
                    )}
                  </div>
                  <div className="setting-control">
                    <SettingControl
                      setting={setting}
                      onChange={(value) => general.change(setting.key, value)}
                      disabled={!general.ready}
                    />
                  </div>
              </div>
            ))}
          </div>
        </section>
      ))}

      {general.settings.length > 0 && (
        <section className="panel settings-reset" aria-label="Restart">
          <div className="setting-row">
            <div className="setting-text">
              <p className="setting-title">Restart Prometheus</p>
              <p className="note">
                Stops and starts the runtime so it rereads its settings, its .env file and its
                code. Takes a few seconds; the window reloads when it is back.
              </p>
            </div>
            <RestartButton label="Restart" />
          </div>
        </section>
      )}

      {general.settings.length > 0 && (
        <section className="panel settings-reset" aria-label="Reset">
          <div className="setting-row">
            <div className="setting-text">
              <p className="setting-title">Reset to defaults</p>
              <p className="note">
                Forget every choice made on this screen. Each setting goes back to your .env
                file or, where that says nothing, the platform&apos;s default.
              </p>
            </div>
            <ResetAllSettings
              onReset={() => general.reset()}
              disabled={!general.ready || !general.anySaved}
            />
          </div>
        </section>
      )}
    </>
  );
}
