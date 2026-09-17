/** Active exact permissions and the one operation a person can perform on them. */

import { useCallback, useEffect, useState } from "react";

import { approvalApi, type CapabilityLease } from "../../../entities/approval";
import { report, type RuntimeClient } from "../../../shared/api";
import { describe } from "../../../shared/lib";

export function usePermissions(client: RuntimeClient) {
  const [ready, setReady] = useState(false);
  const [problem, setProblem] = useState("");
  const [leases, setLeases] = useState<CapabilityLease[]>([]);

  const reload = useCallback(async () => {
    try {
      setLeases(await approvalApi.leases(client));
      setProblem("");
    } catch (error) {
      const said = describe(error);
      setProblem(said);
      report(said);
    } finally {
      setReady(true);
    }
  }, [client]);

  useEffect(() => {
    void reload();
  }, [reload]);

  const revoke = useCallback(
    async (id: string) => {
      try {
        await approvalApi.revoke(client, id);
        setProblem("");
      } catch (error) {
        const said = describe(error);
        setProblem(said);
        report(said);
      }
      await reload();
    },
    [client, reload],
  );

  return { ready, problem, leases, revoke };
}
