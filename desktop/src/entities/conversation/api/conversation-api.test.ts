import { afterEach, describe, expect, it, vi } from "vitest";

import { RuntimeClient } from "../../../shared/api";
import { conversationApi } from "./conversation-api";

const BASE = "http://127.0.0.1:9999";

function answering(body: unknown) {
  const fetchMock = vi.fn().mockResolvedValue({ ok: true, status: 200, json: async () => body });
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}

afterEach(() => vi.unstubAllGlobals());

describe("conversationApi", () => {
  it("says a request came from the desktop, and sends the text unchanged", async () => {
    const fetchMock = answering({ id: "m1" });

    await conversationApi.send(new RuntimeClient(BASE), "thread-1", "Summarise these notes");

    const [url, init] = fetchMock.mock.calls[0];
    expect(url).toBe(`${BASE}/api/conversations/thread-1/messages`);
    expect(JSON.parse(init.body)).toEqual({
      request: "Summarise these notes",
      source: "desktop",
      input_type: "text",
      approvals: "ASK",
      model: "",
      folder: "",
    });
  });

  it("carries how to go about a request beside it, never inside the text", async () => {
    const fetchMock = answering({ id: "m1" });

    await conversationApi.send(new RuntimeClient(BASE), "thread-1", "Tidy the folder", {
      approvals: "AUTO",
      model: "balanced",
    });

    const body = JSON.parse(fetchMock.mock.calls[0][1].body);
    expect(body.request).toBe("Tidy the folder");
    expect(body.approvals).toBe("AUTO");
    expect(body.model).toBe("balanced");
  });
});
