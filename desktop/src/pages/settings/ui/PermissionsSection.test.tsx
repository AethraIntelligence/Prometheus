import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { RuntimeClient, RuntimeProvider } from "../../../shared/api";
import { PermissionsSection } from "./sections/PermissionsSection";

describe("Settings → Permissions", () => {
  it("shows exact authority and lets the user revoke it", async () => {
    let active = true;
    const fetch = vi.fn(async (_input: RequestInfo | URL, init?: RequestInit) => {
      if (init?.method === "DELETE") {
        active = false;
        return new Response(JSON.stringify({ revoked: true }), { status: 200 });
      }
      return new Response(
        JSON.stringify({
          leases: active
            ? [
                {
                  id: "lease-1",
                  workspace_id: "default",
                  subject: "employee-1",
                  action: "fs.write",
                  resource: "path:reports/final.md",
                  limits: { max_payload_bytes: 4096 },
                  grant: "PERSISTENT",
                  reason: "Approved for the report",
                  approval_id: "approval-1",
                  created_at: "2026-09-17T09:00:00Z",
                  expires_at: null,
                },
              ]
            : [],
        }),
        { status: 200 },
      );
    });

    render(
      <RuntimeProvider client={new RuntimeClient("http://runtime", fetch as never)}>
        <PermissionsSection />
      </RuntimeProvider>,
    );

    expect(await screen.findByText(/fs.write · path:reports\/final.md/)).toBeInTheDocument();
    expect(screen.getByText(/max_payload_bytes/)).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Revoke" }));
    await waitFor(() => expect(screen.getByText("No remembered permissions.")).toBeInTheDocument());
  });
});
