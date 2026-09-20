import { isTauri } from "@tauri-apps/api/core";
import { openPath, openUrl } from "@tauri-apps/plugin-opener";

/**
 * Open a page in the person's own browser - where a plugin's token is issued.
 *
 * Through the shell in a window, whose webview navigates nowhere but its own
 * page, and whose permission allows https and nothing else. In a browser there
 * is no shell, and a new tab is the same thing.
 */
export async function openExternal(url: string): Promise<void> {
  if (!url.startsWith("https://")) return;
  if (isTauri()) {
    await openUrl(url);
    return;
  }
  window.open(url, "_blank", "noopener,noreferrer");
}

/**
 * Open a file on this machine in whatever the system opens it with.
 *
 * Only a window can: a browser page has no way to reach a path, and pretending
 * with a download would put a copy somewhere the person did not choose.
 */
export async function openFile(location: string): Promise<void> {
  if (!location || !isTauri()) return;
  await openPath(location);
}

/**
 * Open the folder a file is in, which is the honest form of "save it".
 *
 * The file is already on this machine, in the folder the thread works in - so
 * what a person wants is to be taken to it, not to be handed a second copy of
 * something they already have.
 */
export async function showInFolder(location: string): Promise<void> {
  const folder = parentOf(location);
  if (!folder || !isTauri()) return;
  await openPath(folder);
}

/** The directory part of a path, on either separator. Empty when there is none. */
export function parentOf(location: string): string {
  const cut = Math.max(location.lastIndexOf("/"), location.lastIndexOf("\\"));
  return cut > 0 ? location.slice(0, cut) : "";
}

/**
 * Put text on the clipboard, saying whether it got there.
 *
 * The API is refused outright in some contexts rather than failing quietly, so
 * the answer is carried back and the caller says what happened instead of
 * pretending it worked.
 */
export async function copyText(text: string): Promise<boolean> {
  try {
    await navigator.clipboard.writeText(text);
    return true;
  } catch {
    return false;
  }
}

/** Whether `openFile` can do anything here, so a page offers no button that cannot. */
export function canOpenFiles(): boolean {
  return isTauri();
}
