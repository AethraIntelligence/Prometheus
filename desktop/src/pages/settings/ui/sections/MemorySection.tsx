/**
 * What the platform remembers here, and the three things a person does to it.
 *
 * Search, a note of their own, and forgetting one line. Reading and forgetting
 * are still two contracts in the core (ADR 0009, amended by ADR 0021): the
 * runtime says whether this machine offers the second, and what may be
 * forgotten is exactly what this list shows - an employee's private notes are
 * not on it and cannot be reached from it.
 */

import { useState } from "react";

import { MemoryLine } from "../../../../entities/memory";
import { AddMemoryForm, ForgetMemory } from "../../../../features/manage-memory";
import { useRuntime } from "../../../../shared/api";
import { Modal, PlusIcon, SearchIcon } from "../../../../shared/ui";
import { useMemory } from "../../model/useMemory";

export function MemorySection() {
  const client = useRuntime();
  const memory = useMemory(client);
  const [adding, setAdding] = useState(false);
  const searching = memory.search.trim() !== "";

  return (
    <>
      <p className="lede">
        What the platform noted about its work here, and what you told it to
        keep. Every run starts from this. A line marked <em>you</em> is true of
        you in every workspace; <em>here</em> is true of this one.
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
                        memory.canForget ? (
                          <ForgetMemory id={item.id} onForget={memory.forget} />
                        ) : undefined
                      }
                    />
                  ))}
                </ul>
              )}
            </div>
          </section>

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
