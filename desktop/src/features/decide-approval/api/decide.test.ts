import { afterEach, describe, expect, it, vi } from "vitest";

import { RuntimeClient } from "../../../shared/api";
import { decideApproval } from "./decide";

afterEach(() => vi.unstubAllGlobals());

describe("decideApproval", () => {
  it("carries the answer and adds nothing of its own", async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValue({ ok: true, status: 200, json: async () => ({}) });
    vi.stubGlobal("fetch", fetchMock);

    await decideApproval(new RuntimeClient("http://127.0.0.1:9999"), "a-1", false);

    const [url, init] = fetchMock.mock.calls[0];
    expect(url).toBe("http://127.0.0.1:9999/api/approvals/a-1");
    expect(JSON.parse(init.body)).toEqual({ approved: false, comment: "", grant: "ONCE" });
  });
});
