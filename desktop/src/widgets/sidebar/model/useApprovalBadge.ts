import { useEffect, useRef, useState } from "react";

import { approvalApi, type Approval } from "../../../entities/approval";
import type { RuntimeClient } from "../../../shared/api";

const STORAGE_KEY = "prometheus.notified-approvals";

function remembered(): Set<string> {
  try {
    return new Set(JSON.parse(localStorage.getItem(STORAGE_KEY) ?? "[]") as string[]);
  } catch {
    return new Set();
  }
}

export function approvalNotificationState(items: Approval[], seen: Set<string>) {
  const actionable = items.filter((item) => item.actionable !== false);
  return {
    count: actionable.length,
    newIds: actionable.map((item) => item.id).filter((id) => !seen.has(id)),
  };
}

export function useApprovalBadge(client: RuntimeClient) {
  const [count, setCount] = useState(0);
  const [fresh, setFresh] = useState(0);
  const seen = useRef(remembered());

  useEffect(() => {
    let current = true;
    let quiet: number | undefined;
    const read = async () => {
      try {
        const inbox = await approvalApi.inbox(client);
        if (!current) return;
        const { count: actionableCount, newIds } = approvalNotificationState(
          inbox.pending ?? [],
          seen.current,
        );
        setCount(actionableCount);
        if (newIds.length > 0) {
          setFresh(newIds.length);
          newIds.forEach((id) => seen.current.add(id));
          localStorage.setItem(STORAGE_KEY, JSON.stringify([...seen.current].slice(-200)));
          window.clearTimeout(quiet);
          quiet = window.setTimeout(() => setFresh(0), 5000);
        }
      } catch {
        // The badge is advisory. The inbox page gives a durable error if opened.
      }
    };
    void read();
    const timer = window.setInterval(() => void read(), 3000);
    return () => {
      current = false;
      window.clearInterval(timer);
      window.clearTimeout(quiet);
    };
  }, [client]);

  return { count, fresh };
}
