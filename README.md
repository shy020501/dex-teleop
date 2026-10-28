# Dex-teleop

https://github.com/user-attachments/assets/29a96f0c-2265-41fd-8486-cf33e6efd5af

Dex-teleop is a platform that connects input devices (e.g., MANUS Metaglove, HTC Vive Tracker) on Windows to dexterous robot simulators running on Linux servers for teleoperation and demonstration data collection.

- **Input:** MANUS Metaglove Pro Haptic finger tracking and keyboard hand translation with fixed orientation. HTC Vive Tracker reception and calibration are not yet implemented.
- **Haptics:** Convert simulated finger contact forces into glove vibrations.
- **Recording:** Save camera images, robot and task states, commands, and episode metadata in HDF5.
- **Dashboard:** Select tasks, download assets, view cameras, adjust controls, collect episodes, and inspect recordings and replays.

## 📢 Updates

- (2026.10.28) Added DexJoCo task environments with shared teleoperation, recording, and replay.
- (2026.10.28) Added DexMimicGen task environments with shared teleoperation, recording, and replay.
- (2026.10.27) Added POMDAR on MuJoCo with ORCA hand control, haptics, recording, and comparison replay.
- (2026.10.27) Initial release with support for MANUS gloves, keyboard input, and the Bench2Dex simulator.

## 🧩 Simulations

A shared Windows client and browser dashboard work across supported simulators, while each simulator handles its robot control and finger retargeting.

| Simulation | Engine | Robot control | Finger retargeting |
| --- | --- | --- | --- |
| [Bench2Dex](https://github.com/Bench2Dex/Bench2Dex) | [Isaac Sim](https://docs.isaacsim.omniverse.nvidia.com/5.1.0/index.html) / [Isaac Lab](https://isaac-sim.github.io/IsaacLab/) | Two arms with dexterous hands; arm inverse kinematics | DexPilot |
| [POMDAR](https://github.com/srl-ethz/pomdar_benchmark) | [MuJoCo](https://mujoco.org/) | Single floating right ORCA hand, controlled through a MuJoCo mocap target | POMDAR ORCA retargeter |
| [DexJoCo](https://dexjoco.github.io/) | [MuJoCo](https://mujoco.org/) | Single or dual Panda arms with Allegro hands; native operational-space controller | Pretrained GeoRT networks with MANUS input |
| [DexMimicGen](https://dexmimicgen.github.io/) | [MuJoCo](https://mujoco.org/) / [robosuite](https://robosuite.ai/) | Task-specific dual Panda or GR1 robots; native arm controllers | MANUS joint-angle mapping for coupled hands; pinch input for parallel grippers |

## 📡 Communication

Connections use SSH tunnels. Keep the **Windows control client** running during teleoperation: it handles glove input, HTC Vive Tracker pose input, browser keyboard input, and haptic output. The same client and connection settings work across supported simulators.

| Purpose | Protocol | Linux server | Windows client |
| --- | --- | --- | --- |
| Video, dashboard, browser key input | HTTP API / latest JPEG frame polling | `127.0.0.1:8080` | `127.0.0.1:18080` |
| Control input, status, haptic feedback | ZeroMQ over TCP, REQ/REP | `127.0.0.1:5557` | `127.0.0.1:15557` |

## 📋 Requirements

- **Linux Server:** Conda and Git, plus the requirements for your chosen simulator:
  - **Bench2Dex:** Ubuntu 22.04/24.04 x86_64, glibc ≥ 2.35, NVIDIA RTX GPU with a compatible driver, Python 3.11, Isaac Sim 5.1, Isaac Lab v2.3.2, PyTorch 2.7.0 + CUDA 12.8. [GPU requirements](https://docs.isaacsim.omniverse.nvidia.com/5.1.0/installation/requirements.html)
  - **POMDAR:** Linux, NVIDIA GPU and driver with EGL support for headless rendering, Python 3.11, MuJoCo 3.3.7, CPU PyTorch 2.7.0.
  - **DexJoCo:** Linux, NVIDIA GPU and driver with EGL support, Python 3.11, MuJoCo 3.4.0, Gymnasium 1.0.0, CPU PyTorch 2.7.0.
  - **DexMimicGen:** Linux, NVIDIA GPU and driver with EGL support, Python 3.10, MuJoCo 3.3.7, robosuite 1.5.2.
- **Windows:** Python 3.11 x64, PowerShell, OpenSSH; MANUS Core and Windows SDK 3.2.1 for glove input and haptics.

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

Setup creates `data/<simulator>/` and `assets/<simulator>/`, using the selected profile's simulator name. For example, `DATASET_DIR=/mnt/storage/data` places DexJoCo recordings under `/mnt/storage/data/dexjoco`.

### 3. Simulator setup

Install the simulators you want to use. Each has a separate Conda environment containing its simulation and teleoperation dependencies.

#### Bench2Dex

```bash
conda env create -f environments/bench2dex.yml
conda activate bench2dex
bash scripts/setup_bench2dex.sh
```

Installs Bench2Dex sources and patches, Isaac Sim/Isaac Lab, and teleoperation dependencies, then downloads assets for the default task, `86_short_jigsaw_puzzle`. Versions are pinned in [sources.lock.json](configs/sources.lock.json) and [constraints-bench2dex.txt](requirements/constraints-bench2dex.txt).

#### POMDAR

```bash
conda env create -f environments/pomdar.yml
conda activate pomdar
bash scripts/setup_pomdar.sh
```

Installs MuJoCo, CPU PyTorch, and teleoperation dependencies in the active `pomdar` environment, then fetches pinned POMDAR sources and the ORCA/selected-task assets. The default task is `V1_Wheel`. See [requirements/pomdar.txt](requirements/pomdar.txt) for Python package dependencies.

#### DexJoCo

```bash
conda env create -f environments/dexjoco.yml
conda activate dexjoco
bash scripts/setup_dexjoco.sh
```

Installs the pinned [DexJoCo source](https://github.com/brave-eai/dexjoco), task assets, pretrained GeoRT hand-retargeting networks, and teleoperation dependencies. The default task is `click_mouse`; the catalog contains 11 single-arm and bimanual tasks. See [requirements/dexjoco.txt](requirements/dexjoco.txt) for Python package dependencies.

#### DexMimicGen

```bash
conda env create -f environments/dexmimicgen.yml
conda activate dexmimicgen
bash scripts/setup_dexmimicgen.sh
```

Installs the pinned [DexMimicGen task environments](https://github.com/NVlabs/dexmimicgen), robosuite, robot/task assets, and teleoperation dependencies. The default task is `TwoArmLiftTray`, using two Panda arms with dexterous hands. The nine-task catalog also includes GR1 hands and three tasks with Panda parallel grippers. See [requirements/dexmimicgen.txt](requirements/dexmimicgen.txt) for Python package dependencies.

## 🚀 Quick start

Replace `{user}@{teleop-server-ip}` with your SSH login and server address, and `/path/to/dex-teleop` with the project path on the server.

### 1. Linux server

From the project directory, activate the environment for the simulator you want to run.

**Bench2Dex:**

```bash
conda activate bench2dex
./scripts/run_teleop.sh --config bench2dex.yaml
```

**POMDAR:**

```bash
conda activate pomdar
./scripts/run_teleop.sh --config pomdar.yaml
```

**DexJoCo:**

```bash
conda activate dexjoco
./scripts/run_teleop.sh --config dexjoco.yaml
```

**DexMimicGen:**

```bash
conda activate dexmimicgen
./scripts/run_teleop.sh --config dexmimicgen.yaml
```

Omitting `--config` selects Bench2Dex. Each simulator restores its last selected task and runs without a step limit by default. Stop the current server before switching simulators on the same ports.

To override settings, add environment variables or command-line options:

```bash
# Choose a GPU and timing window.
DEX_TELEOP_GPU_INDEX=1 TIMING_WINDOW=60 ./scripts/run_teleop.sh --config pomdar.yaml

# Choose a task and set a simulation step limit.
./scripts/run_teleop.sh --config pomdar.yaml --task V1_Wheel --steps 5000
```

All integrations accept `--task` and `--steps`. `--steps N` limits physics steps; `--steps 0` disables the limit. `--config` accepts a filename in `configs/` or a full path. Use `./scripts/run_browser.sh --config pomdar.yaml` for viewing only, substituting the profile as needed. See [Runtime configuration](#-runtime-configuration) for more settings.

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
| `F1` / `F2` | Select right / left hand when available; POMDAR supports only the right hand |
| `W` / `S` | Move along world `+Y` / `-Y` |
| `A` / `D` | Move along world `-X` / `+X` |
| `Space` / `Shift` | Move along world `+Z` / `-Z` |
| `F6` | Reset the scene. If recording, save as a failed episode before resetting |
| `F7` | Reset and start recording / stop recording (see outcome handling below) |

Controls are disabled when focus is lost. Configure movement limits, recording folders, and haptics in the dashboard. Episode outcomes depend on the simulator:

- **Bench2Dex:** Tasks with automatic success evaluation finish and save on success. Manual stops and resets save as failure.
- **DexJoCo / DexMimicGen:** Use the selected task's upstream success evaluator and save automatically on success. Manual stops, resets, interruptions, and frame limits save as failure.
- **POMDAR:** Press `F7` to stop recording, then select **Success** or **Failure** in the popup. The scene and recording pause at the stop frame until you choose; the episode is then saved and the scene resets. Reloading the browser reopens the pending choice. Resets, interruptions (including server shutdown while waiting for a choice), and the frame limit save as failure. Outcomes are operator judgments; automatic success detection and official POMDAR benchmark scoring are not implemented.

### Recorded data and replay

Recordings are saved under `data/<simulator>/` in the dashboard's selected subfolder. Open an episode in the **Data** tab to inspect its metadata and generate a replay comparing recorded images, restored states, and simulated actions. Control and recording pause during replay generation.

State and action layouts depend on the simulator. MuJoCo integrations preserve generalized positions/velocities, actuator commands, full integration states, and control changes between sampled frames. Simulator-specific hand-pose or policy targets are stored separately. Inspect each episode's metadata for its dimensions and control units.

## 🔧 Runtime configuration

Set environment variables before the launch command. Activate the chosen simulator's Conda environment in each new terminal.

| Environment variable | Default | Description |
| --- | --- | --- |
| `DEX_TELEOP_GPU_INDEX` | `0` | Physical GPU index; selects EGL rendering for MuJoCo integrations |
| `TIMING_WINDOW` | `120` | Number of recent steps used for timing averages; positive integer |
| `DEX_TELEOP_EXECUTION_MODE` | `split` | Bench2Dex only: separate physics/control and rendering processes, or `serial` for one process |
| `DEBUG_MODE` | `false` | `true`: preview the initial static scene |
| `DEX_TELEOP_PYTHON` | `python` | Python executable used by the launcher |
| `DEX_TELEOP_POMDAR_PYTHON` | Launcher's interpreter | Optional POMDAR runtime executable override |
| `DEX_TELEOP_DEXJOCO_PYTHON` | Launcher's interpreter | Optional DexJoCo runtime executable override |
| `DEX_TELEOP_DEXMIMICGEN_PYTHON` | Launcher's interpreter | Optional DexMimicGen runtime executable override |
| `DEX_TELEOP_BROWSER_FPS` | `24` | Bench2Dex only: browser video generation rate limit, 1–30 |
| `DEX_TELEOP_BROWSER_QUALITY` | `75` | Bench2Dex only: JPEG quality, 30–95 |

| Default setting | Bench2Dex | POMDAR | DexJoCo | DexMimicGen |
| --- | --- | --- | --- | --- |
| Physics rate | 60 Hz | 500 Hz | 500 Hz | 500 Hz |
| Control rate | 20 Hz | 60 Hz | 50 Hz | 20 Hz |
| Recording rate | 20 Hz | 20 Hz | 25 Hz | 20 Hz |
| Cameras | Main + two wrists | Main + right wrist | Main + one or two wrists | Main; two wrists on Panda tasks |

Rates are in simulation time. The timing overlay shows the average simulation timestep, wall-clock period, and real-time factor over `TIMING_WINDOW` steps.

- **Bench2Dex:** Control and haptics are configured in [configs/bench2dex.yaml](configs/bench2dex.yaml); cameras and recording in [dashboard_collect.yaml](configs/bench2dex/dashboard_collect.yaml). `split` runs two Isaac processes on the same GPU and uses more GPU memory. The launcher also accepts legacy `BENCH2DEX_*` aliases, with `DEX_TELEOP_*` taking precedence.
- **POMDAR:** [configs/pomdar.yaml](configs/pomdar.yaml) configures control, retargeting, haptics, simulation rates, cameras, JPEG quality, and the 1,800-frame episode limit. Preview defaults to 20 Hz. Physics and retargeting run on the CPU; the selected GPU renders cameras through EGL.
- **DexJoCo:** [configs/dexjoco.yaml](configs/dexjoco.yaml) configures control, haptics, simulation rates, cameras, and recording. The native arm controller and GeoRT networks run on the CPU; camera rendering uses EGL. Recording and preview default to 25 Hz, aligned with the 50 Hz control loop.
- **DexMimicGen:** [configs/dexmimicgen.yaml](configs/dexmimicgen.yaml) configures control, haptics, simulation rates, cameras, and recording. Robot and hand types follow the selected upstream task. Physics, retargeting, and arm control run on the CPU; camera rendering uses EGL.

Windows uses [configs/client.yaml](configs/client.yaml) for connection, device, key, and local haptic settings. Control speed, available hands, poses, workspace limits, and haptic support come from the Linux server. Switching compatible simulators requires no Windows configuration or command changes when the endpoints stay the same; release movement keys before resuming after reconnecting.
