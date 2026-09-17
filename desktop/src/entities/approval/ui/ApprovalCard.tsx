/**
 * A question the person has to answer, shown but not answered here.
 *
 * The card presents; deciding is a feature, and it arrives through `actions`.
 * The split is the architectural rule made structural: this layer cannot call
 * the runtime even if somebody later wanted it to, so the one place a decision
 * is sent stays the one place it is sent from.
 *
 * Whether this action needed asking was settled by the policy engine before the
 * window heard about it. Whether it now happens is settled by the person.
 * Nothing in the interface gets a vote - which is why the risk is printed in
 * the runtime's own word rather than turned into a colour this layer chose.
 *
 * Folded by default. A write carries the whole file in its arguments, and a
 * card as tall as the file pushed the buttons below the fold; the action is
 * cut to one line and the arguments wait behind "Show details".
 */

import { useState, type ReactNode } from "react";

import { WarningIcon } from "../../../shared/ui";
import type { Approval } from "../model/types";

interface Props {
  approval: Approval;
  actions?: ReactNode;
}

export function ApprovalCard({ approval, actions }: Props) {
  const details = Object.entries(approval.payload ?? {});
  const preview = Object.entries(approval.preview ?? {});
  const [open, setOpen] = useState(false);
  const pending = !approval.state || approval.state === "PENDING";
  return (
    <article
      className={approval.live && pending ? "gate" : "gate ended"}
      aria-label={`Approval: ${approval.action}`}
    >
      <div className="gate-top">
        <WarningIcon />
        <b>
          {pending ? "Needs approval" : approval.state} ·{" "}
          <span className="risk">{approval.risk}</span>
        </b>
      </div>
      <h4 className={open ? undefined : "folded"} title={open ? undefined : approval.action}>
        Prometheus wants to {approval.action}
      </h4>
      {approval.reason && <p>{approval.reason}</p>}
      {approval.status_explanation && <p className="note">{approval.status_explanation}</p>}
      {approval.requires_explicit_confirmation && (
        <p className="note">
          Security step-up: approve only this exact action. Auto approval and saved permissions
          do not apply because untrusted content preceded it.
        </p>
      )}
      {approval.scope && (
        <p className="note">
          Exact scope: <code>{approval.scope.subject_name || approval.scope.subject}</code> may{" "}
          <code>{approval.scope.action}</code> on <code>{approval.scope.resource}</code>
        </p>
      )}
      {open && (details.length > 0 || preview.length > 0) && (
        <dl className="gate-details">
          {details.map(([key, value]) => (
            <div key={key}>
              <dt>{key}</dt>
              <dd>{String(value)}</dd>
            </div>
          ))}
          {preview.map(([key, value]) => (
            <div key={`preview-${key}`}>
              <dt>preview · {key}</dt>
              <dd>{typeof value === "object" ? JSON.stringify(value) : String(value)}</dd>
            </div>
          ))}
        </dl>
      )}
      {pending && !approval.live && (
        <p className="stale">
          Nothing is waiting on this any more — the run that asked has ended. It cannot be
          approved or resumed.
        </p>
      )}
      {pending && (approval.approve_effect || approval.reject_effect) && (
        <dl className="gate-effects">
          {approval.approve_effect && <div><dt>If approved</dt><dd>{approval.approve_effect}</dd></div>}
          {approval.reject_effect && <div><dt>If rejected</dt><dd>{approval.reject_effect}</dd></div>}
        </dl>
      )}
      {(actions || details.length > 0 || preview.length > 0) && (
        <div className="gate-acts">
          {actions}
          {(details.length > 0 || preview.length > 0) && (
            <button
              type="button"
              className="gate-more"
              aria-expanded={open}
              onClick={() => setOpen((now) => !now)}
            >
              {open ? "Hide details" : "Show details"}
            </button>
          )}
        </div>
      )}
    </article>
  );
}
