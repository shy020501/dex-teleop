"""Task-scoped, pinned-revision asset downloads. Never starts Isaac/Kit or fetches the whole dataset."""
import copy
import hashlib
import json
import os
import posixpath
import queue
import re
import shutil
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path, PurePosixPath
from urllib.parse import quote
from urllib.request import urlopen

from dex_teleop.paths import WORKSPACE
from .paths import ASSET_ROOT, LEGACY_ASSET_ROOT, SOURCE_ROOT
REPO='Bench2Dex/Assets'
COMMON=['Robots_p/ur5+wuji/usd/Multi_UR5_wuji_with_flange.usd',
        *['URDF-Zoo/ur5+wuji/urdf/'+n for n in ('Right_Hand.urdf','Left_Hand.urdf','Multi_UR5_wuji_with_flange.urdf')]]
BUILTIN_MDL={'OmniPBR.mdl','OmniGlass.mdl','gltf/pbr.mdl'}


def safe_relative(value):
    if not isinstance(value,str) or not value or '\\' in value or ':' in value or value.startswith('/'):
        raise ValueError('Invalid repository asset path')
    if any(p in ('','.','..') for p in value.split('/')) or any(ord(c)<32 for c in value):
        raise ValueError('Invalid repository asset path')
    if PurePosixPath(value).parts[0] not in ('Objects','Robots_p','URDF-Zoo'):
        raise ValueError('Asset path is outside the allowed repository folders: '+value)
    return value


def destination(root,relative):
    root=Path(root);relative=safe_relative(relative)
    path=root/relative
    cursor=root
    for part in PurePosixPath(relative).parts:
        cursor=cursor/part
        if cursor.is_symlink():raise ValueError('Unexpected asset symlink: '+str(cursor))
    if not path.resolve().is_relative_to(root.resolve()):raise ValueError('Asset path escapes storage root')
    return path


def scene_entries(file,legacy_root=None):
    import yaml
    legacy_root=Path(legacy_root or LEGACY_ASSET_ROOT).resolve()
    spec=yaml.safe_load(Path(file).read_text())
    entries=[]
    for asset in (spec.get('assets') or {}).values():
        if asset.get('path'):
            path=(Path(file).parent/asset['path']).resolve()
            if not path.is_relative_to(legacy_root):raise ValueError('Scene asset is outside the shared asset root')
            entries.append(safe_relative(path.relative_to(legacy_root).as_posix()))
    return list(dict.fromkeys(entries+COMMON))


def usd_dependencies(path):
    path=Path(path)
    if path.suffix.lower() not in ('.usd','.usda','.usdc'):return []
    site=Path(sys.prefix)/f'lib/python{sys.version_info.major}.{sys.version_info.minor}/site-packages/isaacsim'
    candidates=list((site/'extscache').glob('omni.usd.libs-*'))
    if not candidates:raise RuntimeError('Isaac Sim USD libraries not found; dependency validation cannot be skipped')
    usd=candidates[0]
    env=dict(os.environ)
    env['PYTHONPATH']=str(usd)
    env['LD_LIBRARY_PATH']=os.pathsep.join([str(usd/'bin'),str(usd/'bin/deps'),str(Path(sys.prefix)/'lib'),env.get('LD_LIBRARY_PATH','')])
    process=subprocess.run([sys.executable,str(WORKSPACE/'scripts/bench2dex/usd_asset_dependencies.py'),str(path)],
                           env=env,capture_output=True,text=True,timeout=45)
    if process.returncode:raise RuntimeError('USD dependency scan failed: '+process.stderr[-1500:])
    return json.loads(process.stdout)


class RemoteRepository:
    def __init__(self,revision=None):
        from huggingface_hub import HfApi
        self.api=HfApi()
        self.revision=self.api.dataset_info(REPO,revision=revision).sha
        if not re.fullmatch(r'[a-f0-9]{40}',self.revision):raise RuntimeError('Unexpected repository revision')

    def info(self,path):
        from huggingface_hub.hf_api import RepoFile
        path=safe_relative(path)
        items=self.api.get_paths_info(REPO,[path],revision=self.revision,repo_type='dataset')
        item=next((x for x in items if isinstance(x,RepoFile) and x.path==path),None)
        if item is None:raise FileNotFoundError('Remote asset not found: '+path)
        lfs=item.lfs
        return dict(path=path,size=item.size,sha256=getattr(lfs,'sha256',None) if lfs else None,
                    git_sha1=item.blob_id if not lfs else None)

    def open(self,path):
        url='https://huggingface.co/datasets/'+REPO+'/resolve/'+self.revision+'/'+quote(safe_relative(path),safe='/')
        return urlopen(url,timeout=20)

    def expand(self,path):
        # USD tiled textures are individual files in the same pinned revision.
        parent,name=posixpath.split(path)
        expression=re.compile('^'+re.escape(name).replace(re.escape('<UDIM>'),r'\d{4}')+'$')
        matches=[x.path for x in self.api.list_repo_tree(REPO,path_in_repo=parent,revision=self.revision,
                 repo_type='dataset',recursive=False) if expression.fullmatch(posixpath.basename(x.path))]
        if not matches:raise FileNotFoundError('No UDIM texture tiles: '+path)
        return matches


class Cancelled(Exception):pass


class AssetDownloads:
    def __init__(self,root=ASSET_ROOT,scenes=None,remote_factory=RemoteRepository,scanner=usd_dependencies):
        self.root=Path(root)
        self.scenes=Path(scenes or SOURCE_ROOT/'scenes')
        self.remote_factory,self.scanner=remote_factory,scanner
        self.lock=threading.RLock()
        self.jobs={}
        self.cancels={}
        self.pending=queue.Queue()
        self.worker=None

    def _task(self,task):
        if not isinstance(task,str) or not re.fullmatch(r'[A-Za-z0-9_-]+',task):raise ValueError('Invalid task')
        file=self.scenes/(task+'.yaml')
        if not file.is_file():raise ValueError('Unknown task')
        return file

    def _manifest_path(self,task):
        self._task(task)
        path=self.root/'.bench2dex/tasks'/(task+'.json')
        for parent in [path,*list(path.parents)[:3]]:
            if parent.is_symlink():raise ValueError('Unexpected asset manifest symlink')
        return path

    def _manifest(self,task,data):
        path=self._manifest_path(task);path.parent.mkdir(parents=True,exist_ok=True)
        temp=path.with_name(path.name+'.'+uuid.uuid4().hex+'.tmp')
        try:temp.write_text(json.dumps(data));temp.replace(path)
        finally:temp.unlink(missing_ok=True)

    def readiness(self,task,entries=None):
        entries=entries or scene_entries(self._task(task))
        missing=[x for x in entries if not destination(self.root,x).is_file()]
        manifest=None
        try:manifest=json.loads(self._manifest_path(task).read_text())
        except FileNotFoundError:pass
        except (OSError,ValueError):return False,len(missing),'Invalid dependency manifest'
        if manifest is not None:
            if not manifest.get('complete'):return False,len(missing),'Dependency download incomplete'
            for item in manifest.get('files',[]):
                path=destination(self.root,item['path'])
                if not path.is_file() or path.stat().st_size!=item['size']:
                    return False,max(1,len(missing)),'Missing dependency'
        # Previously verified/migrated legacy scenes have no download manifest.
        return not missing,len(missing),None

    def snapshot(self):
        with self.lock:
            return dict(root=str(self.root),busy=any(j['state'] in ('queued','downloading') for j in self.jobs.values()),
                        jobs=copy.deepcopy(list(self.jobs.values())))

    def _update(self,task,**fields):
        with self.lock:self.jobs[task].update(fields)

    def start(self,task):
        self._task(task)
        if not self.root.is_dir() or not os.access(self.root,os.W_OK|os.X_OK):
            raise ValueError('Asset root is not writable: '+str(self.root))
        with self.lock:
            if task in self.jobs and self.jobs[task]['state'] in ('queued','downloading'):
                return self.jobs[task]['id']
            identity=uuid.uuid4().hex
            self.jobs[task]=dict(id=identity,task=task,state='queued',bytes_received=0,bytes_total=0,
                                 completed_files=0,discovered_files=0,current_file='',error=None)
            self.cancels[task]=threading.Event()
            self.pending.put(task)
            if self.worker is None or not self.worker.is_alive():
                self.worker=threading.Thread(target=self._work,daemon=True,name='task-asset-downloader')
                self.worker.start()
            return identity

    def cancel(self,task):
        self._task(task)
        with self.lock:
            if task not in self.cancels:raise ValueError('No download for task')
            self.cancels[task].set()

    def _work(self):
        while True:
            try:task=self.pending.get(timeout=1)
            except queue.Empty:
                # Keep the sole worker alive; no networking while idle.
                continue
            try:self._download(task,self.cancels[task])
            except Cancelled:self._update(task,state='cancelled',current_file='')
            except Exception as exc:self._update(task,state='error',error=str(exc),current_file='')
            finally:self.pending.task_done()

    @staticmethod
    def _check(cancel):
        if cancel.is_set():raise Cancelled()

    def _file(self,task,remote,info,cancel):
        path=destination(self.root,info['path']);size=info['size']
        def digest(stream):
            sha=hashlib.sha256();git=hashlib.sha1(f'blob {size}\0'.encode());count=0
            while True:
                self._check(cancel)
                block=stream.read(1024*1024)
                if not block:break
                count+=len(block);sha.update(block);git.update(block)
            return count,sha.hexdigest(),git.hexdigest()
        def valid(result):
            count,sha,git=result
            return count==size and (not info.get('sha256') or sha==info['sha256']) and (not info.get('git_sha1') or git==info['git_sha1'])
        if path.is_file():
            with path.open('rb') as f:result=digest(f)
            if valid(result):return result[1],False
            # Existing unexpected content must never be silently overwritten.
            raise RuntimeError('Existing asset differs from pinned repository; inspect before retry: '+info['path'])
        path.parent.mkdir(parents=True,exist_ok=True)
        temp=path.with_name('.'+path.name+'.'+uuid.uuid4().hex+'.part')
        try:
            for attempt in range(3):
                self._check(cancel)
                try:
                    sha=hashlib.sha256();git=hashlib.sha1(f'blob {size}\0'.encode());count=0
                    with remote.open(info['path']) as response,temp.open('wb') as output:
                        while True:
                            self._check(cancel)
                            block=response.read(1024*1024)
                            if not block:break
                            output.write(block);sha.update(block);git.update(block);count+=len(block)
                            if count>size:raise RuntimeError('Oversized asset response')
                            with self.lock:self.jobs[task]['bytes_received']+=len(block)
                        output.flush();os.fsync(output.fileno())
                    result=(count,sha.hexdigest(),git.hexdigest())
                    if not valid(result):raise RuntimeError('Asset size/hash verification failed: '+info['path'])
                    # Protect against symlink replacement during the transfer.
                    destination(self.root,info['path'])
                    temp.replace(path)
                    return result[1],True
                except (OSError,RuntimeError):
                    if attempt==2:raise
                    if cancel.wait(.5*(attempt+1)):raise Cancelled()
        finally:temp.unlink(missing_ok=True)

    def _download(self,task,cancel):
        self._check(cancel)
        self._update(task,state='downloading')
        # Mark incomplete before any entrypoint can become visible to task selection.
        self._manifest(task,dict(complete=False))
        remote=self.remote_factory()
        self._update(task,revision=remote.revision)
        pending=list(scene_entries(self._task(task)))
        seen=set();files=[];builtins=[]
        while pending:
            self._check(cancel)
            relative=safe_relative(pending.pop(0))
            if relative in seen:continue
            seen.add(relative)
            if '<UDIM>' in relative:
                pending.extend(remote.expand(relative));continue
            info=remote.info(relative)
            if info['size']<0:raise ValueError('Invalid remote file size')
            with self.lock:
                self.jobs[task]['bytes_total']+=info['size']
                self.jobs[task]['discovered_files']+=1
            self._update(task,current_file=relative)
            sha,downloaded=self._file(task,remote,info,cancel)
            files.append(dict(path=relative,size=info['size'],sha256=sha))
            with self.lock:self.jobs[task]['completed_files']+=1
            for ref in self.scanner(destination(self.root,relative)):
                if not ref:continue  # Empty USD asset path denotes a same-layer reference.
                if ref in BUILTIN_MDL:
                    builtins.append(ref);continue
                if ref.startswith(('http:','https:','omniverse:','/')) or '\\' in ref:
                    raise ValueError('Unsupported non-portable USD dependency: '+ref)
                dependency=posixpath.normpath(posixpath.join(posixpath.dirname(relative),ref))
                pending.append(safe_relative(dependency))
        self._check(cancel)
        self._manifest(task,dict(complete=True,revision=remote.revision,files=files,builtin_mdl=sorted(set(builtins))))
        self._update(task,state='complete',current_file='',finished_at=time.time())
