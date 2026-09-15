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

/** Whether `openFile` can do anything here, so a page offers no button that cannot. */
export function canOpenFiles(): boolean {
  return isTauri();
}
