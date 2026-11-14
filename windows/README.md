# herdr-bridge

A small native Windows controller for the existing, reviewed Herdr bridge. Version 0.2.2.

## Open

Double-click **herdr-bridge** on your Desktop or in the Start menu. A native window shows Connected, Ready for the VM, Reconnecting, Stopped, or an explicit failure. Opening/closing the window does not start or stop the transport.

- Start bridge starts only the existing reviewed transport if no helper owns its lock.
- Stop bridge requests graceful disconnection. Herdr cores and workers keep running.
- Open Herdr opens the installed Herdr app.
- Diagnostics shows local connection metadata, never terminal payloads or credentials.

## Recovery

If VM SSH is stopped after reboot, run `sudo systemctl start ssh` in the VM terminal and enter the password there. The existing Windows helper retries automatically. This app does not run sudo, display an administrator prompt, change service startup, or configure automatic startup.

If Windows itself reboots or you sign out, open this app and choose Start bridge after opening your existing Windows Herdr core. A changed/missing core fails closed rather than starting a replacement. Do not duplicate machine registration. Saved terminal slots after a VM reboot are not proof that agents survived; resume saved conversations separately.

## Security and scope

No inbound Windows port, firewall change, account, service, scheduled task or autostart is installed. Run normally, never as administrator. Uses the existing account, dedicated SSH key, strict pinned host key and source-restricted VM SSH path. The VM account and VM root can access files/run commands as your Windows account through this bridge. This is not a read-only or Jitbit-only connection.

Transport is pinned by SHA256 to the independently reviewed version. It opens only the already-existing core pipe and preserves original server/holder identity for its lifetime. Disconnects discard local transport buffers; an operation in flight can have an uncertain outcome, so verify before retrying consequential work.

## Files

- herdr_bridge_app.py: native tkinter UI/controller, standard library only.
- Launch-herdr-bridge.cmd: direct local launcher.
- transport/windows_bridge.py: versioned Windows transport source; install it and bridge_config.py in `%LOCALAPPDATA%\HerdrBridge` before starting the controller.
- Runtime: C:\\Python312, Tk 8.6 (already installed, not bundled).
- Existing VM endpoint: the remote publisher command configured in the local bridge config.
- Host-specific values live in `%LOCALAPPDATA%\HerdrBridge\bridge.json`. Create it from `bridge.json.example`, enter the verified Windows account/core/pipe, VM host/user, remote publisher command, and key paths, then keep it private. `bridge.json` is ignored by Git. Never commit it or private keys.

## Validate

Run `py -3 -I herdr_bridge_app.py --self-test` for isolated controller tests, `--status` for a non-mutating runtime check, or `--smoke-test` to briefly render the window. None of these starts/stops the transport.

## Remove

Close the UI, use Stop bridge if you want to disconnect, then delete the herdr-bridge shortcuts and this project folder. Do not delete runtime keys or VM SSH configuration as part of uninstall. No registry or scheduled-task cleanup is needed.
