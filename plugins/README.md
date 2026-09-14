# Plugins

A plugin is a service the platform knows how to install: one directory here,
holding `plugin.yaml` and, if it has one, `icon.svg`. Settings -> Plugins in the
desktop window lists every directory, and installing one is choosing it and
pasting a token. Adding a plugin is adding a directory; nothing in Python
changes (`infrastructure/integrations/yaml_catalog.py`).

```yaml
id: example                    # the directory name; lower-case, digits, hyphens
name: Example
description: One line under the name
about: A paragraph shown before installing
category: Productivity         # plugins are grouped by this
publisher: Example Inc.
homepage: https://example.com
popular: true                  # also listed under Popular
runtime: NODE                  # NODE (npx), PYTHON (uvx) or DOCKER
command: npx
args: ["-y", "@example/mcp-server", "--root", "${FOLDER}"]
settings:
  - key: EXAMPLE_TOKEN         # SECRET: stored encrypted, handed over as this env variable
    label: API token
    kind: SECRET
    help_url: https://example.com/tokens
  - key: FOLDER                # PATH or TEXT: filled into ${FOLDER} in args
    label: Folder
    kind: PATH
capabilities: [FILE_ACCESS]    # what work can reach it (the closed vocabulary)
effects:                       # what each tool does to the world, written HERE
  READ: [list_things, get_thing]
  WRITE: [create_thing]
icon: { color: "#111111", background: "#FFFFFF" }
env:                           # fixed variables; may name a plain setting as ${KEY}
  TOKENS_DIR: "~/.example/${ACCOUNT}"
setup:                         # steps shown above the form, where a token is not enough
  - text: Create an OAuth client of type Desktop app.
    url: https://example.com/clients
sign_in:                       # for servers that sign in through the browser
  label: Sign in with Example
  tool: list_things            # a READ tool; success means signed in
  arguments: { limit: 1 }
  help: A page opens in your browser.
```

A SECRET setting may also be supplied by the installation rather than typed by
each person: `PROMETHEUS_PLUGIN_CREDENTIALS__<KEY>=value` in `.env`. The window
then tucks that setting (and the `setup` steps) under "Use your own credentials
instead". Anything a person stores under the same key wins.

A plain setting (PATH or TEXT) that no argument names is handed to the server as
its own environment variable, the way a secret is.

## Signing in through the browser

Some services (Google) cannot be reached with a pasted token. `sign_in` names a
harmless read: the window's button calls it, a server that is not yet authorised
opens its own sign-in page on this machine, and pressing Check calls it again.
Only whether the call succeeded is read - never the server's reply - so nothing
depends on how a server words its request to sign in.

## What the effects mean

The server does not get a vote on whether its own actions need approval
(ADR 0015). `effects` is this platform's classification, and a tool it does not
name is EXECUTE, which asks a person every time. So list a tool under READ only
when it cannot change anything anywhere; leave anything that creates, sends,
deletes or spends out, and it will ask. The tool names come from running the
server and listing what it offers - the Plugins screen shows them once a plugin
is installed.

## Icons

`icon.svg` is read as path data only: its `viewBox` and every `<path d>`; any
other element is ignored. Brand marks here come from
[Simple Icons](https://simpleicons.org) (CC0-1.0); generic ones from
[Material Design Icons](https://pictogrammers.com/library/mdi/) (Apache-2.0).
Brand names and logos belong to their owners and are used to identify the
service being connected.
