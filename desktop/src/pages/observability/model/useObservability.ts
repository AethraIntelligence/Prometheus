import { useCallback, useEffect, useState } from "react";

import { traceApi, type DiagnosticHealth, type Trace } from "../../../entities/trace";
import { report, type RuntimeClient } from "../../../shared/api";
import { describe } from "../../../shared/lib";

export function useObservability(client: RuntimeClient, initialTraceId: string | null) {
  const [traces, setTraces] = useState<Trace[]>([]);
  const [selected, setSelected] = useState<string | null>(initialTraceId);
  const [trace, setTrace] = useState<Trace | null>(null);
  const [health, setHealth] = useState<DiagnosticHealth | null>(null);
  const [metrics, setMetrics] = useState<Record<string, unknown>>({});
  const [bundle, setBundle] = useState<Record<string, unknown> | null>(null);
  const [problem, setProblem] = useState("");
  const [ready, setReady] = useState(false);

  const load = useCallback(async () => {
    try {
      const [recent, currentHealth, currentMetrics] = await Promise.all([
        traceApi.all(client),
        traceApi.health(client),
        traceApi.metrics(client),
      ]);
      setTraces(recent);
      setHealth(currentHealth);
      setMetrics(currentMetrics);
      setSelected((value) => value ?? recent[0]?.trace_id ?? null);
      setProblem("");
    } catch (error) {
      const message = describe(error);
      setProblem(message);
      void report(message);
    } finally {
      setReady(true);
    }
  }, [client]);

  useEffect(() => void load(), [load]);
  useEffect(() => {
    if (!selected) {
      setTrace(null);
      return;
    }
    void traceApi.one(client, selected).then(setTrace).catch((error) => setProblem(describe(error)));
  }, [client, selected]);

  const preview = async () => {
    try {
      setBundle(await traceApi.bundle(client, selected ?? undefined));
    } catch (error) {
      setProblem(describe(error));
    }
  };

  const exportBundle = async (path: string) => {
    await traceApi.export(client, path, selected ? [selected] : []);
  };

  return { traces, selected, setSelected, trace, health, metrics, bundle, preview, exportBundle, problem, ready };
}
