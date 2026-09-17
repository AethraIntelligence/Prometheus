import { useCallback, useEffect, useState } from "react";

import { approvalApi, type ApprovalGrant, type ApprovalInbox } from "../../../entities/approval";
import { decideApproval } from "../../../features/decide-approval";
import { report, type RuntimeClient } from "../../../shared/api";
import { describe } from "../../../shared/lib";

const EMPTY: ApprovalInbox = {
  pending: [],
  recent: [],
  counts: { total: 0, actionable: 0, critical: 0, long_wait: 0 },
};

export function useApprovalInbox(client: RuntimeClient) {
  const [inbox, setInbox] = useState<ApprovalInbox>(EMPTY);
  const [ready, setReady] = useState(false);
  const [busy, setBusy] = useState(false);
  const [problem, setProblem] = useState("");

  const reload = useCallback(async () => {
    try {
      setInbox(await approvalApi.inbox(client));
      setProblem("");
    } catch (error) {
      const message = describe(error);
      setProblem(message);
      void report(message);
    } finally {
      setReady(true);
    }
  }, [client]);

  useEffect(() => {
    void reload();
    const timer = window.setInterval(() => void reload(), 3000);
    return () => window.clearInterval(timer);
  }, [reload]);

  const decide = useCallback(
    async (
      id: string,
      approved: boolean,
      grant: ApprovalGrant = "ONCE",
      durationSeconds?: number,
    ) => {
      setBusy(true);
      setProblem("");
      try {
        await decideApproval(client, id, approved, "", grant, durationSeconds);
      } catch (error) {
        const message = describe(error);
        setProblem(message);
        void report(message);
      } finally {
        await reload();
        setBusy(false);
      }
    },
    [client, reload],
  );

  return { inbox, ready, busy, problem, decide };
}
