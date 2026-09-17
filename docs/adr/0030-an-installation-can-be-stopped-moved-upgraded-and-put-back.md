# ADR 0030: An installation can be stopped, moved, upgraded and put back

## Status

Accepted - 2026-09-17.

## Context

Until now the platform was used from a clone. A person who installs an
application instead cannot run `alembic upgrade head`, cannot find a key file to
back up, and has nobody to ask when a second copy of the app started a second
engine on the same database. Several things that were acceptable from a clone
were not acceptable in an installer:

- a database written by a newer version was opened, and migrated, by an older
  one;
- a migration was a manual step with no backup and no recovery if it was killed;
- the master key lived in a plaintext file beside the data, and credentials from
  before the encrypted store were never removed from disk;
- two engines could run against one data directory;
- the brake (`prometheus stop`) stopped only actions on a screen, and nothing
  stopped an effect whose approval arrived in the same instant somebody pulled it;
- a fresh installation with no model provider refused to start, so the window
  that would let a person add one never opened;
- every shipped plugin ran an unpinned package, whatever its registry served;
- there was no backup, no restore and no update path.

## Decision

**One stop, durable, checked at the effect.** The STOP record is versioned JSON
beside the data directory; a plain-text record from before still means stopped,
and an unreadable one is stopped. Engaging it forbids new work (task runner,
`Runs`, scheduler), sweeps what exists (parked approvals answered no, running
tasks asked to stop, objectives closed, work a crash left closed in the store,
leases revoked, integrations disconnected, sandboxes killed) and writes an audit
line per workspace. The executor reads it again after the approval gate answers
and immediately before calling the tool, so no approval carries an effect past a
stop. A running process watches the record, so a stop set by the CLI or the
shell's menu is enforced; a process started on a stopped machine enforces it
before resuming anything. Releasing starts nothing. No report claims an effect
was undone.

**One owner per data directory, proved by the kernel.** A process that does work
takes an OS lock (`flock`/`msvcrt`) on a file beside the data directory and
writes who it is next to it. A second one is refused with the owner's pid and
address; a lock left by a dead process is free because the kernel released it.
The shell also allows a single instance and focuses the existing window.

**Upgrades are judged, backed up and recoverable.** The store's revision is read
without writing. A revision this code does not know is refused untouched; an
unversioned store is refused; a known older one is migrated after a preflight
(writable directory, free space, SQLite integrity) and a verified online backup,
with a marker naming the backup. A failed migration puts the backup back; a
killed one is put back by the next start, which finds the marker. `serve`
migrates on start. PostgreSQL waits for an acknowledged external backup.

**The master key lives in the OS credential vault.** Keychain, Credential
Locker or Secret Service through `keyring`; a key file from before is moved by
write-verify-remove, repeatable after a crash, and two differing keys are never
reconciled by guessing. A headless machine sets the key in the environment or
chooses the file backend explicitly; there is no silent fallback. The plaintext
credential file is removed once every value reads back from the encrypted store.

**Backups are an archive with a manifest; restore checks everything first.** A
backup is an allow-list (database snapshot, settings, workspaces, the files work
produced, custom declarations) with a size and SHA-256 per entry and a format
version. Secrets are included only sealed with a passphrase (scrypt, AES-GCM).
A restore verifies the manifest, every digest, unlisted content, path safety,
database integrity and schema, free space and the passphrase before writing;
then stages beside the data directory and switches with two renames, keeping the
replaced installation. Files that changed since the backup are never
overwritten. From the window, a restore runs between one process image and the
next.

**Updates wait for a safe point.** The runtime reports effects in flight; before
an update it holds new effects and waits for running ones, and the shell installs
only when told the runtime is ready. The updater verifies the package's minisign
signature against the key compiled into the build; a build without a key has no
updater.

**Model routing waits for work.** A client binds its model on first use, so a
fresh installation starts and explains the missing provider when work is asked
for.

**Plugins run only what was reviewed.** `plugins/catalog.lock.json` holds a
digest of each declaration and the registry's published digest of its pinned
artifact. An unlocked or changed declaration is not offered; an install asks the
registry again and refuses on any difference or on no answer.

**Releases are built and drilled per OS.** The release workflow builds on each
supported OS's runner with a bundled relocatable Python, checks licenses (GPL
dependencies of the desktop driver are left out of the bundle), produces SBOMs,
verifies Apple, Authenticode and minisign signatures before publishing, installs
the artifact and runs the release drill against it.

## Consequences

The packaged application needs no terminal from install to restore. The cost is
a small amount of state beside the data directory (the stop record, the lock and
its owner record, a migration marker while one runs) and an explicit choice on
headless machines. Desktop computer use is not part of the packaged build until
its driver's licensing is resolved; browser computer use is unaffected.
Transitive dependencies of plugin packages are still resolved by their package
managers at run time. Signing identities belong to the repository owner, so the
per-OS release drill and a published update channel exist only once a release is
cut with them.
