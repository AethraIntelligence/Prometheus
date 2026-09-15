import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import { ComposerMenu, FolderTray, folderLabel } from "./ComposerMenu";

afterEach(() => vi.unstubAllGlobals());

describe("ComposerMenu", () => {
  it("offers a folder to work in, and reports the one chosen", async () => {
    const onChoose = vi.fn();
    render(
      <ComposerMenu
        folder=""
        fileRoot="/Users/me/Documents/Prometheus"
        saved={["/Users/me/Clients"]}
        onChoose={onChoose}
      />,
    );

    await userEvent.click(screen.getByRole("button", { name: "Add to the request" }));
    await userEvent.click(screen.getByRole("menuitem", { name: /Work in a folder/ }));
    expect(screen.getByRole("menuitemradio", { name: /Its own folder/ })).toHaveAttribute(
      "aria-checked",
      "true",
    );
    await userEvent.click(screen.getByRole("menuitemradio", { name: /Clients/ }));

    expect(onChoose).toHaveBeenCalledWith("/Users/me/Clients");
    expect(screen.queryByRole("menu")).not.toBeInTheDocument();
  });

  it("shows a tray only for a folder somebody chose, and changes it from there", async () => {
    vi.stubGlobal("prompt", () => "/Users/me/Elsewhere");
    const onChoose = vi.fn();
    const root = "/Users/me/Documents/Prometheus";
    const { rerender } = render(
      <FolderTray folder="" fileRoot={root} saved={[]} onChoose={onChoose} />,
    );
    expect(screen.queryByRole("button", { name: /Folder:/ })).not.toBeInTheDocument();

    rerender(<FolderTray folder={`${root}/News`} fileRoot={root} saved={[]} onChoose={onChoose} />);
    expect(screen.queryByRole("button", { name: /Folder:/ })).not.toBeInTheDocument();

    rerender(
      <FolderTray folder="/Users/me/Clients" fileRoot={root} saved={[]} onChoose={onChoose} />,
    );
    await userEvent.click(screen.getByRole("button", { name: "Folder: Clients" }));
    await userEvent.click(screen.getByRole("menuitem", { name: /Choose a folder/ }));

    expect(onChoose).toHaveBeenCalledWith("/Users/me/Elsewhere");
  });

  it("calls a folder by its last part", () => {
    expect(folderLabel("/Users/me/Documents/Prometheus/")).toBe("Prometheus");
    expect(folderLabel("C:\\\\work\\\\clients")).toBe("clients");
  });
});
