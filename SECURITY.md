# Security policy

## Reporting a vulnerability

Report a vulnerability privately through GitHub:
[open a security advisory](https://github.com/NimbleBrainInc/todosforagents/security/advisories/new).
Please don't open a public issue for it.

Include what you found, how to reproduce it, and the version you ran (`uv pip show todosforagents`, or the
package version). We aim to acknowledge a report within three business days and to agree a
disclosure date with you once a fix is ready.

## Supported versions

Security fixes go into the latest release. Update to it to receive them.

## Scope notes

The local runtime is built for one person on one machine: it binds to loopback only, rejects a
foreign `Origin` or `Host`, and requires a per-install bearer token on HTTP (stdio needs none).
A report that it can be reached from another machine, or by a local process without the token,
is in scope.
