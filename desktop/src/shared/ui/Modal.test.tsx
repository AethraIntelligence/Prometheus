/**
 * Where a dialog is drawn, which is the whole of what it got wrong.
 *
 * A `fixed` scrim is measured against the viewport only while no ancestor has
 * a filter, a transform or a backdrop. The page header has a backdrop blur, so
 * the brief opened from it was laid out inside a 52-pixel bar and disappeared
 * under the conversation. Nothing about that is visible to jsdom - so what is
 * asserted is the cause: the dialog is a child of the body, whatever it was
 * opened from.
 */

import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { Modal } from "./Modal";

describe("Modal", () => {
  it("is drawn into the body, not into the corner it was opened from", () => {
    const { container } = render(
      <div style={{ backdropFilter: "blur(18px)" }}>
        <Modal title="Brief" onClose={() => {}}>
          <p>what the thread established</p>
        </Modal>
      </div>,
    );

    const dialog = screen.getByRole("dialog", { name: "Brief" });
    expect(container.contains(dialog)).toBe(false);
    expect(document.body.contains(dialog)).toBe(true);
    expect(dialog.closest(".modal-scrim")?.parentElement).toBe(document.body);
  });
});
