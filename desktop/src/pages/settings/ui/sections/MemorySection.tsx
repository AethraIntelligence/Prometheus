/**
 * What the platform remembers here, and what a person does to it.
 *
 * Search, a note of their own, tracing a line to where it came from and where
 * it was used, correcting it (the old wording is kept as replaced), choosing how
 * long it is kept, and forgetting it. Reading and forgetting
 * are still two contracts in the core (ADR 0009, amended by ADR 0021): the
 * runtime says whether this machine offers the second, and what may be
 * forgotten is exactly what this list shows - an employee's private notes are
 * not on it and cannot be reached from it.
 */

import { useState } from "react";

import {
  MemoryLine,
  MemoryTraceView,
  type MemoryItem,
  type MemoryTrace,
} from "../../../../entities/memory";
import {
  AddMemoryForm,
  CorrectMemoryForm,
  ForgetMemory,
  KeepMemory,
} from "../../../../features/manage-memory";
import { useRuntime } from "../../../../shared/api";
import { Modal, PlusIcon, SearchIcon } from "../../../../shared/ui";
import { useMemory } from "../../model/useMemory";

export function MemorySection() {
  const client = useRuntime();
  const memory = useMemory(client);
  const [adding, setAdding] = useState(false);
  const [correcting, setCorrecting] = useState<MemoryItem | null>(null);
  const [tracing, setTracing] = useState<MemoryTrace | null>(null);
  const searching = memory.search.trim() !== "";

  return (
    <>
      <p className="lede">
        What the platform noted about its work here, and what you told it to
        keep. Every run starts from this. A line marked <em>you</em> is true of
        you in every workspace; <em>here</em> is true of this one. Each line says
        what it rests on: <em>stated</em> and <em>recorded</em> are facts,
        <em>reported</em> and <em>assumption</em> are claims a run is told to check.
      </p>

      {!memory.available && memory.ready && (
        <p className="note">Memory is switched off on this machine (PROMETHEUS_FLAGS__MEMORY=false).</p>
      )}
      {memory.problem && (
        <p className="problem" role="alert">
          {memory.problem}
        </p>
      )}

      {memory.available && (
        <>
          <section className="panel">
            <div className="panel-head">
              <h2>What is remembered here</h2>
              <button
                type="button"
                className="addbtn"
                onClick={() => setAdding(true)}
                disabled={!memory.ready}
              >
                <PlusIcon />
                New note
              </button>
            </div>
            <label className="search memory-search">
              <SearchIcon />
              <input
                type="search"
                value={memory.search}
                placeholder="Search memory"
                aria-label="Search memory"
                onChange={(event) => memory.setSearch(event.target.value)}
                onKeyDown={(event) => {
                  if (event.key === "Escape") memory.setSearch("");
                }}
              />
            </label>
            <label className="checkbox memory-history">
              <input
                type="checkbox"
                checked={memory.history}
                onChange={(event) => memory.setHistory(event.target.checked)}
              />
              Show lines replaced by a correction
            </label>
            <div className="card">
              {memory.items.length === 0 && memory.ready ? (
                <p className="card-empty">
                  {searching ? "Nothing remembered matches that." : "Nothing remembered yet."}
                </p>
              ) : (
                <ul className="memories">
                  {memory.items.map((item) => (
                    <MemoryLine
                      key={item.id}
                      item={item}
                      actions={
                        <span className="actions">
                          <button
                            type="button"
                            onClick={() =>
                              void memory.trace(item.id).then((found) => setTracing(found))
                            }
                          >
                            Trace
                          </button>
                          {item.status !== "SUPERSEDED" && (
                            <>
                              <button type="button" onClick={() => setCorrecting(item)}>
                                Correct
                              </button>
                              <KeepMemory
                                id={item.id}
                                expiresAt={item.expires_at}
                                onKeep={memory.keep}
                              />
                            </>
                          )}
                          {memory.canForget && (
                            <ForgetMemory id={item.id} onForget={memory.forget} />
                          )}
                        </span>
                      }
                    />
                  ))}
                </ul>
              )}
            </div>
          </section>

          {correcting && (
            <Modal
              title="Correct a memory"
              note="The new wording replaces this line for every future run. The old one stays visible as replaced, and is removed after 30 days."
              onClose={() => setCorrecting(null)}
            >
              <CorrectMemoryForm
                current={correcting.content}
                onCorrect={async (content) => {
                  await memory.correct(correcting.id, content);
                  setCorrecting(null);
                }}
              />
            </Modal>
          )}

          {tracing && (
            <Modal
              title="Where this memory comes from"
              note="Its source, what it replaced or disagrees with, and every run that was given it, with the reason it was chosen."
              onClose={() => setTracing(null)}
            >
              <MemoryTraceView trace={tracing} />
            </Modal>
          )}

          {adding && (
            <Modal
              title="Remember something"
              note="Kept until you forget it. A longer text belongs in Documents, where it is quoted with its source."
              onClose={() => setAdding(false)}
            >
              <AddMemoryForm
                onAdd={async (content, aboutThePerson) => {
                  await memory.add(content, aboutThePerson);
                  setAdding(false);
                }}
                disabled={!memory.ready}
              />
            </Modal>
          )}
        </>
      )}
    </>
  );
}
