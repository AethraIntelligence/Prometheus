import type { RuntimeClient } from "../../../shared/api";
import type { DiagnosticHealth, Trace } from "../model/types";

export const traceApi = {
  all: (client: RuntimeClient) => client.get<Trace[]>("/api/traces"),
  one: (client: RuntimeClient, id: string) => client.get<Trace>(`/api/traces/${id}`),
  health: (client: RuntimeClient) => client.get<DiagnosticHealth>("/api/diagnostics/health"),
  metrics: (client: RuntimeClient) => client.get<Record<string, unknown>>("/api/diagnostics/metrics"),
  bundle: (client: RuntimeClient, id?: string) =>
    client.get<Record<string, unknown>>(`/api/diagnostics/bundle${id ? `?trace_id=${id}` : ""}`),
  export: (client: RuntimeClient, path: string, traceIds: string[]) =>
    client.post<{ path: string }>("/api/diagnostics/export", { path, trace_ids: traceIds }),
};
