# Dex-teleop

https://github.com/user-attachments/assets/9f70417c-ed22-4339-a561-d05528b4329d

Dex-teleop is a platform that connects input devices (e.g., MANUS Metaglove, HTC Vive Tracker) on Windows to dexterous robot simulators running on Linux servers for teleoperation and demonstration data collection.

Designed for integration with different simulators, it currently supports [Bench2Dex](https://github.com/Bench2Dex/Bench2Dex) on [Isaac Sim](https://docs.isaacsim.omniverse.nvidia.com/5.1.0/index.html)/[Isaac Lab](https://isaac-sim.github.io/IsaacLab/). A browser dashboard provides scene selection, camera views, recording, and replay.

- **Arm control:** HTC Vive Tracker position and orientation tracking, with keyboard control also available.
- **Finger control:** Map MANUS Metaglove Pro Haptic input to robot hands using DexPilot.
- **Haptics:** Convert simulated finger contact forces into glove vibrations.
- **Recording:** Store RGB images from the main and both wrist cameras, robot states and commands, and object states in HDF5.

## 📡 Communication

| Purpose | Protocol | Linux server | Windows client |
| --- | --- | --- | --- |
| Video, dashboard, browser key input | HTTP API / latest JPEG frame polling | `127.0.0.1:8080` | `127.0.0.1:18080` |
| Control input, status, haptic feedback | ZeroMQ over TCP, REQ/REP | `127.0.0.1:5557` | `127.0.0.1:15557` |

Connections use SSH tunnels. The **Windows control client** is also required for browser keyboard control.

## 📢 Updates

- (2026.10.27) Initial release with support for MANUS gloves, keyboard input, and the Bench2Dex simulator.

## 📋 Requirements

- **Linux Server:**
  - **Bench2Dex:** Ubuntu 22.04/24.04 x86_64, glibc ≥ 2.35, NVIDIA RTX GPU with a compatible driver, Conda, Git. Runtime: Python 3.11, Isaac Sim 5.1, Isaac Lab v2.3.2, PyTorch 2.7.0 + CUDA 12.8. [GPU requirements](https://docs.isaacsim.omniverse.nvidia.com/5.1.0/installation/requirements.html)
- **Windows:** Python 3.11 x64, PowerShell, OpenSSH, MANUS Core and Windows SDK 3.2.1.

## 📦 Installation

### 1. Clone the repository

```bash
git clone https://github.com/shy020501/dex-teleop
cd dex-teleop
```

### 2. Storage paths (Optional)

Set shared storage roots for all simulators. To use an external HDD or SSD, provide absolute paths; each variable is optional.

```bash
DATASET_DIR="/mnt/storage/data" \
ASSET_DIR="/mnt/storage/assets" \
./scripts/setup_storage.sh
```

| Optional variable | Project path | Purpose |
| --- | --- | --- |
| `DATASET_DIR` | `data/` | Shared root for recorded data |
| `ASSET_DIR` | `assets/` | Shared root for simulation assets |

Skip this step to use local `data/` and `assets/` directories. External roots are saved as symlinks, so these variables are not needed again. Existing data and conflicting links are not overwritten.

### 3. Bench2Dex setup

Each simulator uses its own Conda environment, with shared teleoperation dependencies added to its simulation requirements.

```bash
conda env create -f environments/bench2dex.yml
conda activate dex-teleop-bench2dex
bash scripts/setup_bench2dex.sh
```

This creates `bench2dex/` under each storage root: for example, `DATASET_DIR=/mnt/storage/data` gives `/mnt/storage/data/bench2dex`. Additional simulator integrations can create their own subdirectories under the same roots.

The script installs Bench2Dex sources and patches, Isaac Sim/Isaac Lab, and [shared teleoperation dependencies](requirements/teleop.txt), then downloads the USD files, textures, and URDFs for the default scene, `86_short_jigsaw_puzzle`. Versions are pinned in [sources.lock.json](configs/sources.lock.json) and [constraints-bench2dex.txt](requirements/constraints-bench2dex.txt).

## 🚀 Quick start

Replace `{user}@{teleop-server-ip}` with your SSH login and server address, and `/path/to/dex-teleop` with the project path on the server.

### 1. Linux server

From the project directory with the `dex-teleop-bench2dex` Conda environment active:

```bash
./scripts/run_teleop.sh
```

The default run has no step limit and restores the last selected scene (initial default: `86_short_jigsaw_puzzle`). To override settings:

```bash
# Choose a GPU and timing window.
DEX_TELEOP_GPU_INDEX=1 TIMING_WINDOW=60 ./scripts/run_teleop.sh

# Choose a scene and set a simulation step limit.
./scripts/run_teleop.sh --task 86_short_jigsaw_puzzle --steps 3600

# Select a Linux server profile from configs/ (or use a full config path).
./scripts/run_teleop.sh --config bench2dex.yaml
```

`--steps N` sets the simulation step limit; `--steps 0` (default) disables it. Environment variables and command-line options can be combined. See [Runtime configuration](#-runtime-configuration) for more settings. Use `./scripts/run_browser.sh` for viewing only.

### 2. Windows setup and SSH tunnel

```powershell
scp '{user}@{teleop-server-ip}:/path/to/dex-teleop/dist/dex_teleop_windows.zip' .
Expand-Archive .\dex_teleop_windows.zip -DestinationPath .\dex_teleop_client
cd .\dex_teleop_client
powershell -ExecutionPolicy Bypass -File windows\Setup-Teleop.ps1
powershell -ExecutionPolicy Bypass -File windows\Start-TeleopTunnel.ps1 -ServerAddress '{user}@{teleop-server-ip}'
```

### 3. Browser and control client

Keep the tunnel open, then run the following from `dex_teleop_client` in a new PowerShell window.

```powershell
Start-Process 'http://127.0.0.1:18080/'
powershell -ExecutionPolicy Bypass -File windows\Run-Teleop.ps1
```

Click the simulation image to enable control. Add `-FingerSource none` if you are not using gloves. See the [Windows guide](windows/README.md) for SDK setup, calibration, SSH options, and updates.

The client runs directly in PowerShell without opening a separate window. Add `-ShowRtt` to print round-trip time (RTT) once per second.

## 🎮 Keyboard controls and recording

| Key | Action |
| --- | --- |
| `F1` / `F2` | Select right / left hand |
| `W` / `S` | Move along world `+Y` / `-Y` |
| `A` / `D` | Move along world `-X` / `+X` |
| `Space` / `Shift` | Move along world `+Z` / `-Z` |
| `F6` | Reset the scene. If recording, save as a failed episode before resetting |
| `F7` | Reset and start recording / if recording, save as a failed episode and stop |

Controls are disabled when focus is lost. Successful tasks are saved automatically, with a limit of 1,800 frames per episode.
Configure movement limits, recording folders, and haptics in the dashboard.

## 🔧 Runtime configuration

Set environment variables before the launch command. Legacy `BENCH2DEX_*` variables are also supported; `DEX_TELEOP_*` takes precedence.

| Environment variable | Default | Description |
| --- | --- | --- |
| `DEX_TELEOP_GPU_INDEX` | `0` | Physical GPU index |
| `TIMING_WINDOW` | `120` | Number of recent steps used for timing averages; positive integer |
| `DEX_TELEOP_EXECUTION_MODE` | `split` | `split`: separate physics/control and rendering processes; `serial`: single process |
| `DEBUG_MODE` | `false` | `true`: preview the initial static scene |
| `DEX_TELEOP_PYTHON` | `python` | Python executable for the Isaac environment |
| `DEX_TELEOP_BROWSER_FPS` | `24` | Browser video generation rate limit, 1–30 |
| `DEX_TELEOP_BROWSER_QUALITY` | `75` | JPEG quality, 30–95 |

Physics runs at 60 Hz and control/recording at 20 Hz in simulation time. The UI shows the mean simulation timestep, wall-clock period, and real-time factor over the last N steps.

`split` runs two Isaac processes on the same GPU and uses more GPU memory. GPU functionality and performance still require separate validation.

Linux launchers select the simulator from a server profile, defaulting to [configs/bench2dex.yaml](configs/bench2dex.yaml). Use `--config bench2dex.yaml` or a custom config path. Activate the selected simulator's Conda environment before launching. Camera and recording settings are in [configs/bench2dex/dashboard_collect.yaml](configs/bench2dex/dashboard_collect.yaml).

Windows uses [configs/client.yaml](configs/client.yaml) for connection, device, key, and local haptic settings. It receives control speed, available hands, poses, workspace limits, and haptic support from the server. Switching compatible simulators requires no Windows config or command changes when the connection endpoints stay the same; release movement keys before resuming after reconnecting. Each additional simulator needs a Linux integration that implements the common teleoperation protocol; Bench2Dex is currently the available integration.
