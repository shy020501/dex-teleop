"""Verified, task-scoped downloads from the official pinned POMDAR revision."""
import copy
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import queue
import threading
import time
import uuid
import xml.etree.ElementTree as ET
from urllib.parse import quote
from urllib.request import Request, urlopen

from .paths import ASSET_ROOT, SOURCE_ROOT, SOURCE_REPOSITORY, SOURCE_REVISION
from .task_info import TASKS, task_info


def safe_relative(value):
    if not isinstance(value, str) or not value or value.startswith('/') or '\\' in value or ':' in value:
        raise ValueError('Invalid POMDAR repository path')
    if any(part in ('', '.', '..') for part in value.split('/')) or any(ord(c) < 32 for c in value):
        raise ValueError('Invalid POMDAR repository path')
    return value


def destination(root, relative):
    root = Path(root)
    path = root/safe_relative(relative)
    cursor = root
    for part in PurePosixPath(relative).parts:
        cursor = cursor/part
        if cursor.is_symlink():
            raise ValueError('Unexpected POMDAR file symlink: '+str(cursor))
    if not path.resolve().is_relative_to(root.resolve()):
        raise ValueError('POMDAR file escapes its storage root')
    return path


def asset_path(reference):
    reference = safe_relative(reference)
    # Official XML uses meshdir="assets" with paths relative to sim/assets.
    return 'sim/assets/'+reference


def dependency_paths(task, source_root=SOURCE_ROOT):
    fragment = task_info(task)['fragment']
    roots = ['sim/hand/orca_v1.xml', f'sim/tasks/{fragment}.xml']
    result = {'sim/assets/table_texture.png'}
    for relative in roots:
        path = destination(source_root, relative)
        for element in ET.parse(path).getroot().iter():
            reference = element.get('file')
            if not reference:
                continue
            if element.tag not in ('mesh', 'texture', 'hfield', 'skin'):
                raise ValueError('Unexpected external reference in POMDAR XML: '+element.tag)
            result.add(asset_path(reference))
    return sorted(result)


class RemoteRepository:
    revision = SOURCE_REVISION

    def __init__(self):
        url = f'https://api.github.com/repos/{SOURCE_REPOSITORY}/git/trees/{self.revision}?recursive=1'
        with urlopen(Request(url, headers={'User-Agent':'dex-teleop-pomdar'}), timeout=30) as response:
            tree = json.load(response)
        if tree.get('truncated') or tree.get('sha') != self.revision:
            raise RuntimeError('POMDAR repository inventory is incomplete or has an unexpected revision')
        self.entries = {item['path']:item for item in tree['tree'] if item['type'] == 'blob' and item['mode'] != '120000'}

    def info(self, relative):
        item = self.entries.get(safe_relative(relative))
        if item is None:
            raise FileNotFoundError('Missing pinned POMDAR file: '+relative)
        return dict(path=relative, size=int(item['size']), git_sha1=item['sha'])

    def open(self, relative):
        url = f'https://raw.githubusercontent.com/{SOURCE_REPOSITORY}/{self.revision}/'+quote(safe_relative(relative), safe='/')
        return urlopen(Request(url, headers={'User-Agent':'dex-teleop-pomdar'}), timeout=30)


class Cancelled(Exception):
    pass


def fetch_file(root, remote, relative, cancel=None, progress=None):
    info = remote.info(relative)
    if info['size'] < 0 or info['size'] > 128*1024*1024:
        raise ValueError('POMDAR file exceeds the 128 MiB per-file budget')
    path = destination(root, relative)
    def check():
        if cancel is not None and cancel.is_set():
            raise Cancelled()
    def digest(stream, output=None):
        sha = hashlib.sha1(f'blob {info["size"]}\0'.encode())
        count = 0
        while True:
            check()
            block = stream.read(1024*1024)
            if not block: break
            count += len(block)
            if count > info['size']: raise RuntimeError('Oversized POMDAR download')
            sha.update(block)
            if output is not None:
                output.write(block)
                if progress: progress(len(block))
        if count != info['size'] or sha.hexdigest() != info['git_sha1']:
            raise RuntimeError('POMDAR file hash/size mismatch: '+relative)
    check()
    if path.exists():
        with path.open('rb') as stream: digest(stream)
        return info
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name('.'+path.name+'.'+uuid.uuid4().hex+'.part')
    try:
        with remote.open(relative) as stream, temporary.open('wb') as output:
            digest(stream, output)
            output.flush(); os.fsync(output.fileno())
        check()
        destination(root, relative)
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)
    return info


def prepare_sources(source_root=SOURCE_ROOT, remote=None):
    remote = remote or RemoteRepository()
    selected = [path for path in remote.entries if
                (path.startswith('teleop/') and PurePosixPath(path).suffix in ('.py', '.xml', '.yaml')
                 and '/assets/' not in path) or
                (path.startswith('sim/tasks/') and path.endswith('.xml')) or
                path in ('README.md', 'requirements.txt', 'requirements-teleop.txt',
                         'sim/hand/orca_v1.xml', 'sim/launch_mujoco_orca.py')]
    if not selected or sum(remote.info(path)['size'] for path in selected) > 5*1024*1024:
        raise RuntimeError('Unexpected POMDAR source bundle size')
    files = [fetch_file(source_root, remote, path) for path in sorted(selected)]
    manifest = destination(source_root, '.dex-teleop-source.json')
    manifest.write_text(json.dumps(dict(revision=remote.revision, files=files), indent=2)+'\n')
    return files


class AssetDownloads:
    def __init__(self, root=ASSET_ROOT, source_root=SOURCE_ROOT, remote_factory=RemoteRepository):
        self.root, self.source_root = Path(root), Path(source_root)
        self.remote_factory = remote_factory
        self.lock = threading.RLock()
        self.jobs, self.cancels = {}, {}
        self.pending = queue.Queue()
        self.worker = None

    def _manifest_path(self, task):
        task_info(task)
        return destination(self.root, '.pomdar/tasks/'+task+'.json')

    def _manifest(self, task, payload):
        path = self._manifest_path(task)
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(path.name+'.'+uuid.uuid4().hex+'.tmp')
        try:
            temporary.write_text(json.dumps(payload, indent=2)+'\n')
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)

    def readiness(self, task):
        task_info(task)
        try:
            required = dependency_paths(task, self.source_root)
            missing = sum(not destination(self.root, path).is_file() for path in required)
            manifest = json.loads(self._manifest_path(task).read_text())
            if not manifest.get('complete') or manifest.get('revision') != SOURCE_REVISION:
                return False, max(1, missing), 'Task assets have not been verified'
            records = {item['path']:item for item in manifest.get('files', [])}
            for relative in required:
                info = records.get(relative)
                path = destination(self.root, relative)
                if not info or not path.is_file() or path.stat().st_size != info['size']:
                    return False, max(1, missing), 'Missing task asset'
            return True, 0, None
        except FileNotFoundError:
            return False, 1, None
        except (OSError, ValueError, KeyError, ET.ParseError):
            return False, 1, 'Invalid task source or asset manifest'

    def snapshot(self):
        with self.lock:
            return dict(root=str(self.root), busy=any(j['state'] in ('queued', 'downloading') for j in self.jobs.values()),
                        jobs=copy.deepcopy(list(self.jobs.values())))

    def _update(self, task, **fields):
        with self.lock: self.jobs[task].update(fields)

    def _new_job(self, task):
        task_info(task)
        self.root.mkdir(parents=True, exist_ok=True)
        if not os.access(self.root, os.W_OK | os.X_OK):
            raise ValueError('POMDAR asset root is not writable: '+str(self.root))
        identity = uuid.uuid4().hex
        self.jobs[task] = dict(id=identity, task=task, state='queued', bytes_received=0, bytes_total=0,
                               completed_files=0, discovered_files=0, current_file='', error=None)
        self.cancels[task] = threading.Event()
        return identity

    def start(self, task):
        with self.lock:
            if task in self.jobs and self.jobs[task]['state'] in ('queued', 'downloading'):
                return self.jobs[task]['id']
            identity = self._new_job(task)
            self.pending.put(task)
            if self.worker is None:
                self.worker = threading.Thread(target=self._work, daemon=True, name='pomdar-assets')
                self.worker.start()
            return identity

    def cancel(self, task):
        task_info(task)
        with self.lock:
            if task not in self.cancels: raise ValueError('No asset download for this task')
            self.cancels[task].set()

    def download_sync(self, task):
        with self.lock: self._new_job(task)
        self._download(task, self.cancels[task])
        return copy.deepcopy(self.jobs[task])

    def _work(self):
        while True:
            task = self.pending.get()
            try: self._download(task, self.cancels[task])
            except Cancelled: self._update(task, state='cancelled', current_file='')
            except Exception as exc: self._update(task, state='error', error=str(exc), current_file='')
            finally: self.pending.task_done()

    def _download(self, task, cancel):
        self._update(task, state='downloading')
        self._manifest(task, dict(complete=False, revision=SOURCE_REVISION))
        remote = self.remote_factory()
        required = dependency_paths(task, self.source_root)
        files = [remote.info(path) for path in required]
        total = sum(item['size'] for item in files)
        if total > 128*1024*1024:
            raise ValueError('POMDAR task exceeds the 128 MiB download budget')
        self._update(task, bytes_total=total, discovered_files=len(files), revision=remote.revision)
        for item in files:
            self._update(task, current_file=item['path'])
            def progress(count):
                with self.lock: self.jobs[task]['bytes_received'] += count
            fetch_file(self.root, remote, item['path'], cancel, progress)
            with self.lock: self.jobs[task]['completed_files'] += 1
        if cancel.is_set(): raise Cancelled()
        self._manifest(task, dict(complete=True, revision=remote.revision, files=files))
        self._update(task, state='complete', current_file='', finished_at=time.time())
