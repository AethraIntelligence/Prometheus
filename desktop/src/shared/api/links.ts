import { isTauri } from "@tauri-apps/api/core";
import { openUrl } from "@tauri-apps/plugin-opener";

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
