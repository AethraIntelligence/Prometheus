/**
 * What the runtime says about a context of work.
 *
 * `active` and `file_root` are read, never worked out here. Which workspace this
 * machine is in is a file beside the database that the core owns, and where a
 * workspace's files live follows from settings this window cannot see - a
 * window that guessed at either would be a second answer to a question the core
 * already answers.
 */

export interface Workspace {
  id: string;
  name: string;
  description: string;
  /** Where its files are on this machine: each thread gets a folder of its own under it. */
  file_root: string;
  /** Folders a person saved to point a thread at. Absent from an older runtime. */
  folders?: string[];
  /** The first workspace. It holds everything written before there were others. */
  is_default: boolean;
  active: boolean;
  created_at: string;
}

export interface WorkspaceList {
  workspaces: Workspace[];
}
