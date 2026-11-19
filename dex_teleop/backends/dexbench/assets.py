"""Task-scoped pinned LFS payloads; never mutate the upstream source checkout.

Asset identities come from the pinned Git pointers, not a moving HF revision.
Small metadata/licenses are copied from the same commit. Required R1/H3 runtime
inputs are fetched separately at their pinned revision, without demonstrations.
"""
import copy
from concurrent.futures import ThreadPoolExecutor, as_completed
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import threading
import time
from urllib.parse import quote
from urllib.request import Request, urlopen
import uuid

from dex_teleop.paths import WORKSPACE
from .paths import ASSET_ROOT, SOURCE_ROOT, SOURCE_REVISION, LFS_ENDPOINT, INPUT_REPOSITORY, INPUT_REVISION

LOCK_PATH=WORKSPACE/'configs/dexbench-assets.lock.json'
INPUT_PREFIXES={'db_r1':'eval/db_conditions/db_r1_layouts/eval','db_h3':'eval/db_h3_inputs'}


class DownloadCancelled(Exception): pass


def _target(root, relative):
    rel=PurePosixPath(relative)
    if rel.is_absolute() or '..' in rel.parts or not rel.parts:
        raise ValueError('Unsafe asset path')
    result=root.joinpath(*rel.parts)
    if not result.resolve().is_relative_to(root.resolve()):raise ValueError('Asset path leaves its root')
    cursor=result
    while cursor!=root:
        if cursor.is_symlink():raise ValueError('Asset paths cannot traverse symlinks inside the asset root')
        cursor=cursor.parent
    return result


def _sha(path):
    h=hashlib.sha256()
    with path.open('rb') as f:
        for chunk in iter(lambda:f.read(1024*1024),b''):h.update(chunk)
    return h.hexdigest()


def _json_request(url, data=None, headers=None):
    if not url.startswith('https://'):raise ValueError('Asset service must use HTTPS')
    values={'User-Agent':'dex-teleop-dexbench','Accept':'application/json',**(headers or {})}
    if data is not None:values['Content-Type']='application/vnd.git-lfs+json'
    req=Request(url,None if data is None else json.dumps(data).encode(),headers=values)
    with urlopen(req,timeout=45) as response:
        return json.load(response),response.headers.get('Link','')


def _publish(temp,target,digest,size):
    if temp.stat().st_size!=size or _sha(temp)!=digest:
        raise ValueError('Downloaded asset integrity check failed: '+target.name)
    if target.exists():
        if target.stat().st_size==size and _sha(target)==digest:return
        raise ValueError('Existing asset differs from the pinned file; preserve or remove it before retrying: '+str(target))
    # link() fails if another writer created the name, without overwriting it.
    try:os.link(temp,target)
    except FileExistsError:
        if target.stat().st_size!=size or _sha(target)!=digest:raise


def _download_file(url,target,digest,size,cancel,progress,headers=None):
    if cancel.is_set():raise DownloadCancelled()
    if not url.startswith('https://'):raise ValueError('Asset download URL must use HTTPS')
    target.parent.mkdir(parents=True,exist_ok=True)
    temporary=target.with_name('.'+target.name+'.download-'+uuid.uuid4().hex)
    try:
        with urlopen(Request(url,headers=headers or {}),timeout=60) as response,temporary.open('xb') as output:
            received=0
            while True:
                if cancel.is_set():raise DownloadCancelled()
                chunk=response.read(1024*1024)
                if not chunk:break
                received+=len(chunk)
                if received>size:raise ValueError('Asset response exceeds its pinned size')
                output.write(chunk);progress(len(chunk))
        _publish(temporary,target,digest,size)
    finally:temporary.unlink(missing_ok=True)


class AssetDownloads:
    def __init__(self,root=ASSET_ROOT,source=SOURCE_ROOT,lock_path=LOCK_PATH):
        self.root,self.source=Path(root).resolve(),Path(source)
        self.manifest=json.loads(Path(lock_path).read_text())
        if self.manifest.get('source_revision')!=SOURCE_REVISION:raise ValueError('DexBench asset lock revision mismatch')
        self.lock=threading.RLock();self.jobs=[];self.worker=None;self.stop_event=threading.Event()
        self.validated={};self.readiness_cache={}

    def _valid(self,relative,entry):
        path=_target(self.root,relative)
        try:
            stat=path.stat()
            if not path.is_file() or stat.st_size!=entry['size']:return False
        except OSError:return False
        signature=(str(path),stat.st_size,stat.st_mtime_ns,stat.st_ctime_ns,stat.st_ino)
        with self.lock:
            cached=self.validated.get(signature)
        if cached is not None and time.monotonic()-cached[0]<60:return cached[1]==entry['sha256']
        digest=_sha(path)
        with self.lock:self.validated[signature]=(time.monotonic(),digest)
        return digest==entry['sha256']

    def _entries(self,task):
        from .task_info import task_info
        task_info(task)
        selected=set(self.manifest['tasks'][task])
        return {path:entry for path,entry in self.manifest['files'].items()
                if not entry['lfs'] or path in selected}

    def readiness(self,task):
        with self.lock:
            cached=self.readiness_cache.get(task)
            if cached and time.monotonic()-cached[0]<3:return cached[1]
        try:
            entries=self._entries(task)
            missing=sum(not self._valid(path,entry) for path,entry in entries.items())
            if task in INPUT_PREFIXES:
                marker=self.root/'runtime_inputs'/('.'+task+'.json')
                try:
                    inputs=json.loads(marker.read_text())
                    if inputs['revision']!=INPUT_REVISION or not inputs['files']:raise ValueError('Runtime input marker is invalid')
                    missing+=sum(not self._valid(path,entry) for path,entry in inputs['files'].items())
                except (OSError,ValueError,KeyError):missing+=1
            result=(missing==0,missing,None)
        except (OSError,ValueError,KeyError) as exc:result=(False,1,str(exc))
        with self.lock:self.readiness_cache[task]=(time.monotonic(),result)
        return result

    def snapshot(self):
        with self.lock:return dict(root=str(self.root),busy=bool(self.worker and self.worker.is_alive()),jobs=copy.deepcopy(self.jobs))

    def start(self,task):
        self._entries(task)
        with self.lock:
            if self.worker and self.worker.is_alive():raise ValueError('A DexBench asset download is already running')
            self.stop_event=threading.Event()
            job=dict(id=uuid.uuid4().hex,task=task,state='downloading',bytes_received=0,bytes_total=0,
                     completed_files=0,discovered_files=0,error=None)
            self.jobs.append(job);self.jobs=self.jobs[-20:]
            self.worker=threading.Thread(target=self._run,args=(task,job,self.stop_event),daemon=True,name='dexbench-assets')
            self.worker.start();return job['id']

    def cancel(self,task):
        with self.lock:
            if not self.worker or not self.worker.is_alive():return
            if self.jobs[-1]['task']!=task:raise ValueError('The selected task is not being downloaded')
            self.stop_event.set()

    def install(self,task):
        """Blocking command-line setup path, with the same manifest as the UI."""
        self.start(task);self.worker.join()
        job=self.snapshot()['jobs'][-1]
        if job['state']!='complete':raise RuntimeError(job['error'] or job['state'])
        return job

    def _run(self,task,job,cancel):
        try:
            self.root.mkdir(parents=True,exist_ok=True)
            entries=self._entries(task)
            pending={p:e for p,e in entries.items() if not self._valid(p,e)}
            with self.lock:job.update(discovered_files=len(pending),bytes_total=sum(e['size'] for e in pending.values()))
            def progress(size):
                with self.lock:job['bytes_received']+=size
            for relative,entry in pending.items():
                if entry['lfs']:continue
                if cancel.is_set():raise DownloadCancelled()
                target=_target(self.root,relative);target.parent.mkdir(parents=True,exist_ok=True)
                source=self.source/'assets'/relative
                if source.is_file() and source.stat().st_size==entry['size'] and _sha(source)==entry['sha256']:
                    temporary=target.with_name('.'+target.name+'.copy-'+uuid.uuid4().hex)
                    try:
                        temporary.write_bytes(source.read_bytes());_publish(temporary,target,entry['sha256'],entry['size'])
                    finally:temporary.unlink(missing_ok=True)
                    progress(entry['size'])
                else:
                    url=f'https://raw.githubusercontent.com/RLWRLD/DexBench/{SOURCE_REVISION}/assets/{quote(relative,safe="/")}'
                    _download_file(url,target,entry['sha256'],entry['size'],cancel,progress)
                with self.lock:job['completed_files']+=1
            payloads=[(p,e) for p,e in pending.items() if e['lfs']]
            with ThreadPoolExecutor(max_workers=8,thread_name_prefix='dexbench-lfs') as pool:
                for begin in range(0,len(payloads),80):
                    if cancel.is_set():raise DownloadCancelled()
                    batch=payloads[begin:begin+80]
                    objects={e['sha256']:dict(oid=e['sha256'],size=e['size']) for _,e in batch}
                    reply,_=_json_request(LFS_ENDPOINT+'/objects/batch',dict(operation='download',transfers=['basic'],objects=list(objects.values())))
                    links={item['oid']:item for item in reply['objects']}
                    futures=[]
                    for relative,entry in batch:
                        item=links.get(entry['sha256'],{})
                        if 'error' in item or 'download' not in item.get('actions',{}):
                            raise ValueError('Pinned asset is unavailable from the upstream LFS service: '+relative)
                        link=item['actions']['download']
                        futures.append(pool.submit(_download_file,link['href'],_target(self.root,relative),entry['sha256'],entry['size'],cancel,progress,link.get('header')))
                    for future in as_completed(futures):
                        future.result()
                        with self.lock:job['completed_files']+=1
            if task in INPUT_PREFIXES:self._install_inputs(task,cancel,progress,job)
            with self.lock:self.readiness_cache.clear()
            ready,_,error=self.readiness(task)
            if not ready:raise ValueError(error or 'Some task assets are still missing after download')
            with self.lock:job.update(state='complete',finished_at=time.time())
        except DownloadCancelled:
            with self.lock:job.update(state='cancelled',error=None)
        except Exception as exc:
            cancel.set()
            with self.lock:job.update(state='error',error=str(exc))
        finally:
            with self.lock:self.readiness_cache.clear()

    def _install_inputs(self,task,cancel,progress,job):
        prefix=INPUT_PREFIXES[task]
        url=f'https://huggingface.co/api/datasets/{INPUT_REPOSITORY}/tree/{INPUT_REVISION}/{prefix}?recursive=true&limit=1000'
        entries=[]
        while url:
            if cancel.is_set():raise DownloadCancelled()
            batch,links=_json_request(url)
            entries.extend(item for item in batch if item['type']=='file')
            next_url=re.search(r'<([^>]+)>;\s*rel="next"',links)
            url=next_url.group(1) if next_url else None
            if url and not url.startswith('https://huggingface.co/'):raise ValueError('Invalid runtime input pagination URL')
        if not entries or sum(item['size'] for item in entries)>256*1024**2:
            raise ValueError('Runtime input bundle is empty or exceeds the 256 MiB safety limit')
        result={}
        for item in entries:
            # R1 pile JSON / H3 numerical starts only; never training trajectories.
            relative_path=PurePosixPath(item['path'])
            if not str(relative_path).startswith(prefix+'/'):raise ValueError('Runtime input escapes selected prefix')
            input_root=prefix.removeprefix('eval/db_conditions/').removeprefix('eval/')
            relative='runtime_inputs/'+input_root+'/'+str(relative_path.relative_to(prefix))
            target=_target(self.root,relative);target.parent.mkdir(parents=True,exist_ok=True)
            digest=item.get('lfs',{}).get('oid')
            if digest:
                entry=dict(size=item['size'],sha256=digest)
                if not self._valid(relative,entry):
                    with self.lock:job['discovered_files']+=1;job['bytes_total']+=entry['size']
                    _download_file(f'https://huggingface.co/datasets/{INPUT_REPOSITORY}/resolve/{INPUT_REVISION}/{quote(item["path"],safe="/")}',target,digest,entry['size'],cancel,progress)
                    with self.lock:job['completed_files']+=1
            else:
                # Non-LFS files have Git blob SHA-1, which binds bytes at the pin.
                req=Request(f'https://huggingface.co/datasets/{INPUT_REPOSITORY}/resolve/{INPUT_REVISION}/{quote(item["path"],safe="/")}')
                with urlopen(req,timeout=45) as response:payload=response.read(item['size']+1)
                blob=hashlib.sha1(f'blob {len(payload)}\0'.encode()+payload).hexdigest()
                if len(payload)!=item['size'] or blob!=item['oid']:raise ValueError('Runtime input Git identity mismatch')
                entry=dict(size=len(payload),sha256=hashlib.sha256(payload).hexdigest())
                if not self._valid(relative,entry):
                    temporary=target.with_name('.'+target.name+'.input-'+uuid.uuid4().hex)
                    try:temporary.write_bytes(payload);_publish(temporary,target,entry['sha256'],entry['size'])
                    finally:temporary.unlink(missing_ok=True)
                progress(len(payload))
            result[relative]=entry
        marker=self.root/'runtime_inputs'/('.'+task+'.json')
        temporary=marker.with_suffix('.tmp')
        temporary.write_text(json.dumps(dict(revision=INPUT_REVISION,files=result),indent=2)+'\n');temporary.replace(marker)
