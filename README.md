# Herdr Windows bridge

This repository contains the transport that connects a Windows Herdr core to a Linux VM. It is a bridge/helper repo, separate from the main email application and the `redis_queue` service. It does not send order emails or attachments.

## How the bridge works

The Linux `vm_bridge.py via` command waits for the publisher's private Unix socket and relays the Herdr byte stream. The Windows helper owns that publisher connection: it verifies the existing Herdr core and holder, opens the existing named pipe, and starts the Linux `publish` command through the pinned SSH connection. The Linux publisher accepts one stream and removes its socket when the connection ends.

The Windows controller is in [`windows/README.md`](windows/README.md). It starts and stops only the bridge helper; it does not start, stop, or restart Herdr cores or workers. It is not installed as a service or automatic startup task.

## Configuration

Linux transport source: `vm_bridge.py`. Its installed copy is under `~/.local/lib/herdr-windows-bridge/` on the VM.

Windows source and launcher are under `windows/`. The runtime files belong in `%LOCALAPPDATA%\HerdrBridge`. Create the local `bridge.json` from [`windows/bridge.json.example`](windows/bridge.json.example) and fill in the machine's verified core, account, pipe, SSH host/user, publisher command, and key paths. Keep that config and SSH keys local; `bridge.json` is gitignored and must not be committed.

## Verify connectivity

After starting the Windows helper, check the HerdR machine state with `hc machine ls` and verify a harmless command with `hc exec --on windows "hostname"`. A connected bridge confirms remote Herdr access; it does not verify email delivery or attachment handling in the separate application.

The Windows controller also provides a local self-test and status view. See [`windows/README.md`](windows/README.md) for commands and recovery details.

## Linux relay behavior

The `via` path validates the fixed UID, private directory/socket permissions, same-UID peer credentials, exact command allowlist, and bounded buffers. It waits up to ten seconds for the publisher socket and does not launch the publisher itself. Preserve these controls when changing the Linux transport.

Files:

- `vm_bridge.py` — Linux-side publisher and `via` relay.
- `test_vm_bridge.py`, `test_edges.py`, `test_regressions.py` — Linux relay regression scripts.
- `windows/herdr_bridge_app.py` — Windows controller.
- `windows/transport/windows_bridge.py` — Windows named-pipe and SSH publisher owner.
- `windows/bridge_config.py` — validated host-local settings loader and status validation.
