import { isTauri } from "@tauri-apps/api/core";
import { open } from "@tauri-apps/plugin-dialog";

/**
 * Asking the person to choose files on this machine, with the system's dialog.
 *
 * Paths come back, not contents: the runtime is on the same machine and reads
 * the file itself, so carrying the bytes through the window would make it a
 * second copy of something the person already has.
 *
 * `null` means there is no dialog to show - the page opened in a browser rather
 * than in the shell - and the caller falls back to asking for a path. An empty
 * list means the person closed the dialog, which is not the same thing.
 */
export async function chooseFiles(options: {
  multiple?: boolean;
  title?: string;
}): Promise<string[] | null> {
  if (!isTauri()) return null;
  const chosen = await open({
    multiple: options.multiple ?? false,
    directory: false,
    title: options.title,
  });
  if (chosen === null) return [];
  return Array.isArray(chosen) ? chosen : [chosen];
}

/**
 * Asking the person to choose folders, with the same dialog and the same answers:
 * `null` when there is no dialog here, an empty list when it was closed.
 */
export async function chooseFolders(options: {
  multiple?: boolean;
  title?: string;
}): Promise<string[] | null> {
  if (!isTauri()) return null;
  const chosen = await open({
    multiple: options.multiple ?? false,
    directory: true,
    title: options.title,
  });
  if (chosen === null) return [];
  return Array.isArray(chosen) ? chosen : [chosen];
}
