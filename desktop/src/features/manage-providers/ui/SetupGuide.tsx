/**
 * Recommended setups: which provider, which models, and the steps to get there.
 *
 * Everything written on it - provider names, model ids, prices, links - arrives
 * from the runtime, so this window still names no vendor. What it adds is the
 * order: pick a setup, get a key, add the connection, press one button. Whether
 * a step is done is read off what the runtime says (`connection`, `applied`),
 * never inferred here.
 */

import { useState } from "react";

import type { ProviderGuide, RecommendedModel, Setup, SetupStep } from "../../../entities/provider";
import { openExternal } from "../../../shared/api";
import { CheckIcon, CopyIcon } from "../../../shared/ui";

interface Props {
  guide: ProviderGuide;
  onConnect: (setup: Setup) => void;
  onApply: (setup: Setup) => Promise<void>;
  disabled?: boolean;
}

/** A setup already applied is where a returning person is; otherwise the first. */
function opening(guide: ProviderGuide): string {
  return (guide.setups.find((setup) => setup.applied) ?? guide.setups[0])?.id ?? "";
}

export function SetupGuide({ guide, onConnect, onApply, disabled }: Props) {
  const [chosen, setChosen] = useState(() => opening(guide));
  const setup = guide.setups.find((item) => item.id === chosen) ?? guide.setups[0];
  if (!setup) return null;

  return (
    <div className="guide">
      <p className="note">{guide.intro}</p>
      <div className="tabs" role="tablist" aria-label="Recommended setups">
        {guide.setups.map((item) => (
          <button
            key={item.id}
            type="button"
            role="tab"
            aria-selected={item.id === setup.id}
            className={item.id === setup.id ? "tab on" : "tab"}
            onClick={() => setChosen(item.id)}
          >
            {item.title}
            {item.applied && <CheckIcon aria-label="in use" />}
          </button>
        ))}
      </div>

      <article className="setup" role="tabpanel" aria-label={setup.title}>
        <header className="setup-head">
          <span className="badge">{setup.badge}</span>
          <h3>{setup.title}</h3>
        </header>
        <p>{setup.summary}</p>
        <p className="note">
          <strong>Good for:</strong> {setup.good_for}
        </p>

        <ol className="setup-steps">
          {setup.steps.map((step, index) => (
            <Step
              key={index}
              step={step}
              setup={setup}
              disabled={disabled}
              onConnect={onConnect}
              onApply={onApply}
            />
          ))}
        </ol>

        <h4>What gets added</h4>
        <ul className="setup-models">
          {setup.models.map((model) => (
            <ModelLine key={model.name} model={model} />
          ))}
        </ul>

        {setup.cautions.length > 0 && (
          <div className="setup-cautions">
            <h4>Worth knowing</h4>
            <ul>
              {setup.cautions.map((caution) => (
                <li key={caution}>{caution}</li>
              ))}
            </ul>
          </div>
        )}
      </article>
      {guide.checked && (
        <p className="note">Checked against the providers on {guide.checked}.</p>
      )}
    </div>
  );
}

function Step({
  step,
  setup,
  disabled,
  onConnect,
  onApply,
}: {
  step: SetupStep;
  setup: Setup;
  disabled?: boolean;
  onConnect: (setup: Setup) => void;
  onApply: (setup: Setup) => Promise<void>;
}) {
  const [busy, setBusy] = useState(false);
  const done =
    (step.action === "connect" && setup.connection !== "") ||
    (step.action === "apply" && setup.applied);

  return (
    <li className={done ? "done" : undefined}>
      <span className="setup-step-text">
        {done && <CheckIcon aria-label="done" />}
        {step.text}
      </span>
      {step.command && <Command text={step.command} />}
      <span className="actions">
        {step.action === "link" && (
          <button type="button" onClick={() => void openExternal(step.url)}>
            Open {new URL(step.url).host}
          </button>
        )}
        {step.action === "connect" &&
          (setup.connection ? (
            <span className="note">Connected as “{setup.connection}”</span>
          ) : (
            <button type="button" disabled={disabled} onClick={() => onConnect(setup)}>
              Add connection
            </button>
          ))}
        {step.action === "apply" && (
          <button
            type="button"
            className={setup.applied ? undefined : "primary"}
            disabled={disabled || busy || !setup.connection}
            title={setup.connection ? undefined : "Add the connection first"}
            onClick={async () => {
              setBusy(true);
              try {
                await onApply(setup);
              } finally {
                setBusy(false);
              }
            }}
          >
            {busy ? "Adding…" : setup.applied ? "Apply again" : "Use this setup"}
          </button>
        )}
      </span>
    </li>
  );
}

function Command({ text }: { text: string }) {
  const [copied, setCopied] = useState(false);
  return (
    <span className="setup-command">
      <code>{text}</code>
      <button
        type="button"
        aria-label="Copy command"
        onClick={async () => {
          try {
            await navigator.clipboard.writeText(text);
            setCopied(true);
            setTimeout(() => setCopied(false), 1500);
          } catch {
            // A clipboard the webview refuses is not worth an error: the
            // command is on the screen to select by hand.
          }
        }}
      >
        {copied ? <CheckIcon /> : <CopyIcon />}
      </button>
    </span>
  );
}

function price(model: RecommendedModel): string {
  if (model.free) return "Free";
  return `$${model.input_cost_per_1m_usd} in / $${model.output_cost_per_1m_usd} out per million tokens`;
}

function ModelLine({ model }: { model: RecommendedModel }) {
  return (
    <li>
      <div className="setup-model-head">
        <strong>{model.role}</strong>
        <span className={model.free ? "badge" : "badge quiet"}>{price(model)}</span>
      </div>
      <code className="setup-model-id">{model.model}</code>
      <p className="note">{model.why}</p>
      {model.route.length > 0 && (
        <p className="note">Takes: {model.route.map((kind) => kind.toLowerCase()).join(", ")}</p>
      )}
    </li>
  );
}
