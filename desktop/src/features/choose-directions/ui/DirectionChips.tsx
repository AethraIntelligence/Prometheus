/**
 * How a request should be carried out, chosen under the field it is typed in.
 *
 * Two chips beside the workspace one, drawn the same way: the chip is what is
 * seen and the native list sits invisibly over it, so the operating system's
 * own menu is the accessible one. Neither decides anything. Whether "Auto"
 * actually skips a question is the runtime's answer - a machine configured to
 * refuse stays refusing - and a chosen model is a preference the router weighs
 * against what each piece of work needs, not an order this window gives.
 */

import type { ApprovalChoice, Directions } from "../../../entities/conversation";
import type { ModelEntry } from "../../../entities/provider";
import { ChevronDown } from "../../../shared/ui";

const APPROVALS: { value: ApprovalChoice; label: string; hint: string }[] = [
  {
    value: "ASK",
    label: "Ask me",
    hint: "Wait for me before anything that needs approval",
  },
  { value: "AUTO", label: "Auto", hint: "Go ahead without asking" },
  {
    value: "DENY",
    label: "Never",
    hint: "Refuse anything that needs approval",
  },
];

interface Props {
  directions: Directions;
  models: ModelEntry[];
  onChange: (next: Directions) => void;
  disabled?: boolean;
}

export function DirectionChips({ directions, models, onChange, disabled }: Props) {
  const approval = APPROVALS.find((item) => item.value === directions.approvals) ?? APPROVALS[0];
  const model = models.find((item) => item.name === directions.model);

  return (
    <>
      <label className="dockchip switcher" title={approval.hint}>
        Approvals <b>{approval.label}</b>
        <ChevronDown />
        <select
          aria-label="Approvals"
          value={approval.value}
          disabled={disabled}
          onChange={(event) =>
            onChange({
              ...directions,
              approvals: event.target.value as ApprovalChoice,
            })
          }
        >
          {APPROVALS.map((item) => (
            <option key={item.value} value={item.value}>
              {item.label} - {item.hint}
            </option>
          ))}
        </select>
      </label>
      {models.length > 0 && (
        <label className="dockchip switcher" title="Which model the work should prefer">
          Model <b>{model?.name ?? "Auto"}</b>
          <ChevronDown />
          <select
            aria-label="Model"
            value={model?.name ?? ""}
            disabled={disabled}
            onChange={(event) => onChange({ ...directions, model: event.target.value })}
          >
            <option value="">Auto - let Prometheus choose</option>
            {models.map((item) => (
              <option key={item.name} value={item.name}>
                {item.name}
              </option>
            ))}
          </select>
        </label>
      )}
    </>
  );
}
