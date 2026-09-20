/**
 * Providers, the catalog, and which model each kind of work reaches.
 *
 * Three panels and two dialogs. What used to stand here was both forms open at
 * once - eight fields and a seven-box fieldset above the list they add to - so
 * the screen read as a form with some records underneath rather than as the
 * record of what is configured.
 */

import { useEffect, useState } from "react";

import {
  ConnectionCard,
  ModelRow,
  type Connection,
  type ModelEntry,
  type Setup,
} from "../../../../entities/provider";
import {
  AddConnectionForm,
  AddModelForm,
  ProviderAdviceList,
  RequirementsList,
  SetupGuide,
  WorkRouting,
} from "../../../../features/manage-providers";
import { useRuntime } from "../../../../shared/api";
import { Modal, PlusIcon } from "../../../../shared/ui";
import { useProviders } from "../../model/useProviders";

/** What a model may be picked for. The router refuses anything outside its list. */
const MODEL_CAPABILITIES = [
  "TEXT_REASONING",
  "TOOL_CALLING",
  "STRUCTURED_OUTPUT",
  "LONG_CONTEXT",
  "CODE",
  "VISION",
  "EMBEDDING",
  "DECISION",
];

/**
 * The kinds of work a model can be given, in the order they happen in a run.
 * Named here only to lay the rows out; what each one means, and which model it
 * would otherwise reach, is the router's answer and arrives from the runtime.
 */
const TASK_KINDS = [
  "PLANNING",
  "EXECUTION",
  "VERIFICATION",
  "SYNTHESIS",
  "EXTRACTION",
  "DECISION",
  "CONVERSATION",
  "EMBEDDING",
];

export function ModelsSection() {
  const client = useRuntime();
  const providers = useProviders(client);
  const [adding, setAdding] = useState<"connection" | "model" | null>(null);
  // Which recommendation opened the connection dialog, so it arrives filled in.
  const [forSetup, setForSetup] = useState<Setup | null>(null);

  const connections = providers.settings.connections;
  const guide = providers.settings.guide;
  // A machine with nothing connected yet is the person the advice is for, so it
  // opens; afterwards it stays one click away instead of pushing the record down.
  // Decided once, when the runtime first answers: following the count would
  // fold the advice away the moment its second step succeeded, one step short
  // of the button it was leading to.
  const [guideOpen, setGuideOpen] = useState<boolean | null>(null);
  useEffect(() => {
    if (providers.ready && guideOpen === null) setGuideOpen(connections.length === 0);
  }, [providers.ready, guideOpen, connections.length]);

  return (
    <>
      <p className="lede">
        A provider is a kind; a connection is an account. Two keys to one vendor
        are two connections, and a model says which one it is reached through. A
        key is stored encrypted and never shown again - only replaced.
      </p>

      {providers.problem && (
        <p className="problem" role="alert">
          {providers.problem}
        </p>
      )}

      {guide && guide.setups.length > 0 && (
        <section className="panel">
          <details
            className="guide-fold"
            open={guideOpen ?? false}
            onToggle={(event) => setGuideOpen(event.currentTarget.open)}
          >
            <summary>
              <h2>Which models to use</h2>
              <span className="note">Recommended setups, free options and what each needs</span>
            </summary>
            <SetupGuide
              guide={guide}
              disabled={!providers.ready}
              onConnect={(setup) => {
                setForSetup(setup);
                setAdding("connection");
              }}
              onApply={(setup) => providers.useSetup(setup.id)}
            />
            <details className="guide-more">
              <summary>Which provider should I choose?</summary>
              <ProviderAdviceList guide={guide} />
            </details>
            <details className="guide-more">
              <summary>What a model has to be able to do</summary>
              <RequirementsList guide={guide} />
            </details>
          </details>
        </section>
      )}

      <section className="panel">
        <div className="panel-head">
          <h2>Connections</h2>
          <button type="button" className="addbtn" onClick={() => setAdding("connection")}>
            <PlusIcon />
            New provider
          </button>
        </div>
        <div className="card">
          {connections.length === 0 && providers.ready && (
            <p className="card-empty">
              Nothing added. The machine is using whatever key it was configured
              with and the models it shipped with.
            </p>
          )}
          {connections.map((connection: Connection) => (
            <ConnectionCard
              key={connection.id}
              connection={connection}
              actions={
                <div className="actions">
                  <button
                    type="button"
                    onClick={() => void providers.dropProvider(connection.name)}
                  >
                    Remove
                  </button>
                </div>
              }
            />
          ))}
        </div>
      </section>

      <section className="panel">
        <div className="panel-head">
          <h2>Models</h2>
          <button
            type="button"
            className="addbtn"
            onClick={() => setAdding("model")}
            // A model is reached through a connection, so there is nothing to
            // add one to until one exists. Disabled rather than hidden: the
            // button is where a person looks for the reason.
            disabled={connections.length === 0}
            title={connections.length === 0 ? "Add a provider first" : undefined}
          >
            <PlusIcon />
            New model
          </button>
        </div>
        <div className="card">
          {providers.settings.models.length === 0 && providers.ready && (
            <p className="card-empty">Nothing in the catalog yet.</p>
          )}
          {providers.settings.models.map((entry: ModelEntry) => (
            <ModelRow
              key={entry.name}
              entry={entry}
              actions={
                <div className="actions">
                  <button type="button" onClick={() => void providers.dropEntry(entry.name)}>
                    Remove
                  </button>
                </div>
              }
            />
          ))}
        </div>
      </section>

      <section className="panel">
        <div className="panel-head">
          <h2>Where work goes</h2>
        </div>
        <p className="note">
          Each kind of work can be given to one model. Left to the router, it
          picks from what the work needs and what each model can do.
        </p>
        <WorkRouting
          kinds={TASK_KINDS}
          defaults={providers.settings.defaults}
          models={providers.settings.models}
          onRoute={providers.route}
        />
      </section>

      {adding === "connection" && (
        <Modal
          title="Add provider"
          note="The kinds come from the runtime. A key is sent and never read back."
          onClose={() => {
            setAdding(null);
            setForSetup(null);
          }}
        >
          <AddConnectionForm
            kinds={providers.settings.kinds}
            initialKind={forSetup?.kind}
            initialName={forSetup?.connection_name}
            onAdd={async (submission) => {
              await providers.addProvider(submission);
              setAdding(null);
              setForSetup(null);
            }}
          />
        </Modal>
      )}

      {adding === "model" && (
        <Modal
          title="Add model"
          note="A model on this machine is picked from what its runner has; a hosted one is named as its provider names it."
          onClose={() => setAdding(null)}
        >
          <AddModelForm
            connections={connections}
            installed={providers.installed}
            onAdd={async (submission) => {
              await providers.addEntry(submission);
              setAdding(null);
            }}
            known={MODEL_CAPABILITIES}
          />
        </Modal>
      )}
    </>
  );
}
