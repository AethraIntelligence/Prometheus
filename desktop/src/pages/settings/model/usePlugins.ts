/**
 * The Plugins screen's state: what can be installed, what is, and who works here.
 *
 * Every operation ends by reading the runtime's answer again rather than by
 * patching a local copy - a server that did not start, a grant the core
 * narrowed, a token that is now kept: each is something only the runtime knows.
 */

import { useCallback, useEffect, useState } from "react";

import { employeeApi, type Employee } from "../../../entities/employee";
import {
  integrationApi,
  type InstalledIntegration,
  type Plugin,
  type RuntimeState,
} from "../../../entities/integration";
import {
  addIntegration,
  storeCredential,
  type Submission,
} from "../../../features/add-integration";
import {
  grantIntegration,
  installPlugin,
  replaceSecret,
  signInIntegration,
} from "../../../features/install-plugin";
import {
  connectIntegration,
  disableIntegration,
  enableIntegration,
  removeIntegration,
} from "../../../features/manage-integration";
import { report, type RuntimeClient } from "../../../shared/api";
import { describe } from "../../../shared/lib";

export interface PluginsState {
  ready: boolean;
  available: boolean;
  problem: string;
  plugins: Plugin[];
  installed: InstalledIntegration[];
  runtimes: Record<string, RuntimeState>;
  employees: Employee[];
  /** Resolves to the installed integration's id, or empty where installing failed. */
  install: (plugin: Plugin, values: Record<string, string>, employees: string[]) => Promise<string>;
  grant: (id: string, employees: string[]) => Promise<void>;
  replaceSecret: (id: string, name: string, value: string) => Promise<void>;
  addCustom: (submission: Submission) => Promise<void>;
  signIn: (id: string) => Promise<boolean>;
  connect: (id: string) => Promise<void>;
  enable: (id: string) => Promise<void>;
  disable: (id: string) => Promise<void>;
  remove: (id: string) => Promise<void>;
}

export function usePlugins(client: RuntimeClient): PluginsState {
  const [ready, setReady] = useState(false);
  const [available, setAvailable] = useState(true);
  const [problem, setProblem] = useState("");
  const [plugins, setPlugins] = useState<Plugin[]>([]);
  const [installed, setInstalled] = useState<InstalledIntegration[]>([]);
  const [runtimes, setRuntimes] = useState<Record<string, RuntimeState>>({});
  const [employees, setEmployees] = useState<Employee[]>([]);

  const fail = useCallback((error: unknown) => {
    const said = describe(error);
    setProblem(said);
    report(said);
  }, []);

  const reload = useCallback(async () => {
    try {
      const [body, workforce] = await Promise.all([
        integrationApi.plugins(client),
        employeeApi.all(client).catch(() => [] as Employee[]),
      ]);
      setAvailable(body.available ?? false);
      setPlugins(body.plugins ?? []);
      setInstalled(body.installed ?? []);
      setRuntimes(body.runtimes ?? {});
      setEmployees(workforce ?? []);
      setProblem("");
    } catch (error) {
      fail(error);
    } finally {
      setReady(true);
    }
  }, [client, fail]);

  useEffect(() => {
    void reload();
  }, [reload]);

  const perform = useCallback(
    async (action: () => Promise<unknown>) => {
      try {
        await action();
        setProblem("");
      } catch (error) {
        fail(error);
      }
      await reload();
    },
    [fail, reload],
  );

  return {
    ready,
    available,
    problem,
    plugins,
    installed,
    runtimes,
    employees,
    install: async (plugin, values, chosen) => {
      let id = "";
      await perform(async () => {
        id = (await installPlugin(client, plugin.id, values, chosen)).id;
      });
      return id;
    },
    grant: (id, chosen) => perform(() => grantIntegration(client, id, chosen)),
    replaceSecret: (id, name, value) =>
      perform(async () => {
        await replaceSecret(client, name, value);
        await connectIntegration(client, id);
      }),
    addCustom: (submission) =>
      perform(async () => {
        if (submission.secretName && submission.secretValue) {
          await storeCredential(client, submission.secretName, submission.secretValue);
        }
        const created = await addIntegration(client, {
          name: submission.name,
          configuration: { command: submission.command, args: submission.args },
          capabilities: submission.capabilities,
          secret_names: submission.secretName ? [submission.secretName] : [],
        });
        await connectIntegration(client, created.id);
      }),
    signIn: async (id) => {
      let signedIn = false;
      await perform(async () => {
        signedIn = await signInIntegration(client, id);
      });
      return signedIn;
    },
    connect: (id) => perform(() => connectIntegration(client, id)),
    enable: (id) => perform(() => enableIntegration(client, id)),
    disable: (id) => perform(() => disableIntegration(client, id)),
    remove: (id) => perform(() => removeIntegration(client, id)),
  };
}
