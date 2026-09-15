/**
 * Text the runtime wrote in Markdown, rendered rather than shown as source.
 *
 * Answers were split into paragraphs on blank lines and printed, so every list,
 * every `**` and every link arrived as punctuation. The model was not at fault:
 * nothing here had ever read Markdown.
 *
 * Raw HTML in the text is not rendered - an answer quotes pages it read, and a
 * page's markup is not something to put into the window. Links never navigate
 * the window either: they open in the person's browser.
 *
 * A link is drawn as a source chip - the publication's name, or the site's
 * when the text is only an address. An address printed in full, twice, is what
 * made a list of ten news items read as a list of URLs.
 */

import type { MouseEvent, ReactNode } from "react";
import ReactMarkdown, { type Components } from "react-markdown";
import remarkGfm from "remark-gfm";

import { openExternal } from "../api";

/** The site an address points at, the way a person names it. */
export function siteOf(href: string): string {
  try {
    return new URL(href).hostname.replace(/^www\./, "");
  } catch {
    return href;
  }
}

function textOf(children: ReactNode): string {
  if (typeof children === "string" || typeof children === "number") return String(children);
  if (Array.isArray(children)) return children.map(textOf).join("");
  return "";
}

const looksLikeAddress = (text: string) => /^(https?:\/\/|www\.)/i.test(text.trim());

const components: Components = {
  a({ href = "", children }) {
    const external = /^https?:\/\//i.test(href);
    if (!external) return <span className="md-link">{children}</span>;
    const text = textOf(children).trim();
    const label = !text || looksLikeAddress(text) ? siteOf(href) : text;
    const open = (event: MouseEvent) => {
      event.preventDefault();
      void openExternal(href);
    };
    return (
      <a className="source" href={href} title={href} onClick={open}>
        {label}
      </a>
    );
  },
  // An image would be fetched from wherever the text says, which the window's
  // policy refuses anyway; saying where it is beats a broken frame.
  img({ src = "", alt }) {
    return <span className="md-link">{alt || siteOf(src)}</span>;
  },
};

export function Markdown({ text }: { text: string }) {
  return (
    <div className="md">
      <ReactMarkdown remarkPlugins={[remarkGfm]} components={components}>
        {text}
      </ReactMarkdown>
    </div>
  );
}
