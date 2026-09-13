import { render, screen, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import type { ModelEntry } from "../../../entities/provider";
import { WorkRouting } from "./WorkRouting";

const entry = (name: string, extra: Partial<ModelEntry>): ModelEntry => ({
  name,
  provider: "local",
  model: name,
  connection: "",
  capabilities: [],
  context_tokens: 8192,
  input_cost_per_1k_usd: 0,
  output_cost_per_1k_usd: 0,
  quality: 0.5,
  dimensions: 0,
  embeds: false,
  generates_text: true,
  used_for: [],
  ...extra,
});

describe("WorkRouting", () => {
  it("offers embedding models for embedding and writing models for the rest", () => {
    render(
      <WorkRouting
        kinds={["PLANNING", "EMBEDDING"]}
        defaults={{}}
        models={[
          entry("LFM2", {}),
          entry("bge-m3", { embeds: true, generates_text: false, dimensions: 1024 }),
        ]}
        onRoute={async () => {}}
      />,
    );

    const planning = screen.getByRole("combobox", { name: "Model for planning" });
    const embedding = screen.getByRole("combobox", { name: "Model for embedding" });
    expect(within(planning).queryByRole("option", { name: "bge-m3" })).toBeNull();
    expect(within(planning).getByRole("option", { name: "LFM2" })).toBeInTheDocument();
    expect(within(embedding).queryByRole("option", { name: "LFM2" })).toBeNull();
    expect(within(embedding).getByRole("option", { name: "bge-m3" })).toBeInTheDocument();
  });
});
