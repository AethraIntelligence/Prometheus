# ADR 0024: Generated code runs behind an OS boundary or not at all

## Status

Accepted - 2026-09-17. Replaces the plain subprocess used by `code.run`.

## Context

The original code tool created a temporary directory, stripped the environment,
used Python isolated mode and applied time, CPU, memory and output limits. Those
were useful resource controls, but the child was still an ordinary process. It
could read host files by absolute path and open network connections. Requiring
approval before launch did not make the approved program safe.

Calling that arrangement a sandbox would be worse than exposing its absence:
the same approval would mean "bounded scratch program" on one machine and
"arbitrary access as the signed-in user" on another.

## Decision

`code.run` uses one Docker contract on every platform. The container has a
read-only root, no capabilities, no new privileges, no network, bounded memory,
CPU, PIDs and open files, and runs as uid/gid 65534. Its only host mount is a
new temporary scratch directory; user and workspace paths are never mounted.

There is no plain-process fallback. An unavailable or explicitly disabled
Docker engine or installed Python sandbox image remains visible in the tool
description and returns a deterministic failure before a container is created.
Validation does not advertise `CODE_EXECUTION` as available in that state.

Wall time, CPU, memory, output, open-file, process and scratch-disk limits remain
in addition to the sandbox. A timeout or resource breach stops the container,
whose process always starts as the unprivileged nobody uid and gid.

The tool result records the selected sandbox, the denied network policy and all
files created in scratch with their sizes. The normal tool-call ledger persists
that result, so the observable effects survive the deletion of the temporary
directory.

## Consequences

- Code cannot open workspace files directly. Data enters through an explicit
  file tool and is passed into the program; output leaves through stdout and an
  explicit write tool.
- Python runs with its standard library but not the application's packages or
  the user's site packages.
- Docker and the `python:3.12-alpine` image are explicit runtime prerequisites for
  generated code. Prometheus never pulls an image during a task; installation
  is an administrator action and a missing image is reported before execution.
- The same negative boundary tests apply on macOS, Linux and Windows Docker
  hosts because the policy is a container contract rather than host-specific
  sandbox code.
