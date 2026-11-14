# Herdr Windows bridge

This repository contains the Linux-side stdio bridge currently installed at `~/.local/lib/herdr-windows-bridge/`.

## Current transport

`vm_bridge.py publish` creates a private Unix-domain socket under the runtime directory and relays one byte stream. The configured HerdR machine target runs `vm_bridge.py via <approved peer command>`; `via` waits up to ten seconds for the publisher socket and then relays the stream. The publisher is single-use and removes its socket on exit.

The relay enforces a fixed UID, private directory/socket permissions, same-UID peer credentials, an exact command allowlist, and bounded buffers. Preserve those controls when changing the lifecycle.

## Known operational gap

The transport has no supervisor or health/restart integration in this repository yet. If the publisher process is absent, `via` times out. The initial source import includes the installed implementation and its existing regression tests; the next change should make publisher startup, single-instance ownership, reconnect behavior, and health status explicit.

HerdR's `--via` contract appends the remote command line as the final argument. This implementation checks that argument against the allowlist, then waits for the publisher socket; it does not launch the remote command itself. The publisher's upstream stdio owner and its startup hook must be identified before adding a supervisor, so a service does not report a healthy socket while disconnected from Windows.

## Files

- `vm_bridge.py` — current installed transport implementation.
- `test_vm_bridge.py`, `test_edges.py`, `test_regressions.py` — existing local regression scripts.
