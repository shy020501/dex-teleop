# Windows client — MANUS Core / SDK 3.2.1

This client controls the Linux simulation. The browser displays simulation and captures focused keys through separate HTTP endpoints. No MANUS SDK is required on Linux. Previous Windows keyboard and MANUS operation was user-confirmed. The updated browser keyboard path still requires Windows acceptance; Linux tests do not prove browser/OS key handling.

## Server connection settings

The examples use `{user}@{teleop-server-ip}`. Replace `{user}` with your Linux login name and `{teleop-server-ip}` with the server IP, removing the braces. Keep the argument quoted in PowerShell. Replace `/path/to/dex-teleop` with the absolute Linux workspace path.

Pass this combined address to `-ServerAddress`. The scripts also accept a plain hostname/IP or your own SSH alias, with an optional `-UserName`. An explicit `user@host` takes precedence over `-UserName` and `DEX_TELEOP_SSH_USER`. For a nonstandard SSH port, add `-SshPort PORT` to the scripts or `-P PORT` to `scp`; otherwise SSH uses its local configuration and default port.

You can set defaults for the current PowerShell window instead of repeating script arguments:

```powershell
$env:DEX_TELEOP_SSH_HOST = '{user}@{teleop-server-ip}'
$env:DEX_TELEOP_REMOTE_ROOT = '/path/to/dex-teleop'
```

These environment variables are inherited by child processes; set them again in a new window or pass the script arguments. No server address or account is built into the client. `Update-Teleop.ps1` requires `-RemoteRoot` or `DEX_TELEOP_REMOTE_ROOT`; it does not assume a home-directory layout.

## 1. Obtain and install

On Linux, build the client ZIP from the workspace with `./scripts/build_windows_bundle.sh`. Use the Python environment containing the project's dependencies.

The build publishes `dist/dex_teleop_windows.zip` and the earlier download name `dist/bench2dex_windows_teleop.zip` with identical contents. Both contain the console-only client and `-ShowRtt` support. Stop the existing client and extract into the same project folder from which you run `windows\Run-Teleop.ps1`.

In PowerShell:

```powershell
scp '{user}@{teleop-server-ip}:/path/to/dex-teleop/dist/dex_teleop_windows.zip' .
Expand-Archive .\dex_teleop_windows.zip -DestinationPath .\dex_teleop_client
cd .\dex_teleop_client
powershell -ExecutionPolicy Bypass -File windows\Setup-Teleop.ps1
```

Install Python 3.11 x64 first if `py -3.11` is unavailable. Setup creates a project-local `.venv`, without modifying other Python installations.

## 2. SSH and keyboard-only check

Keep one PowerShell window running:

```powershell
powershell -ExecutionPolicy Bypass -File windows\Start-TeleopTunnel.ps1 -ServerAddress '{user}@{teleop-server-ip}'
```

If your working browser tunnel already forwards local 18080 to server 8080, add only the input tunnel instead:

```powershell
powershell -ExecutionPolicy Bypass -File windows\Start-TeleopTunnel.ps1 -ServerAddress '{user}@{teleop-server-ip}' -InputOnly
```

Open http://127.0.0.1:18080 and run the console client in another PowerShell:

```powershell
powershell -ExecutionPolicy Bypass -File windows\Run-Teleop.ps1 -FingerSource none
```

Click the **simulation image in the browser**. Keep the client PowerShell running. The client does not create a separate window; all movement keys are read from the focused browser simulation. No deadman key is required. W/S = forward/back (world +Y/-Y), A/D = left/right (-X/+X), Space/Shift = up/down (+Z/-Z). These are world axes, not camera-relative directions. F1 selects right, F2 selects left, In the collection dashboard, browser F6 restores the entire scene; F7 starts/stops an episode. Success is saved automatically using the task evaluator. The programmatic Home API remains available. Space no longer activates control: it moves upward.

Release a movement key to hold the arm while glove fingers remain active. Switching tabs, clicking outside the simulation image, losing browser focus or losing key heartbeats deactivates input. After reconnect release all movement keys before moving again. The old native-window keyboard mode has been removed. If the video tunnel uses another local port, pass `-BrowserUrl http://127.0.0.1:YOURPORT` to Run-Teleop.

The default command prints startup, connection changes and errors, without periodic RTT output. To display input request/reply RTT once per second:

```powershell
powershell -ExecutionPolicy Bypass -File windows\Run-Teleop.ps1 -FingerSource manus -ShowRtt
```

Example: `[RTT] 18.2 ms | Connected | browser active | right`. Disconnected or stale samples show `--` instead of an old RTT. Printing runs separately from the input sender. Stop with **Ctrl+C** in this client PowerShell; the client disarms input when reachable, stops the haptic watchdog/output, and shuts down the MANUS SDK. Keep the separate SSH tunnel and MANUS Core running if you plan to restart the client. No Python or SDK reinstall is required for this update; update the Windows code/bundle.

Start with one hand and small movements; check that the other hand stays still and orientation stays fixed. The server supplies home poses, workspace limits, control speed, and supported features when the client connects. Key mappings and device preferences are in `configs/client.yaml`; use `-Config path\to\client.yaml` for another client settings file. The same Windows client works with any Linux integration implementing the common protocol. Do not run two control clients simultaneously.

`-FingerSource mock` supplies explicitly synthetic finger data for testing. It is not a glove calibration or recording source.

## 3. Verify the official MANUS SDK first

Use the authorized **3.2.1 Windows SDK** and matching Core 3.2.1. Connect the dongle, start Core, pair/calibrate both gloves and verify tracking in Core. SDK access requires the appropriate MANUS SDK entitlement/license; Core tracking alone does not establish SDK access.

```powershell
powershell -ExecutionPolicy Bypass -File windows\Setup-Teleop.ps1 -ManusPythonPath 'C:\YOUR\MANUS_SDK_3.2.1\Python'
.\.venv\Scripts\python.exe 'C:\YOUR\MANUS_SDK_3.2.1\Python\examples\minimal_client.py'
```

Select **Local mode (2)** in the official example, confirm it connects to local Core and receives actual glove data, then exit that example before running our client. Replace example paths with the actual vendor SDK location. Proprietary SDK/DLLs and license keys are not included in this bundle.

Official references: [Python setup](https://docs.manus-meta.com/3.2.0/Plugins/SDK/Python/getting%20started/) and [3.2.1 SDK client API](https://docs.manus-meta.com/3.2.1/Plugins/SDK/Python/SDK%20Client/).

Next validate our raw skeleton selection independently:

```powershell
.\.venv\Scripts\python.exe -m dex_teleop.devices.manus.sdk_probe --seconds 15 --output manus-probe.json
```

Confirm `canonical_sides_seen` and the 25 selected nodes for the appropriate glove IDs. The probe rejects incomplete/ambiguous topology instead of inventing a mapping. If it fails, retain `manus-probe.json` and the console error; do not treat missing tracking as valid fingers. A raw skeleton may contain more than 25 nodes: semantic node metadata selects the canonical 25.

Finally:

```powershell
powershell -ExecutionPolicy Bypass -File windows\Run-Teleop.ps1 -FingerSource manus
```

Click the simulation image, select the matching hand with F1/F2, and test finger flexion without pressing movement keys before combining keyboard motion. MANUS wrist orientation normalizes finger coordinates only; arm orientation remains the robot's initial home orientation. Retargeting uses the existing Bench2Dex DexPilot implementation.

## 4. Acceptance checklist

- Keyboard moves all three world axes; release stops, browser F6 resets the entire environment and F7 starts/stops collection; no deadman is required.
- The inactive hand stays still; F1/F2 select the intended hand.
- Core/SDK/probe identify actual left/right gloves and keep receiving fresh data.
- Finger flexion follows the glove through the Linux retargeter.
- Keyboard and fingers work simultaneously while the browser updates.
- Disconnect SSH, verify motion holds, reconnect and release/repress the movement keys.

Only after these checks should hardware end-to-end operation be marked verified. Physical grasping has not been verified. Image capture, keyboard state and glove transport use separate paths; keyboard state shares the existing HTTP tunnel but does not depend on JPEG fetches.

## Troubleshooting

- Local port bind failure: choose another `-VideoPort` or `-InputPort`; for input also pass matching `-Endpoint tcp://127.0.0.1:NEWPORT` to Run-Teleop. This is a local Windows port issue, independent of SSH login permissions.
- No keyboard response: update the client files, refresh the browser, click the simulation image, check connected status and select hand. Keep both HTTP and ZeroMQ SSH forwards running. Browser polling defaults to local port 18080.
- SDK import/DLL/license failure: use matching Windows x64 SDK and its supported runtime, then rerun the official Local example. Linux SDK installation does not fix Windows SDK failures.
- Raw topology error: retain the probe output for checking the actual SDK node metadata. Do not replace the mapping with arbitrary joint angles.
- Input RTT is request/reply time measured with one local clock; it is not an estimate of synchronized one-way latency. Server timeout uses its own monotonic clock.
- Task change leaves `ok` and RTT frozen: the 2026-10-21 fix bounds receives and uses a nonblocking read after polling. An older client could remain in `recv()` during a task restart and never reach its reconnect handler. Stop only the control client with Ctrl+C (close its window on older versions), keep MANUS Core/SSH/the Linux simulation running, and update the client. Release movement keys and click the simulation after restarting. The old `ok` text alone does not prove current connectivity. A minimal update preserving all local settings and the installed SDK is:

  ```powershell
  cd "$env:USERPROFILE\dex_teleop_client"
  scp '{user}@{teleop-server-ip}:/path/to/dex-teleop/dex_teleop/core/transport.py' .\dex_teleop\core\transport.py
  powershell -ExecutionPolicy Bypass -File windows\Run-Teleop.ps1 -FingerSource manus
  ```

  Keep your usual `-Endpoint` / `-BrowserUrl` options if using custom tunnel ports. Regression tests exercise real local sockets with a disappearing response and repeated server replacement; Windows MANUS acceptance must still be checked on the actual desktop.

## Updating an existing installation

Close the old control client; keep the SSH tunnels. From PowerShell:

```powershell
cd $env:USERPROFILE
scp '{user}@{teleop-server-ip}:/path/to/dex-teleop/dist/dex_teleop_windows.zip' .
Expand-Archive .\dex_teleop_windows.zip -DestinationPath .\dex_teleop_client -Force
cd .\dex_teleop_client
powershell -ExecutionPolicy Bypass -File windows\Run-Teleop.ps1 -FingerSource manus
```

The archive has no `.venv` or vendor SDK and preserves your installed environment. No SDK reinstall is needed. Reload the browser with Ctrl+F5, then click the image. Later updates can use:

```powershell
powershell -ExecutionPolicy Bypass -File windows\Update-Teleop.ps1 -ServerAddress '{user}@{teleop-server-ip}' -RemoteRoot '/path/to/dex-teleop'
```

The old Z floor was home Z minus 0.15 m, about 1.018 m. It is now 0.78 m, with downward range up to 0.50 m. XY and upward limits are unchanged. This is a palm limit, not a fingertip height: contact, IK reachability and joint limits can still constrain motion.

W/S drive +Y/-Y and A/D drive -X/+X in the shared Z-up, metre-based control frame. The Linux integration maps this frame to its simulator. Workspace limits come from the current server. Restart the client after editing `configs/client.yaml`; the browser picks up the connected client's key mappings automatically. Server changes reset the previous hand state; release movement keys before resuming.

## Contact-driven haptics

The latest client enables contact-based finger vibration by default with `-FingerSource manus`. Keep Core and both SSH forwards running; click the simulation image and select the appropriate hand. The server returns measured PhysX finger forces over the existing ZeroMQ replies. The default maximum SDK command is 0.35; **고급 설정 → 최대 진동** sets it from 0.00 to 1.00 in steps of 0.01. The two force handles map contact force from zero to this selected maximum. This is a normalized command, not a physical force or acceleration measurement. A 250 ms feedback watchdog and SDK output run separately from network requests. Invalid tracking, focus loss, stale physics and connection loss produce zero output.

Use `-Haptics off` to disable vibration, or `-Haptics mock` to inspect decoded output without driving the glove. Actual SDK/device output requires Windows confirmation. Test one finger contact and release first. SDK haptic errors appear in PowerShell; they do not stop finger input. The API retains its last command: forced process/PC/Core termination cannot guarantee zero delivery, so power off/reconnect the glove if vibration persists. Force-to-vibration settings belong to the Linux server profile. Local output limits, feedback timeout, and device update rate are in `configs/client.yaml`, the only config included in this bundle. Output stops while reconnecting or when the server does not support haptics.

Latest workspace/camera: W (+Y) upper range is now +0.35 m from startup home, S lower range stays -0.15 m; camera Z increased from 1.70 to 2.00 m. In the collection dashboard, F6 does not randomly resample the layout or restart the server. It restores the initial scene and invalidates old input with a new calibration epoch. Release F6 and movement keys to resume. Normal simulation step counters continue.

The browser dashboard provides task selection, dataset Explorer, HDF5 collection and haptic settings. The repository's root README summarizes server operation and storage. Haptic feedback has a default-on switch, one two-handle contact-force range slider (default 0.15–3.00 N), and the maximum vibration control under advanced settings. Settings persist on Linux across task changes and restarts. Old intensity preferences migrate to the equivalent maximum command. New episodes include `meta/haptic_max_power` along with the existing haptic metadata.

For adjustable output above 0.35, close the Windows control client and update `dex_teleop/core/haptics.py` or install the current source bundle, then restart the client. Older clients still clamp feedback to 0.35. The updated client accepts the validated `haptics.max_power` field sent with each feedback sample; older servers without this field retain the local default ceiling. No SDK or Python dependency reinstall is required. Restart the Linux simulation and refresh the browser to load the updated UI.

The collection settings also include **이동 범위**: Home-relative XYZ minimum/maximum offsets and the absolute minimum palm Z. Edit while collection is idle, then click **범위 적용**. Release movement keys and refocus the simulation to resume. The server publishes new bounds through the existing status protocol; the current Windows client already supports this and needs no update. **기본값** restores a draft of the original values; Apply saves it.
