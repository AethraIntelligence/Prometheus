/**
 * One turn of the conversation: what was asked, and what came back.
 *
 * Either the answer or the fact that work is still happening. What is *missing*
 * from a finished answer is shown rather than hidden: a run that did most of a
 * job and says so is more useful than one that reports success, and Phase 11
 * found the second failure mode is the expensive one.
 *
 * What the work looked like arrives through `work` rather than being drawn
 * here, because the trail is another entity's and one slice does not reach into
 * another. This one only decides where it goes: folded behind the status line
 * while the turn runs, and behind "Worked for" once it has an answer. It used to
 * be open while the turn ran, which put the runtime's own step-by-step - error
 * types included - in front of somebody who had said "Hello". Every step is
 * still one click away; nothing is left out, only put behind the line.
 */

import { useState, type ReactNode } from "react";

import { CheckIcon, ChevronRight, CopyIcon, Markdown, WarningIcon } from "../../../shared/ui";
import { statusLine } from "../model/status";
import type { Artifact, Message } from "../model/types";
import { FileCard } from "./FileCard";

/**
 * How long a finished turn took, the way a person would say it.
 *
 * Arithmetic on two timestamps the runtime wrote - formatting, not a rule about
 * the work. Empty when there is no finish to measure to.
 */
export function workedFor(message: Message): string {
  if (!message.finished_at) return "";
  const took = Date.parse(message.finished_at) - Date.parse(message.created_at);
  if (Number.isNaN(took)) return "";
  const seconds = Math.max(0, Math.round(took / 1000));
  if (seconds < 60) return `${seconds}s`;
  const minutes = Math.floor(seconds / 60);
  if (minutes < 60) return `${minutes}m ${seconds % 60}s`;
  return `${Math.floor(minutes / 60)}h ${minutes % 60}m`;
}

interface Props {
  message: Message;
  work?: ReactNode;
  /** Which memories the answer was given, and why - composed by the widget. */
  memory?: ReactNode;
  /** Run-level actions composed by the page, such as opening its causal trace. */
  actions?: ReactNode;
  /** Opening a file the turn produced. Without it the cards are shown and do nothing. */
  onOpenFile?: (message: Message, artifact: Artifact) => void;
}

export function MessageTurn({ message, work, memory, actions, onOpenFile }: Props) {
  const [open, setOpen] = useState(false);
  const [copied, setCopied] = useState(false);
  const took = workedFor(message);

  const copy = () => {
    void navigator.clipboard
      ?.writeText(message.answer)
      .then(() => {
        setCopied(true);
        setTimeout(() => setCopied(false), 1500);
      })
      .catch(() => {
        // A clipboard the platform refuses is not worth a message: the text is
        // on screen and can be selected by hand.
      });
  };

  return (
    <li className="turn-pair">
      <div className="turn from-user">
        <div className="bubble">{message.text}</div>
      </div>
      <div className={`turn assistant ${message.status.toLowerCase()}`}>
        {message.answered ? (
          <>
            {took &&
              (work ? (
                <button
                  type="button"
                  className="worked"
                  aria-expanded={open}
                  onClick={() => setOpen((shown) => !shown)}
                >
                  <ChevronRight className="cv" />
                  Worked for {took}
                </button>
              ) : (
                <p className="worked still">Worked for {took}</p>
              ))}
            {open && work}
            {message.answer && <Markdown text={message.answer} />}
            {(message.artifacts ?? []).length > 0 && (
              <div className="files" aria-label="Files from this answer">
                {(message.artifacts ?? []).map((artifact) => (
                  <FileCard
                    key={artifact.path}
                    artifact={artifact}
                    onOpen={(chosen) => onOpenFile?.(message, chosen)}
                  />
                ))}
              </div>
            )}
            {message.missing.length > 0 && (
              <section className="rescard short" aria-label="What is still missing">
                <div className="res-top">
                  <span className="res-ico">
                    <WarningIcon />
                  </span>
                  <span className="res-meta">
                    <b>Still missing</b>
                    <span>
                      {message.missing.length}{" "}
                      {message.missing.length === 1 ? "thing" : "things"} the work did not do
                    </span>
                  </span>
                </div>
                <ul className="missing">
                  {message.missing.map((item) => (
                    <li key={item} className="filerow text">
                      {item}
                    </li>
                  ))}
                </ul>
              </section>
            )}
            {memory}
            {actions}
            {message.answer && (
              <div className="react">
                <button type="button" aria-label={copied ? "Copied" : "Copy"} onClick={copy}>
                  {copied ? <CheckIcon /> : <CopyIcon />}
                </button>
              </div>
            )}
          </>
        ) : (
          <>
            {work ? (
              <button
                type="button"
                className="working worked"
                aria-expanded={open}
                aria-live="polite"
                onClick={() => setOpen((shown) => !shown)}
              >
                <span className="spin" aria-hidden="true" />
                {statusLine(message)}
                <ChevronRight className="cv" />
              </button>
            ) : (
              <p className="working" aria-live="polite">
                <span className="spin" aria-hidden="true" />
                {statusLine(message)}
              </p>
            )}
            {open && work}
          </>
        )}
      </div>
    </li>
  );
}
