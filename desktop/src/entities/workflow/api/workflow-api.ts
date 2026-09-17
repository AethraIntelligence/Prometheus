import type { RuntimeClient } from "../../../shared/api";
import type {
  WorkflowDryRun,
  WorkflowList,
  WorkflowRun,
  WorkflowSuggestionList,
} from "../model/types";

export const workflowApi = {
  suggestions(client: RuntimeClient): Promise<WorkflowSuggestionList> {
    return client.get<WorkflowSuggestionList>("/api/workflow-suggestions");
  },
  all(client: RuntimeClient): Promise<WorkflowList> {
    return client.get<WorkflowList>("/api/workflows");
  },
  dryRun(
    client: RuntimeClient,
    name: string,
    version: number,
    inputs: Record<string, unknown>,
  ): Promise<WorkflowDryRun> {
    return client.post<WorkflowDryRun>(`/api/workflows/${encodeURIComponent(name)}/dry-run`, {
      version,
      inputs,
    });
  },
  run(
    client: RuntimeClient,
    name: string,
    version: number,
    inputs: Record<string, unknown>,
  ): Promise<WorkflowRun> {
    return client.post<WorkflowRun>(`/api/workflows/${encodeURIComponent(name)}/run`, {
      version,
      inputs,
    });
  },
};
