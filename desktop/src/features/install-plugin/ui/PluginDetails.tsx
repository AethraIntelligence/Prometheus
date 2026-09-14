import { useEffect, useRef, useState, type ReactNode } from "react";

import { EmployeeChoice, type Employee } from "../../../entities/employee";
import {
  CapabilityList,
  type InstalledIntegration,
  type Plugin,
} from "../../../entities/integration";

/**
 * A connected service: how it is, who may use it, what it offers.
 *
 * Who may use it is saved on its own button rather than on every tick, so a
 * person working down the list does not reconnect a grant five times. A token
 * can be replaced without reinstalling - the name stays, the value changes, and
 * the service reconnects to pick it up.
 */
export function PluginDetails({
  integration,
  plugin,
  employees,
  onGrant,
  onReplaceSecret,
  onSignIn,
  signInNow = false,
  actions,
}: {
  integration: InstalledIntegration;
  plugin?: Plugin;
  employees: Employee[];
  onGrant: (employees: string[]) => Promise<void>;
  onReplaceSecret: (name: string, value: string) => Promise<void>;
  /** Resolves to whether the account is signed in now. */
  onSignIn?: () => Promise<boolean>;
  /** Start signing in as soon as this opens - straight after installing. */
  signInNow?: boolean;
  actions: ReactNode;
}) {
  const locked = integration.holders.filter((name) => !integration.granted_to.includes(name));
  const [chosen, setChosen] = useState<string[]>(integration.granted_to);
  const [busy, setBusy] = useState(false);
  const [secret, setSecret] = useState<{ name: string; value: string }>({ name: "", value: "" });
  const changed =
    [...chosen].sort().join(",") !== [...integration.granted_to].sort().join(",");
  const secrets = plugin?.settings.filter((setting) => setting.kind === "SECRET") ?? [];
  // Where the sign-in stands, as far as this window has asked: nothing yet,
  // waiting on the browser, or confirmed by a call that worked.
  const [signIn, setSignIn] = useState<"idle" | "waiting" | "done">("idle");
  const started = useRef(false);
  useEffect(() => {
    // Once: an installed plugin that signs in through the browser has one
    // thing left to do, and the person just asked for it by pressing Install.
    if (!signInNow || !onSignIn || !plugin?.sign_in || started.current) return;
    started.current = true;
    setBusy(true);
    void onSignIn()
      .then((done) => setSignIn(done ? "done" : "waiting"))
      .finally(() => setBusy(false));
  }, [signInNow, onSignIn, plugin]);

  const run = async (action: () => Promise<void>) => {
    setBusy(true);
    try {
      await action();
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="plugin-details">
      {plugin?.about && <p className="plugin-about">{plugin.about}</p>}
      <p className="meta">
        {integration.tool_count} capabilit{integration.tool_count === 1 ? "y" : "ies"}
        {integration.holders.length > 0
          ? ` · used by ${integration.holders.join(", ")}`
          : " · nobody can use it yet"}
      </p>

      {plugin?.sign_in && onSignIn && (
        <div className="sign-in" role="group" aria-label="Account">
          {signIn === "done" ? (
            <p className="state good">Signed in</p>
          ) : (
            <>
              <button
                type="button"
                className="sign-in-button"
                disabled={busy}
                onClick={() =>
                  void run(async () => setSignIn((await onSignIn()) ? "done" : "waiting"))
                }
              >
                {signIn === "waiting" ? "Check" : plugin.sign_in.label}
              </button>
              <small>
                {signIn === "waiting"
                  ? "Finish signing in on the page that opened in your browser, then press Check."
                  : plugin.sign_in.help}
              </small>
            </>
          )}
        </div>
      )}

      {employees.length > 0 && (
        <div className="grant">
          <EmployeeChoice
            employees={employees}
            chosen={chosen}
            locked={locked}
            onChange={setChosen}
            disabled={busy}
          />
          <button
            type="button"
            disabled={busy || !changed}
            onClick={() => void run(() => onGrant(chosen))}
          >
            Save who can use it
          </button>
        </div>
      )}

      {secrets.length > 0 && (
        <details className="replace-secret">
          <summary>Replace a saved token</summary>
          {secrets.map((setting) => (
            <label key={setting.key} className="setting">
              <span className="setting-label">{setting.label}</span>
              <input
                type="password"
                autoComplete="off"
                value={secret.name === setting.key ? secret.value : ""}
                onChange={(event) => setSecret({ name: setting.key, value: event.target.value })}
                disabled={busy}
              />
            </label>
          ))}
          <button
            type="button"
            disabled={busy || !secret.value.trim()}
            onClick={() =>
              void run(async () => {
                await onReplaceSecret(secret.name, secret.value.trim());
                setSecret({ name: "", value: "" });
              })
            }
          >
            Save and reconnect
          </button>
        </details>
      )}

      <CapabilityList tools={integration.tools} />
      <footer className="actions">{actions}</footer>
    </div>
  );
}
