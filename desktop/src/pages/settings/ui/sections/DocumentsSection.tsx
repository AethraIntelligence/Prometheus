/**
 * What this workspace knows because somebody put it here.
 *
 * A document is not a memory (ADR 0016): it does not decay, and it is quoted
 * with its source. The screen says so rather than leaving the two looking like
 * one list with different rows.
 */

import { useState } from "react";

import { DocumentCard, type Document } from "../../../../entities/document";
import { AddDocumentForm, DocumentActions } from "../../../../features/manage-documents";
import { chooseFiles, useRuntime } from "../../../../shared/api";
import { Modal, PlusIcon } from "../../../../shared/ui";
import { useDocuments } from "../../model/useDocuments";

export function DocumentsSection() {
  const client = useRuntime();
  const documents = useDocuments(client);
  const [adding, setAdding] = useState(false);

  // The system's dialog where there is one; a path field only in a browser,
  // where no page can learn where a chosen file lives on disk.
  //
  // A dialog that refuses to open is said on the screen like a runtime's
  // refusal: the first version of this let the rejection go unhandled, and a
  // person who clicked "New document" saw nothing happen at all.
  const addDocuments = async () => {
    try {
      const chosen = await chooseFiles({ multiple: true, title: "Add documents" });
      if (chosen === null) setAdding(true);
      else if (chosen.length > 0) await documents.addAll(chosen);
    } catch (error) {
      documents.fail(error);
    }
  };

  const updateDocument = async (id: string) => {
    try {
      const chosen = await chooseFiles({ title: "Choose the new version" });
      const path = chosen === null ? window.prompt("Path to the new version")?.trim() : chosen[0];
      if (path) await documents.replace(id, path);
    } catch (error) {
      documents.fail(error);
    }
  };

  return (
    <>
      <p className="lede">
        What this workspace knows because you put it here. Employees quote these
        with their source; they are not the platform's own notes.
      </p>

      {!documents.available && documents.ready && (
        <p className="note">
          Documents are switched off on this machine (PROMETHEUS_FLAGS__KNOWLEDGE=false).
        </p>
      )}
      {documents.problem && (
        <p className="problem" role="alert">
          {documents.problem}
        </p>
      )}

      {documents.available && (
        <>
          <section className="panel">
            <div className="panel-head">
              <h2>Documents</h2>
              <button
                type="button"
                className="addbtn"
                onClick={() => void addDocuments()}
                disabled={!documents.ready}
              >
                <PlusIcon />
                New document
              </button>
            </div>
            <div className="card">
              {documents.documents.length === 0 && documents.ready && (
                <p className="card-empty">Nothing added yet.</p>
              )}
              {documents.documents.map((document: Document) => (
                <DocumentCard
                  key={document.id}
                  document={document}
                  actions={
                    <DocumentActions
                      document={document}
                      onUpdate={updateDocument}
                      onReindex={documents.reindex}
                      onRemove={documents.remove}
                    />
                  }
                />
              ))}
            </div>
          </section>

          {adding && (
            <Modal
              title="Add document"
              note="The runtime reads the file itself, so what this needs is where it is."
              onClose={() => setAdding(false)}
            >
              <AddDocumentForm
                onAdd={async (path) => {
                  await documents.add(path);
                  setAdding(false);
                }}
                disabled={!documents.ready}
              />
            </Modal>
          )}
        </>
      )}
    </>
  );
}
