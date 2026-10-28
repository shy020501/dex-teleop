"""Pinned source bundle, including all eleven task assets and GeoRT weights."""
import copy
import json
from pathlib import Path
import shutil
import hashlib
import os
import subprocess
import threading
import time
import uuid

from .paths import ASSET_ROOT, SOURCE_ROOT, SOURCE_REPOSITORY, SOURCE_REVISION

def _digest(path):
    checksum=hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda:stream.read(1024*1024),b''):checksum.update(chunk)
    return checksum.digest()


def _merge_move(source,target):
    """Resume a cross-device move without replacing unrelated existing files."""
    if source.is_symlink():
        if target.is_symlink() and os.readlink(source)==os.readlink(target):source.unlink();return
        if target.exists() or target.is_symlink():raise ValueError('Conflicting existing asset: '+str(target))
        target.symlink_to(os.readlink(source));source.unlink();return
    if source.is_dir():
        if target.is_symlink() or (target.exists() and not target.is_dir()):raise ValueError('Conflicting asset directory: '+str(target))
        target.mkdir(parents=True,exist_ok=True)
        for child in source.iterdir():_merge_move(child,target/child.name)
        source.rmdir();return
    if target.is_symlink():raise ValueError('Conflicting asset symlink: '+str(target))
    if target.exists():
        if not target.is_file() or source.stat().st_size!=target.stat().st_size or _digest(source)!=_digest(target):
            raise ValueError('Existing asset differs from pinned source: '+str(target))
    else:
        temporary=target.with_name('.'+target.name+'.moving-'+uuid.uuid4().hex)
        try:
            shutil.copy2(source,temporary)
            os.link(temporary,target)  # Publish without replacing an existing file.
        finally:temporary.unlink(missing_ok=True)
    source.unlink()


def prepare_sources():
    from scripts.common.setup_storage import prepare_simulator_storage
    from dex_teleop.paths import WORKSPACE
    prepare_simulator_storage(WORKSPACE,'dexjoco')
    if not SOURCE_ROOT.exists():
        SOURCE_ROOT.parent.mkdir(parents=True,exist_ok=True)
        staging=SOURCE_ROOT.with_name('.DexJoCo-download-'+uuid.uuid4().hex)
        try:
            subprocess.run(['git','clone','--no-checkout',SOURCE_REPOSITORY,str(staging)],check=True)
            subprocess.run(['git','-C',str(staging),'checkout','--detach',SOURCE_REVISION],check=True)
            staging.rename(SOURCE_ROOT)
        finally:
            if staging.exists():shutil.rmtree(staging)
    # Recover a prior no-checkout clone only when it contains no user worktree files.
    if {item.name for item in SOURCE_ROOT.iterdir()}=={'.git'}:
        subprocess.run(['git','-C',str(SOURCE_ROOT),'checkout','--detach',SOURCE_REVISION],check=True)
    head=subprocess.check_output(['git','-C',str(SOURCE_ROOT),'rev-parse','HEAD'],text=True).strip()
    if head!=SOURCE_REVISION:
        raise ValueError('Existing DexJoCo checkout has a different revision; keep it separate before installing the pinned source')
    for relative,name in [('dexjoco/dexjoco/sim/envs/xmls','xmls'),
                          ('teleoperation/GeoRT/checkpoint','geort-checkpoints'),
                          ('teleoperation/GeoRT/assets','geort-assets')]:
        source,target=SOURCE_ROOT/relative,ASSET_ROOT/name
        if source.is_symlink():
            if source.resolve()!=target.resolve() or not target.is_dir():raise ValueError('Conflicting or missing DexJoCo assets: '+str(source))
        else:
            if source.exists():_merge_move(source,target)
            if not target.is_dir():raise ValueError('Pinned asset tree is missing: '+str(target))
            source.symlink_to(target,target_is_directory=True)
    manifest=ASSET_ROOT/'.dex-teleop-assets.json'
    temporary=manifest.with_suffix('.tmp')
    temporary.write_text(json.dumps(dict(repository=SOURCE_REPOSITORY,revision=SOURCE_REVISION,
                                        tasks=11,complete=True),indent=2)+'\n')
    temporary.replace(manifest)
    return dict(source=str(SOURCE_ROOT),assets=str(ASSET_ROOT),revision=SOURCE_REVISION)

class AssetDownloads:
    def __init__(self):self.lock=threading.RLock();self.jobs=[];self.worker=None
    def ready(self):
        try:manifest=json.loads((ASSET_ROOT/'.dex-teleop-assets.json').read_text())
        except (OSError,ValueError):return False
        return manifest.get('complete') is True and manifest.get('revision')==SOURCE_REVISION and all(
            (ASSET_ROOT/name).is_dir() for name in ('xmls','geort-checkpoints','geort-assets'))
    def snapshot(self):
        with self.lock:return dict(root=str(ASSET_ROOT),busy=bool(self.worker and self.worker.is_alive()),jobs=copy.deepcopy(self.jobs))
    def start(self,task):
        from .task_info import task_info
        task_info(task)
        with self.lock:
            if self.worker and self.worker.is_alive():raise ValueError('DexJoCo source installation is already running')
            job=dict(id=uuid.uuid4().hex,task=task,state='downloading',bytes_received=0,bytes_total=0,
                     completed_files=0,discovered_files=0,error=None)
            self.jobs.append(job)
            def work():
                try:
                    prepare_sources()
                    with self.lock:job.update(state='complete',finished_at=time.time())
                except Exception as exc:
                    with self.lock:job.update(state='error',error=str(exc))
            self.worker=threading.Thread(target=work,daemon=True,name='dexjoco-assets');self.worker.start()
            return job['id']
    def cancel(self,task):
        raise ValueError('This source-bundle download cannot be cancelled from the browser; wait for installation to finish')
