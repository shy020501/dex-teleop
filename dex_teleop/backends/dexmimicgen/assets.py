"""Pinned sources and relocatable bundled assets, with verified retry recovery."""
import io
import json
import shutil
import subprocess
import tarfile
import threading
from pathlib import Path
from .paths import SOURCES, ASSET_ROOT
from .task_info import task_info


def _json(path, value):
    temporary = path.with_suffix(path.suffix + '.tmp')
    temporary.write_text(json.dumps(value))
    temporary.replace(path)


def _ensure_source(name, url, revision, root):
    ownership = root.parent / ('.' + root.name + '.dex-teleop-source.json')
    expected = dict(url=url, revision=revision)
    managed = ownership.is_file() and json.loads(ownership.read_text()) == expected
    if not root.exists():
        root.parent.mkdir(parents=True, exist_ok=True)
        if ownership.exists() and not managed:
            raise ValueError('Conflicting source ownership: ' + str(ownership))
        _json(ownership, expected)
        managed = True
        root.mkdir()
    if not (root / '.git').is_dir():
        if not managed or any(root.iterdir()):
            raise ValueError('Refusing unrelated source directory: ' + str(root))
        subprocess.run(['git', 'init', '-q', str(root)], check=True)
    command = ['git', '-C', str(root)]
    repository = subprocess.run(command + ['rev-parse', '--git-dir'], capture_output=True, text=True)
    if repository.returncode:
        if not managed or any(path.name != '.git' for path in root.iterdir()):
            raise ValueError('Unrecognized incomplete repository: ' + str(root))
        subprocess.run(['git', 'init', '-q', str(root)], check=True)
    head = subprocess.run(command + ['rev-parse', '--verify', 'HEAD'], capture_output=True, text=True)
    if head.returncode == 0:
        actual = head.stdout.strip()
        if actual != revision:
            raise ValueError(f'{name}: expected pinned revision {revision}, got {actual}')
        return
    if not managed:
        raise ValueError('Unmanaged incomplete source checkout: ' + str(root))
    origin = subprocess.run(command + ['remote', 'get-url', 'origin'], capture_output=True, text=True)
    if origin.returncode:
        subprocess.run(command + ['remote', 'add', 'origin', url], check=True)
    elif origin.stdout.strip() != url:
        raise ValueError('Conflicting source origin: ' + str(root))
    # No force/reset: any unexpected files in a partial checkout remain protected.
    subprocess.run(command + ['fetch', '--depth', '1', 'origin', revision], check=True)
    subprocess.run(command + ['checkout', '--detach', revision], check=True)


def _asset_files(root, revision, asset_path):
    archive = subprocess.check_output(['git', '-C', str(root), 'archive', f'{revision}:{asset_path}'])
    files = {}
    with tarfile.open(fileobj=io.BytesIO(archive)) as bundle:
        for member in bundle.getmembers():
            path = Path(member.name)
            if path.is_absolute() or '..' in path.parts or not (member.isfile() or member.isdir()):
                raise ValueError('Unsafe upstream asset path')
            if member.isfile():
                files[member.name] = bundle.extractfile(member).read()
    if not files:
        raise ValueError('Pinned source has no bundled assets')
    return files


def _prepare_assets(target, files, revision):
    """Repair only bytes verified against the exact pinned archive.

    An interrupted extraction may leave complete files or a byte prefix. A
    different file, symlink, or unexpected name is preserved and rejected.
    """
    target.mkdir(parents=True, exist_ok=True)
    marker = target / '.dex-teleop-assets.json'
    existing_revision = None
    if marker.is_file():
        try:
            existing_revision = json.loads(marker.read_text()).get('revision')
        except (ValueError, UnicodeDecodeError):
            pass  # A torn marker is recovered only after all bytes verify below.
    if existing_revision is not None and existing_revision != revision:
        raise ValueError('Asset revision differs: ' + str(target))
    partial_suffix = '.dex-teleop-partial'
    for path in target.rglob('*'):
        if path.is_symlink():
            raise ValueError('Unexpected asset symlink: ' + str(path))
        if path.is_dir() or path == marker or path == marker.with_suffix('.json.tmp'):
            continue
        name = path.relative_to(target).as_posix()
        source_name = name[:-len(partial_suffix)] if name.endswith(partial_suffix) else name
        value = path.read_bytes()
        if source_name not in files or not files[source_name].startswith(value):
            raise ValueError('Unverified or modified asset file: ' + str(path))
    for name, value in files.items():
        destination = target / name
        if destination.is_file() and destination.read_bytes() == value:
            continue
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_name(destination.name + partial_suffix)
        temporary.write_bytes(value)
        temporary.replace(destination)
    for path in target.rglob('*' + partial_suffix):
        path.unlink()  # Every such file was checked against its pinned archive entry.
    _json(marker, dict(revision=revision, files=len(files)))


def prepare_sources():
    import fcntl
    ASSET_ROOT.mkdir(parents=True, exist_ok=True)
    with (ASSET_ROOT / '.prepare.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        for name, (url, revision, root, asset_path) in SOURCES.items():
            _ensure_source(name, url, revision, root)
            target = ASSET_ROOT / name
            files = _asset_files(root, revision, asset_path)
            _prepare_assets(target, files, revision)
            local = root / asset_path
            if local.is_symlink():
                if local.resolve() != target.resolve():
                    raise ValueError('Conflicting asset link: ' + str(local))
            elif local.is_dir():
                # Remove only original, byte-identical reproducible source assets.
                for path in local.rglob('*'):
                    if path.is_symlink():
                        raise ValueError('Unexpected source asset link: ' + str(path))
                    if path.is_file():
                        name_in_archive = path.relative_to(local).as_posix()
                        if name_in_archive not in files or path.read_bytes() != files[name_in_archive]:
                            raise ValueError('Locally modified upstream asset: ' + str(path))
                shutil.rmtree(local)
                local.symlink_to(target, target_is_directory=True)
            else:
                local.parent.mkdir(parents=True, exist_ok=True)
                local.symlink_to(target, target_is_directory=True)
    return {name: source[1] for name, source in SOURCES.items()}


class AssetDownloads:
    def __init__(self):
        self.jobs = []
        self.lock = threading.Lock()
        self.thread = None

    def ready(self):
        for name, (_, revision, root, asset_path) in SOURCES.items():
            target = ASSET_ROOT / name
            try:
                marker = json.loads((target / '.dex-teleop-assets.json').read_text())
                if marker.get('revision') != revision or not (root / asset_path).is_dir():
                    return False
                if (root / asset_path).resolve() != target.resolve():
                    return False
            except (OSError, ValueError):
                return False
        return True

    def snapshot(self):
        with self.lock:
            return dict(root=str(ASSET_ROOT), busy=bool(self.thread and self.thread.is_alive()), jobs=[dict(x) for x in self.jobs])

    def start(self, task):
        task_info(task)
        with self.lock:
            if self.thread and self.thread.is_alive():
                raise ValueError('Asset preparation already running')
            job = dict(id=task, task=task, state='downloading', bytes_received=0, bytes_total=0, completed_files=0, discovered_files=0)
            self.jobs.append(job)
            def work():
                try:
                    prepare_sources()
                    with self.lock:
                        job.update(state='complete')
                except Exception as exc:
                    with self.lock:
                        job.update(state='error', error=str(exc))
            self.thread = threading.Thread(target=work, daemon=True)
            self.thread.start()
        return task

    def cancel(self, task):
        raise ValueError('The pinned bundled source download cannot be cancelled mid-checkout')
