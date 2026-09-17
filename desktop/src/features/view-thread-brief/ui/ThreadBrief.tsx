import { useCallback, useEffect, useState } from "react";

import { sessionApi, SessionBriefView, type SessionBrief } from "../../../entities/session";
import { report, useRuntime } from "../../../shared/api";
import { describe } from "../../../shared/lib";
import { Modal } from "../../../shared/ui";

interface Props {
  conversationId: string;
  /** Changes when the thread gained a turn, so the brief is read again. */
  refresh?: number;
}

/**
 * The thread's brief behind one button in the header: what it is working
 * towards, what it decided, what is still open and what it produced.
 */
export function ThreadBrief({ conversationId, refresh }: Props) {
  const client = useRuntime();
  const [open, setOpen] = useState(false);
  const [brief, setBrief] = useState<SessionBrief | null>(null);
  const [problem, setProblem] = useState("");

  const load = useCallback(async () => {
    try {
      setBrief(await sessionApi.brief(client, conversationId));
      setProblem("");
    } catch (error) {
      setProblem(describe(error));
    }
  }, [client, conversationId]);

  useEffect(() => {
    if (open) void load();
  }, [open, load, refresh]);

  const resolve = async (question: string) => {
    try {
      setBrief(await sessionApi.resolve(client, conversationId, question));
    } catch (error) {
      const said = describe(error);
      setProblem(said);
      report(said);
    }
  };

  return (
    <>
      <button type="button" className="head-btn" onClick={() => setOpen(true)}>
        Brief
      </button>
      {open && (
        <Modal
          title="Thread brief"
          note="What Prometheus is shown of this thread before each request. Older requests are folded into stages; the requests themselves are never changed."
          onClose={() => setOpen(false)}
        >
          {problem && (
            <p className="problem" role="alert">
              {problem}
            </p>
          )}
          {brief ? (
            <SessionBriefView brief={brief} onResolve={(question) => void resolve(question)} />
          ) : (
            !problem && <p className="working">Reading the thread…</p>
          )}
        </Modal>
      )}
    </>
  );
}
