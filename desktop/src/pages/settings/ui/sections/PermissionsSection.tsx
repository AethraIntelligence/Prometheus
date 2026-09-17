/** Exact authority remembered from approvals, visible and revocable. */

import { useRuntime } from "../../../../shared/api";
import { usePermissions } from "../../model/usePermissions";

export function PermissionsSection() {
  const permissions = usePermissions(useRuntime());

  return (
    <>
      <p className="lede">
        Prometheus remembers only the exact employee, action and resource you approved.
        A task permission cannot be used by another task; every permission can be revoked here.
      </p>
      {permissions.problem && (
        <p className="problem" role="alert">
          {permissions.problem}
        </p>
      )}
      <section className="panel" aria-label="Active permissions">
        <div className="panel-head">
          <h2>Active permissions</h2>
        </div>
        <div className="card">
          {permissions.ready && permissions.leases.length === 0 ? (
            <p className="card-empty">No remembered permissions.</p>
          ) : (
            permissions.leases.map((lease) => (
              <div className="setting-row" key={lease.id}>
                <div className="setting-text">
                  <p className="setting-title">
                    {lease.action} · {lease.resource}
                  </p>
                  <p className="note">
                    {lease.grant === "TASK" ? "Only this task" : "Persistent exact rule"}
                    {lease.expires_at
                      ? ` · expires ${new Date(lease.expires_at).toLocaleString()}`
                      : " · no expiry"}
                  </p>
                  <p className="note">Subject: {lease.subject_name || lease.subject}</p>
                  {Object.keys(lease.limits).length > 0 && (
                    <p className="note">Limits: {JSON.stringify(lease.limits)}</p>
                  )}
                  {lease.reason && <p className="note">Reason: {lease.reason}</p>}
                </div>
                <button
                  type="button"
                  className="btn btn-line"
                  onClick={() => void permissions.revoke(lease.id)}
                  disabled={!permissions.ready}
                >
                  Revoke
                </button>
              </div>
            ))
          )}
        </div>
      </section>
    </>
  );
}
