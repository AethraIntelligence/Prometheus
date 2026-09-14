/**
 * The settings screen's provider state: what is connected, and nothing derived.
 *
 * Every operation ends by re-reading the runtime's answer rather than patching
 * a local copy. Whether a connection can be removed, which model a kind of work
 * reaches, whether a key is stored - all of those are the core's answers, and a
 * local guess would be a second copy of a rule that disagrees the first time
 * the real one changes.
 */

import { useCallback, useEffect, useState } from "react";

import {
  providerApi,
  type InstalledModels,
  type ProviderSettings,
} from "../../../entities/provider";
import {
  addConnection,
  addModel,
  applySetup,
  clearWorkRouting,
  removeConnection,
  removeModel,
  replaceKey,
  sendWorkTo,
  type ConnectionSubmission,
  type ModelSubmission,
} from "../../../features/manage-providers";
import { report, type RuntimeClient } from "../../../shared/api";
import { describe } from "../../../shared/lib";

const EMPTY: ProviderSettings = {
  kinds: [],
  connections: [],
  models: [],
  defaults: {},
  guide: null,
};

export interface ProvidersState {
  ready: boolean;
  problem: string;
  settings: ProviderSettings;
  addProvider: (submission: ConnectionSubmission) => Promise<void>;
  rotateKey: (name: string, apiKey: string) => Promise<void>;
  dropProvider: (name: string) => Promise<void>;
  addEntry: (submission: ModelSubmission) => Promise<void>;
  dropEntry: (name: string) => Promise<void>;
  route: (taskKind: string, entryName: string) => Promise<void>;
  installed: (connection: string) => Promise<InstalledModels>;
  useSetup: (setupId: string) => Promise<void>;
}

export function useProviders(client: RuntimeClient): ProvidersState {
  const [ready, setReady] = useState(false);
  const [problem, setProblem] = useState("");
  const [settings, setSettings] = useState<ProviderSettings>(EMPTY);

  const fail = useCallback((error: unknown) => {
    const said = describe(error);
    setProblem(said);
    report(said);
  }, []);

  const reload = useCallback(async () => {
    try {
      // Merged onto the empty shape rather than taken as given: a runtime that
      // answered with less than the full body - an older build, a proxy that
      // trimmed it - would otherwise render as a crash instead of an empty
      // section, and a settings screen is exactly where that must not happen.
      const body = await providerApi.all(client);
      setSettings({
        kinds: body?.kinds ?? [],
        connections: body?.connections ?? [],
        models: body?.models ?? [],
        defaults: body?.defaults ?? {},
        guide: body?.guide ?? null,
      });
    } catch (error) {
      fail(error);
    } finally {
      setReady(true);
    }
  }, [client, fail]);

  useEffect(() => {
    void reload();
  }, [reload]);

  const run = useCallback(
    async (action: () => Promise<unknown>) => {
      // Cleared here rather than after the reload that follows. Reading the
      // list again always succeeds, so clearing it there erased the refusal
      // the person was meant to read - "three models use this connection"
      // appeared and vanished in the same frame.
      setProblem("");
      try {
        await action();
      } catch (error) {
        fail(error);
      } finally {
        await reload();
      }
    },
    [fail, reload],
  );

  return {
    ready,
    problem,
    settings,
    addProvider: (submission) =>
      run(() =>
        addConnection(client, {
          name: submission.name,
          kind: submission.kind,
          api_key: submission.apiKey,
          base_url: submission.baseUrl,
        }),
      ),
    rotateKey: (name, apiKey) => run(() => replaceKey(client, name, apiKey)),
    dropProvider: (name) => run(() => removeConnection(client, name)),
    addEntry: (submission) =>
      run(() => {
        const through = settings.connections.find((item) => item.name === submission.connection);
        return addModel(client, {
          name: submission.name,
          // The provider follows from the connection: asking a person to type
          // it as well is asking them to repeat something already decided.
          provider: through?.kind ?? "local",
          model: submission.model,
          connection: submission.connection,
          capabilities: submission.capabilities,
        });
      }),
    dropEntry: (name) => run(() => removeModel(client, name)),
    route: (taskKind, entryName) =>
      run(() =>
        entryName
          ? sendWorkTo(client, taskKind, entryName)
          : clearWorkRouting(client, taskKind),
      ),
    installed: (connection) => providerApi.installed(client, connection),
    useSetup: (setupId) => run(() => applySetup(client, setupId)),
  };
}
