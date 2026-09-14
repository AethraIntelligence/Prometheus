# ADR 0022: A plugin is a declared installation, granted from the window

## Status

Accepted - 2026-09-14. Builds on ADR 0015.

## Context

Phase 14 made any MCP server an ordinary set of tools, and the Plugins screen
could add one from a name and a command. In use that asked a person for four
things they do not have: the package that runs the server, the variable its
token goes in, what each of its tools does to the world, and - because a
connected integration nobody was granted reaches nobody - an edit to an
employee's YAML file. The result was a screen that worked for the person who
wrote the platform and for nobody else.

Running twenty real servers to write their declarations also found two defects
no test had seen: a tool list longer than 64 KiB (Brave Search, Notion) could not
be read, because asyncio's stream limit refused the line before the transport's
own 8 MiB check; and a window opened from the Dock could not start `npx`, `uvx`
or `docker` at all, because launchd's `PATH` does not reach where they live.

## Decision

**A plugin is a directory under `plugins/`** - `plugin.yaml` and `icon.svg` -
read by `infrastructure/integrations/yaml_catalog.py`, the fourth registry of the
employee/workflow/scenario shape. It states the runtime (NODE, PYTHON, DOCKER),
the command and arguments, the settings a person supplies, the capabilities it
brings and an effect map. Installing one is `POST /api/plugins/{id}/install` with
values for those settings and nothing else.

**The effect map is a local declaration, written from what the server was seen
to offer.** It is exactly what a person classifying tools by hand would store
(ADR 0015), so the trust boundary does not move. Shipped plugins mark reads and
local writes only; a test fails the build if one marks anything else as not
asking, except Slack's single SEND. A tool the map does not name is EXECUTE and
asks, so a server that grows a new tool gains a question, not a pass.

**Only the catalog says what runs.** The install route takes a plugin id and
setting values. A value for a setting the plugin does not declare is refused,
because it would otherwise land in the server's environment. Arbitrary commands
remain the "Custom server" form, which says what it is.

**A secret goes to the credential store under its setting's key** and reaches
the server as that environment variable; a server wanting it as an argument
names `${KEY}`, which the transport fills from the environment at start. The
record holds the placeholder, never the value. A token already kept satisfies a
required setting, so reinstalling does not ask for it again.

**A grant can be made from the window.** `integrations.granted_to` (migration
024) names employees on this machine's record; `domain/integrations/grants.py`
treats it exactly as `integrations:` in an employee's file, and it can only add.
The name is added to the expanded definition too, so routing by service
(ADR 0018) finds a holder granted either way. An install is preselected with
`suggested_holders`: the employees already declaring a capability the plugin
brings, or everyone where none does - a plugin granted to nobody reads as broken.

**Icons are path data.** The loader keeps `viewBox` and each `<path d>` and
drops every other element; the window renders them as SVG elements on a tile.
The content security policy loads no image the window did not ship, and markup
from a file is never rendered as HTML.

The transport now reads lines up to its own limit, skips notifications between
a request and its reply, finds programs in the usual install locations, and puts
the program's directory first on the child's `PATH`.

## Consequences

- Adding a plugin to the catalog is adding a directory; no Python changes.
  `plugins/README.md` says how, including how to write the effect map.
- A person installs a service by choosing it and pasting a token, chooses who
  may use it, and can change that later without touching a file.
- Remote MCP servers with OAuth (Gmail, Linear, the hosted Slack and GitHub
  servers) are not in the catalog: the transport is stdio. Those arrive with a
  second transport, not with a flag here.
- Servers that could not be verified here were left out rather than shipped on
  trust: the reference SQLite and Postgres servers failed to start, and Stripe's
  did not answer a handshake without a real key.

## Addendum - services that sign in through the browser (Gmail, Google Drive)

Google cannot be reached with a pasted token, and a shared OAuth client for the
whole platform is not available to a local open project: Gmail's scopes are
restricted, and a client serving everyone needs Google's security assessment
and a secret nobody can keep in a repository. So each person uses their own
Desktop-app client, and the plugin carries the steps to create one (`setup`).

Both plugins run `workspace-mcp` with one service each, and each keeps its
tokens in its own directory (`env`), because one directory shared by two
processes with different scopes lets the second sign-in overwrite the first.

`sign_in` names a READ tool the window's button calls. An unauthorised server
opens its own sign-in page and answers with an error; an authorised one answers
with data. The platform reads only whether the call succeeded, never the text,
so the rule against deciding by message text holds. The tokens stay with the
server on this machine; removing a plugin does not delete them, and a person
who wants them gone deletes `~/.google_workspace_mcp/prometheus-*`.

### Revised the same day: the installation supplies the client

Ten minutes in Cloud Console per person was the step people would not take. So
an installation may supply credentials to plugins by name -
`PROMETHEUS_PLUGIN_CREDENTIALS__<NAME>` in `.env` - and the owner of an
installation creates one Google client for everyone who uses it. A supplied
credential satisfies a required setting, is handed to the server at connect
after anything a person stored under the same name, and is never written to the
credential store or shown. The window folds supplied settings and the Cloud
Console steps into "Use your own credentials instead", and installing a plugin
that signs in through the browser starts the sign-in at once.

The cost is Google's, and stated plainly: an unverified client with restricted
scopes shows a "Google hasn't verified this app" screen and serves at most 100
users. That fits a local platform run by a person or a small team; a public
product would need Google's verification, which this does not attempt. The
client secret of an installed application is not confidential by Google's own
account, which is why a shared one is acceptable at all - and why it still
never enters a public repository, where it would be flagged and revoked.

## Alternatives considered

**Letting a server's own annotations classify its tools.** Faster to write, and
it hands the classification to the party being classified (§24).

**Writing grants into employee YAML from the window.** One place for grants, but
the files are the declarations under version control, and a settings click that
edits them silently is a diff nobody reviewed.

**Bundling icons into the frontend.** It would put every service's name and mark
in the window's source, so adding a plugin would need a frontend build.
