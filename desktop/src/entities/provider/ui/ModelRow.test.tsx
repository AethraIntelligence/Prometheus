import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import type { ModelEntry } from "../model/types";
import { ModelRow } from "./ModelRow";

function entry(overrides: Partial<ModelEntry> = {}): ModelEntry {
  return {
    name: "fast",
    provider: "local",
    model: "small",
    connection: "",
    capabilities: ["TEXT_REASONING"],
    context_tokens: 8192,
    input_cost_per_1k_usd: 0,
    output_cost_per_1k_usd: 0,
    quality: 0.4,
    dimensions: 0,
    embeds: false,
    generates_text: true,
    decides: false,
    used_for: ["EXECUTION"],
    ...overrides,
  };
}

describe("a catalog entry", () => {
  it("shows the contract routing relies on", () => {
    render(<ModelRow entry={entry({ tier: "FAST", privacy: "LOCAL", latency_ms: 800 })} />);

    expect(screen.getByLabelText("Contract")).toHaveTextContent(
      "fast · stays on this machine · ~800 ms · $0/1k in, $0/1k out",
    );
  });

  it("says when prompts leave the machine, and shows nothing it was not told", () => {
    const { rerender } = render(<ModelRow entry={entry({ tier: "STRONG", privacy: "REMOTE" })} />);
    expect(screen.getByLabelText("Contract")).toHaveTextContent("prompts leave this machine");

    rerender(<ModelRow entry={entry()} />);
    expect(screen.queryByLabelText("Contract")).not.toBeInTheDocument();
  });
});
