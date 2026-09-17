import { useState } from "react";

import type { WorkflowSuggestion } from "../../../entities/workflow";
import { Modal } from "../../../shared/ui";

/**
 * The separate confirmation a draft needs before it becomes a workflow.
 *
 * It restates what saving does and does not do, because a suggestion that
 * quietly became a schedule or a permission is the failure this step exists
 * against. The name is checked by the runtime; the dialog only collects it.
 */
export function SaveSuggestionDialog({
  suggestion,
  busy,
  onSave,
  onClose,
}: {
  suggestion: WorkflowSuggestion;
  busy: boolean;
  onSave: (name: string, description: string) => void;
  onClose: () => void;
}) {
  const [name, setName] = useState(suggestion.proposed_name);
  const [description, setDescription] = useState(suggestion.description);
  return (
    <Modal
      title="Save as a workflow"
      note="Saving writes a manual workflow. It is not scheduled, not run, and grants no new permissions: each step runs with its employee's own declaration."
      onClose={onClose}
    >
      <form
        className="add-integration"
        aria-label="Save as a workflow"
        onSubmit={(event) => {
          event.preventDefault();
          onSave(name.trim(), description.trim());
        }}
      >
        <label>
          Name
          <input value={name} onChange={(event) => setName(event.target.value)} required />
        </label>
        <label>
          Description
          <input value={description} onChange={(event) => setDescription(event.target.value)} />
        </label>
        <div className="modal-actions">
          <button type="button" className="mini bordered" onClick={onClose}>
            Cancel
          </button>
          <button type="submit" disabled={busy || !name.trim()}>
            Save workflow
          </button>
        </div>
      </form>
    </Modal>
  );
}
