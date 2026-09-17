import { useEffect, useMemo, useState } from "react";

import { ApprovalCard, type Approval } from "../../../entities/approval";
import { ApprovalDecision } from "../../../features/decide-approval";
import { useRuntime } from "../../../shared/api";
import { PageHead } from "../../../shared/ui";
import { useApprovalInbox } from "../model/useApprovalInbox";

type Grouping = "risk" | "wait" | "resource";

const RISK = ["CRITICAL", "HIGH", "MEDIUM", "LOW"];
const WAIT: Record<string, string> = {
  NEW: "New",
  WAITING: "Waiting",
  LONG_WAIT: "Long wait",
};

function age(seconds = 0) {
  if (seconds < 60) return "Just now";
  if (seconds < 3600) return `${Math.floor(seconds / 60)} min`;
  if (seconds < 86400) return `${Math.floor(seconds / 3600)} hr`;
  return `${Math.floor(seconds / 86400)} day(s)`;
}

function group(items: Approval[], by: Grouping) {
  const grouped = new Map<string, Approval[]>();
  for (const item of items) {
    const key = by === "risk" ? item.risk : by === "wait" ? WAIT[item.wait_group ?? "NEW"] : item.resource_group || "General action";
    grouped.set(key, [...(grouped.get(key) ?? []), item]);
  }
  const entries = [...grouped.entries()];
  if (by === "risk") entries.sort(([left], [right]) => RISK.indexOf(left) - RISK.indexOf(right));
  return entries;
}

function DecisionDetail({
  approval,
  disabled,
  onDecide,
  onOpenWork,
  onOpenThread,
}: {
  approval: Approval;
  disabled: boolean;
  onDecide: Parameters<typeof ApprovalDecision>[0]["onDecide"];
  onOpenWork?: (id: string) => void;
  onOpenThread?: (id: string) => void;
}) {
  return (
    <section className="approval-detail" aria-label="Approval details">
      <ApprovalCard
        approval={approval}
        actions={approval.actionable ? <ApprovalDecision approvalId={approval.id} exactOnly={approval.requires_explicit_confirmation} disabled={disabled} onDecide={onDecide} /> : undefined}
      />
      {disabled && <p className="work-muted">Saving your decision…</p>}
      <dl className="approval-context">
        <div><dt>Waiting</dt><dd>{age(approval.wait_seconds)}</dd></div>
        <div><dt>Resource</dt><dd>{approval.resource_group || "General action"}</dd></div>
        {approval.task_goal && <div><dt>Task</dt><dd>{approval.task_goal}</dd></div>}
        {approval.task_status && <div><dt>Task state</dt><dd>{approval.task_status.replaceAll("_", " ")}</dd></div>}
        {approval.resolved_at && <div><dt>Resolved</dt><dd>{new Date(approval.resolved_at).toLocaleString()}</dd></div>}
        {approval.comment && <div><dt>Outcome</dt><dd>{approval.comment}</dd></div>}
      </dl>
      <div className="approval-links">
        {approval.objective_id && onOpenWork && <button type="button" onClick={() => onOpenWork(approval.objective_id!)}>Open work and evidence</button>}
        {approval.conversation_id && onOpenThread && <button type="button" onClick={() => onOpenThread(approval.conversation_id!)}>Open conversation</button>}
      </div>
    </section>
  );
}

export function ApprovalInboxPage({
  railOpen = true,
  onOpenRail,
  onOpenWork,
  onOpenThread,
}: {
  railOpen?: boolean;
  onOpenRail?: () => void;
  onOpenWork?: (id: string) => void;
  onOpenThread?: (id: string) => void;
}) {
  const page = useApprovalInbox(useRuntime());
  const [tab, setTab] = useState<"pending" | "recent">("pending");
  const [grouping, setGrouping] = useState<Grouping>("risk");
  const [selected, setSelected] = useState<string | null>(null);
  const items = tab === "pending" ? page.inbox.pending : page.inbox.recent;
  const selectedItem = items.find((item) => item.id === selected) ?? items[0] ?? null;
  const groups = useMemo(() => group(items, grouping), [items, grouping]);

  useEffect(() => {
    if (selectedItem && !items.some((item) => item.id === selectedItem.id)) setSelected(null);
  }, [items, selectedItem]);

  return (
    <main className="main">
      <PageHead title="Approval Inbox" railOpen={railOpen} onOpenRail={onOpenRail} chip={`${page.inbox.counts.actionable} awaiting you`} />
      <div className="approval-inbox">
        <section className="approval-summary" aria-label="Approval summary">
          <div><b>{page.inbox.counts.actionable}</b><span>Actionable</span></div>
          <div><b>{page.inbox.counts.critical}</b><span>Critical</span></div>
          <div><b>{page.inbox.counts.long_wait}</b><span>Long wait</span></div>
        </section>
        <div className="approval-toolbar">
          <div className="tabs">
            <button type="button" className={tab === "pending" ? "tab on" : "tab"} onClick={() => { setTab("pending"); setSelected(null); }}>Pending · {page.inbox.pending.length}</button>
            <button type="button" className={tab === "recent" ? "tab on" : "tab"} onClick={() => { setTab("recent"); setSelected(null); }}>Recent decisions</button>
          </div>
          <label>Group by <select value={grouping} onChange={(event) => setGrouping(event.target.value as Grouping)}><option value="risk">Risk</option><option value="wait">Waiting time</option><option value="resource">Resource</option></select></label>
        </div>
        {page.problem && <p className="problem" role="alert">{page.problem}</p>}
        <div className="approval-layout">
          <aside className="approval-list" aria-label={`${tab} approvals`}>
            {!page.ready ? <p className="card-empty">Loading approvals…</p> : items.length === 0 ? <p className="card-empty">{tab === "pending" ? "No decisions are waiting." : "No recent decisions."}</p> : groups.map(([name, approvals]) => <section key={name}><h3>{name}</h3>{approvals.map((approval) => <button type="button" className={selectedItem?.id === approval.id ? "on" : ""} key={approval.id} onClick={() => setSelected(approval.id)}><span className={`approval-risk ${approval.risk.toLowerCase()}`}>{approval.risk}</span><b>{approval.action}</b><small>{age(approval.wait_seconds)} · {approval.resource_group}</small></button>)}</section>)}
          </aside>
          {selectedItem ? <DecisionDetail approval={selectedItem} disabled={page.busy} onDecide={page.decide} onOpenWork={onOpenWork} onOpenThread={onOpenThread} /> : <section className="approval-detail empty"><p>Nothing needs a decision in this view.</p></section>}
        </div>
      </div>
    </main>
  );
}
