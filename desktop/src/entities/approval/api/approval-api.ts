import type { RuntimeClient } from "../../../shared/api";
import type { Approval, CapabilityLease } from "../model/types";

export const approvalApi = {
  async pending(client: RuntimeClient): Promise<Approval[]> {
    const body = await client.get<{ approvals: Approval[] }>("/api/approvals");
    return body.approvals;
  },
  async leases(client: RuntimeClient): Promise<CapabilityLease[]> {
    const body = await client.get<{ leases: CapabilityLease[] }>("/api/capability-leases");
    return body.leases;
  },
  async revoke(client: RuntimeClient, leaseId: string): Promise<void> {
    await client.del(`/api/capability-leases/${leaseId}`);
  },
};
