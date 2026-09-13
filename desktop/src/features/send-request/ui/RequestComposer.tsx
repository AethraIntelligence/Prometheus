/**
 * Where the request is typed. The one action the whole window exists for.
 *
 * Enter sends, Shift-Enter breaks the line - the convention people already
 * have. The field is never disabled while work is running: someone who has
 * thought of the next thing should be able to write it down, and nothing is
 * queued behind a disabled textarea.
 *
 * The row under the field takes whatever the page puts in it (`extras`): the
 * workspace a request will run in is another feature's control, and a feature
 * does not import another. The same goes for stopping: while work runs and the
 * field is empty, the page's `stop` control takes the send button's place, the
 * way every chat window people already use does. The moment something is
 * typed, sending is the action again - a new request is never blocked behind a
 * running one.
 */

import { useLayoutEffect, useRef, useState, type KeyboardEvent, type ReactNode } from "react";

import { ArrowUpIcon } from "../../../shared/ui";

/** Past this the field scrolls rather than pushing the conversation off the screen. */
const MAX_HEIGHT = 180;

interface Props {
  onSend: (request: string) => void | Promise<void>;
  disabled?: boolean;
  placeholder?: string;
  extras?: ReactNode;
  /** Shown instead of the send button while the field is empty. */
  stop?: ReactNode;
}

export function RequestComposer({ onSend, disabled = false, placeholder, extras, stop }: Props) {
  const [text, setText] = useState("");
  const field = useRef<HTMLTextAreaElement>(null);

  useLayoutEffect(() => {
    const element = field.current;
    if (!element) return;
    element.style.height = "auto";
    element.style.height = `${Math.min(MAX_HEIGHT, element.scrollHeight)}px`;
  }, [text]);

  const submit = async () => {
    const request = text.trim();
    if (!request || disabled) return;
    setText("");
    await onSend(request);
  };

  const onKeyDown = (event: KeyboardEvent<HTMLTextAreaElement>) => {
    if (event.key === "Enter" && !event.shiftKey) {
      event.preventDefault();
      void submit();
    }
  };

  return (
    <form
      className="composer"
      onSubmit={(event) => {
        event.preventDefault();
        void submit();
      }}
    >
      <textarea
        ref={field}
        aria-label="Tell Prometheus what you need"
        placeholder={placeholder ?? "Give it a goal."}
        rows={1}
        value={text}
        disabled={disabled}
        onChange={(event) => setText(event.target.value)}
        onKeyDown={onKeyDown}
      />
      <div className="dock-row">
        <div className="dock-extras">{extras}</div>
        {stop && text.trim().length === 0 ? (
          stop
        ) : (
          <button
            type="submit"
            className="send"
            aria-label="Send"
            disabled={disabled || text.trim().length === 0}
          >
            <ArrowUpIcon />
          </button>
        )}
      </div>
    </form>
  );
}
