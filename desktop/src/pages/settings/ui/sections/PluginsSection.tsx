/**
 * Everything this machine can reach the world with, and a shop for more.
 *
 * Two tabs, because there are two ways a capability gets here: somebody
 * installed a plugin - a service connected over MCP, from a catalog or by
 * hand - or the platform shipped with it. Below the tool boundary they are the
 * same thing (ADR 0015), so the planner, the gate and the audit already treat
 * them alike, and this screen agrees.
 *
 * Nothing here decides anything. Which plugins exist is the directory under
 * `plugins/`; whether a server can start here, whom a plugin should go to, and
 * which of its actions ask first are the runtime's answers. Searching and
 * grouping are only how a list is shown.
 *
 * What is built in has no switch. A tool reaches an employee by being listed in
 * that employee's declaration, so the honest answer to "is it on?" is who lists
 * it, and a toggle here would be a second way to say the same thing.
 */

import { useMemo, useState, type ReactNode } from "react";

import {
  PluginMark,
  PluginRow,
  type InstalledIntegration,
  type Plugin,
} from "../../../../entities/integration";
import { ToolRow, type Tool } from "../../../../entities/tool";
import { AddIntegrationForm } from "../../../../features/add-integration";
import { InstallPluginForm, PluginDetails } from "../../../../features/install-plugin";
import { IntegrationActions } from "../../../../features/manage-integration";
import { useRuntime } from "../../../../shared/api";
import { CheckIcon, Modal, PlusIcon, SearchIcon } from "../../../../shared/ui";
import { usePlugins } from "../../model/usePlugins";
import { useTools } from "../../model/useTools";

/** Offered when connecting a server by hand. The core refuses anything outside its own list. */
const CAPABILITIES = ["EMAIL", "WEB_BROWSING", "FILE_ACCESS", "CODE"];
const POPULAR = "Popular";

/** Purely presentational: which of the runtime's status words reads as trouble. */
const UNHAPPY = ["CONNECTION_FAILED", "AUTHENTICATION_REQUIRED", "CONFIGURATION_INVALID", "UNAVAILABLE"];

type Opened =
  | { kind: "plugin"; id: string }
  | { kind: "installed"; id: string; signInNow?: boolean }
  | { kind: "custom" };

export function PluginsSection() {
  const client = useRuntime();
  const tools = useTools(client);
  const store = usePlugins(client);
  const [tab, setTab] = useState<"plugins" | "built-in">("plugins");
  const [search, setSearch] = useState("");
  const [category, setCategory] = useState<string>("");
  const [opened, setOpened] = useState<Opened | null>(null);

  const categories = useMemo(() => {
    const counts = new Map<string, number>();
    for (const plugin of store.plugins) counts.set(plugin.category, (counts.get(plugin.category) ?? 0) + 1);
    return [...counts.entries()].sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0])).map(([name]) => name);
  }, [store.plugins]);

  const groups = useMemo(() => {
    const words = search.trim().toLowerCase();
    if (words) {
      const found = store.plugins.filter((plugin) =>
        `${plugin.name} ${plugin.description} ${plugin.category} ${plugin.publisher}`
          .toLowerCase()
          .includes(words),
      );
      return [{ title: `Results for “${search.trim()}”`, plugins: found }];
    }
    const shown = category ? [category] : [POPULAR, ...categories];
    return shown
      .map((title) => ({
        title,
        plugins:
          title === POPULAR
            ? store.plugins.filter((plugin) => plugin.popular)
            : store.plugins.filter((plugin) => plugin.category === title),
      }))
      .filter((group) => group.plugins.length > 0);
  }, [search, category, categories, store.plugins]);

  const pluginById = (id: string) => store.plugins.find((plugin) => plugin.id === id);
  const installedById = (id: string) => store.installed.find((item) => item.id === id);
  const openPlugin = (plugin: Plugin) =>
    setOpened(plugin.installed ? { kind: "installed", id: plugin.installed } : { kind: "plugin", id: plugin.id });

  return (
    <>
      <p className="lede">
        Connect the services your employees work with. Installing one is choosing
        it and pasting a token; reading runs on its own, and anything that
        changes, sends or deletes something asks you first.
      </p>

      <div className="plugins-bar">
        <div className="tabs" role="tablist">
          <button
            type="button"
            role="tab"
            aria-selected={tab === "plugins"}
            className={tab === "plugins" ? "tab on" : "tab"}
            onClick={() => setTab("plugins")}
          >
            Plugins <span className="count">{store.installed.length}</span>
          </button>
          <button
            type="button"
            role="tab"
            aria-selected={tab === "built-in"}
            className={tab === "built-in" ? "tab on" : "tab"}
            onClick={() => setTab("built-in")}
          >
            Built in <span className="count">{tools.tools.length}</span>
          </button>
        </div>
        {tab === "plugins" && store.available && (
          <button
            type="button"
            className="addbtn"
            onClick={() => setOpened({ kind: "custom" })}
            disabled={!store.ready}
          >
            <PlusIcon />
            Custom server
          </button>
        )}
      </div>

      {tab === "built-in" && (
        <>
          {tools.problem && (
            <p className="problem" role="alert">
              {tools.problem}
            </p>
          )}
          <section className="panel">
            <div className="card">
              {tools.tools.length === 0 && tools.ready && (
                <p className="card-empty">This machine offers nothing yet.</p>
              )}
              {tools.tools.map((tool: Tool) => (
                <ToolRow key={tool.name} tool={tool} />
              ))}
            </div>
          </section>
          <p className="note">
            A plugin marked <strong>asks first</strong> waits for you before it runs. The policy
            engine decides that, not this window - and it follows from what the plugin does to the
            world, so no declaration can lower it.
          </p>
        </>
      )}

      {tab === "plugins" && (
        <>
          {!store.available && store.ready && (
            <p className="note">
              Connecting services is switched off on this machine (PROMETHEUS_FLAGS__INTEGRATIONS=false).
            </p>
          )}
          {store.problem && (
            <p className="problem" role="alert">
              {store.problem}
            </p>
          )}

          {store.available && (
            <>
              <label className="search plugin-search">
                <SearchIcon />
                <input
                  type="search"
                  value={search}
                  placeholder="Search plugins"
                  aria-label="Search plugins"
                  onChange={(event) => setSearch(event.target.value)}
                  onKeyDown={(event) => {
                    if (event.key === "Escape") setSearch("");
                  }}
                />
              </label>

              <section className="panel">
                <div className="panel-head">
                  <h2>Installed</h2>
                </div>
                {store.installed.length === 0 && store.ready ? (
                  <p className="card-empty installed-empty">Nothing is connected yet.</p>
                ) : (
                  <ul className="installed-strip">
                    {store.installed.map((item) => (
                      <InstalledTile
                        key={item.id}
                        item={item}
                        plugin={pluginById(item.plugin)}
                        onOpen={() => setOpened({ kind: "installed", id: item.id })}
                      />
                    ))}
                  </ul>
                )}
              </section>

              {!search.trim() && categories.length > 1 && (
                <div className="tabs category-chips" role="tablist" aria-label="Categories">
                  {["", ...categories].map((name) => (
                    <button
                      key={name || "all"}
                      type="button"
                      role="tab"
                      aria-selected={category === name}
                      className={category === name ? "tab on" : "tab"}
                      onClick={() => setCategory(name)}
                    >
                      {name || "All"}
                    </button>
                  ))}
                </div>
              )}

              {groups.map((group) => (
                <section key={group.title} className="panel">
                  <div className="panel-head">
                    <h2>{group.title}</h2>
                  </div>
                  <ul className="plugin-grid">
                    {group.plugins.map((plugin) => (
                      <PluginRow
                        key={`${group.title}-${plugin.id}`}
                        plugin={plugin}
                        onOpen={() => openPlugin(plugin)}
                        action={
                          plugin.installed ? (
                            <span className="plugin-installed" title="Installed">
                              <CheckIcon />
                            </span>
                          ) : (
                            <button
                              type="button"
                              className="plugin-add"
                              aria-label={`Install ${plugin.name}`}
                              onClick={() => openPlugin(plugin)}
                            >
                              <PlusIcon />
                            </button>
                          )
                        }
                      />
                    ))}
                  </ul>
                </section>
              ))}
              {groups.length === 0 && store.ready && (
                <p className="card-empty">No plugin matches that.</p>
              )}
            </>
          )}
        </>
      )}

      {opened?.kind === "plugin" && pluginById(opened.id) && (
        <PluginModal plugin={pluginById(opened.id)!} onClose={() => setOpened(null)}>
          <InstallPluginForm
            plugin={pluginById(opened.id)!}
            runtime={store.runtimes[pluginById(opened.id)!.runtime]}
            employees={store.employees}
            onInstall={async (values, chosen) => {
              const plugin = pluginById(opened.id)!;
              const installed = await store.install(plugin, values, chosen);
              // A plugin that signs in through the browser goes straight on to
              // it: the person pressed one button and means the whole thing.
              setOpened(
                installed && plugin.sign_in
                  ? { kind: "installed", id: installed, signInNow: true }
                  : null,
              );
            }}
          />
        </PluginModal>
      )}

      {opened?.kind === "installed" && installedById(opened.id) && (
        <InstalledModal
          item={installedById(opened.id)!}
          plugin={pluginById(installedById(opened.id)!.plugin)}
          onClose={() => setOpened(null)}
        >
          <PluginDetails
            key={`${installedById(opened.id)!.id}-${installedById(opened.id)!.granted_to.join(",")}`}
            integration={installedById(opened.id)!}
            plugin={pluginById(installedById(opened.id)!.plugin)}
            employees={store.employees}
            onGrant={(chosen) => store.grant(opened.id, chosen)}
            onReplaceSecret={(name, value) => store.replaceSecret(opened.id, name, value)}
            onSignIn={() => store.signIn(opened.id)}
            signInNow={opened.signInNow}
            actions={
              <IntegrationActions
                integration={installedById(opened.id)!}
                onConnect={store.connect}
                onEnable={store.enable}
                onDisable={store.disable}
                onRemove={async (id) => {
                  await store.remove(id);
                  setOpened(null);
                }}
              />
            }
          />
        </InstalledModal>
      )}

      {opened?.kind === "custom" && (
        <Modal
          title="Add a custom MCP server"
          note="For a server that is not in the list: a name and the command that starts it."
          onClose={() => setOpened(null)}
        >
          <AddIntegrationForm
            onAdd={async (submission) => {
              await store.addCustom(submission);
              setOpened(null);
            }}
            disabled={!store.ready}
            known={CAPABILITIES}
          />
        </Modal>
      )}
    </>
  );
}

function statusTone(item: InstalledIntegration): string {
  if (!item.enabled) return "off";
  if (UNHAPPY.includes(item.status)) return "bad";
  return item.status === "READY" ? "good" : "waiting";
}

function InstalledTile({
  item,
  plugin,
  onOpen,
}: {
  item: InstalledIntegration;
  plugin?: Plugin;
  onOpen: () => void;
}) {
  return (
    <li>
      <button type="button" className="installed-tile" onClick={onOpen} aria-label={`Installed: ${plugin?.name ?? item.name}`}>
        <PluginMark name={plugin?.name ?? item.name} icon={plugin?.icon ?? null} size="tile" />
        <span className="installed-name">{plugin?.name ?? item.name}</span>
        <span className={`state ${statusTone(item)}`}>{item.status.replace(/_/g, " ").toLowerCase()}</span>
      </button>
    </li>
  );
}

function PluginModal({
  plugin,
  onClose,
  children,
}: {
  plugin: Plugin;
  onClose: () => void;
  children: ReactNode;
}) {
  return (
    <Modal title={plugin.name} note={plugin.description} onClose={onClose}>
      <div className="plugin-modal-mark">
        <PluginMark name={plugin.name} icon={plugin.icon} size="large" />
      </div>
      {children}
    </Modal>
  );
}

function InstalledModal({
  item,
  plugin,
  onClose,
  children,
}: {
  item: InstalledIntegration;
  plugin?: Plugin;
  onClose: () => void;
  children: ReactNode;
}) {
  return (
    <Modal
      title={plugin?.name ?? item.name}
      note={plugin?.description ?? "A server added by hand."}
      onClose={onClose}
    >
      <div className="plugin-modal-mark">
        <PluginMark name={plugin?.name ?? item.name} icon={plugin?.icon ?? null} size="large" />
        <span className={`state ${statusTone(item)}`}>
          {item.status.replace(/_/g, " ").toLowerCase()}
        </span>
      </div>
      {children}
    </Modal>
  );
}
