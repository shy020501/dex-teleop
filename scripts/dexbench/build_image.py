#!/usr/bin/env python3
"""Build the pinned runtime. Set DEX_TELEOP_DOCKER_SUDO=true for sudo Docker access."""
import argparse
import os
from pathlib import Path
import subprocess
import sys
import tempfile
ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT))
from dex_teleop.backends.dexbench.paths import SOURCE_ROOT,IMAGE,UPSTREAM_IMAGE,BASE_IMAGE
from scripts.dexbench.setup import prepare_sources
from scripts.dexbench.docker_access import docker_prefix, upstream_docker_environment


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--tag',default=os.environ.get('DEX_TELEOP_DEXBENCH_IMAGE',IMAGE))
    parser.add_argument('--reuse-upstream-image',action='store_true',help='Use an already built pinned upstream image')
    args=parser.parse_args()
    if not args.tag or any(c.isspace() for c in args.tag):parser.error('Invalid image tag')
    docker=docker_prefix()
    probe=subprocess.run([*docker,'version','--format','{{.Server.Version}}'],text=True,capture_output=True)
    if probe.returncode:
        raise SystemExit('Docker daemon access is required. If sudo docker works, run this script as your normal user with DEX_TELEOP_DOCKER_SUDO=true. Otherwise ask the administrator for Docker access.\n'+probe.stderr.strip())
    prepare_sources()
    if f'ARG BASE_IMAGE={BASE_IMAGE}' not in (SOURCE_ROOT/'docker/Dockerfile').read_text():
        raise SystemExit('The source Dockerfile no longer matches the pinned base image')
    if not args.reuse_upstream_image:
        with upstream_docker_environment() as env:
            subprocess.run([sys.executable,str(SOURCE_ROOT/'docker/build.py'),'--root',str(SOURCE_ROOT),'--tag',UPSTREAM_IMAGE],check=True,env=env)
    else:
        labels=subprocess.check_output([*docker,'image','inspect',UPSTREAM_IMAGE,'--format','{{ index .Config.Labels "org.opencontainers.image.revision" }}'],text=True).strip()
        from dex_teleop.backends.dexbench.paths import SOURCE_REVISION
        if labels!=SOURCE_REVISION:raise SystemExit('Existing upstream image has a different source revision')
    # Tiny context excludes data, assets and any unrelated user files.
    with tempfile.TemporaryDirectory(prefix='dex-teleop-dexbench-overlay-') as folder:
        directory=Path(folder)
        (directory/'requirements.txt').write_bytes((ROOT/'requirements/dexbench-runtime.txt').read_bytes())
        (directory/'Dockerfile').write_text(f'''FROM {UPSTREAM_IMAGE}
USER root
COPY requirements.txt /tmp/dex-teleop-requirements.txt
RUN /isaac-sim/python.sh -m pip install --no-deps -r /tmp/dex-teleop-requirements.txt && rm /tmp/dex-teleop-requirements.txt
RUN /isaac-sim/python.sh -c "import h5py, PIL, yaml, zmq; from dex_retargeting.retargeting_config import RetargetingConfig"
''')
        subprocess.run([*docker,'build','--tag',args.tag,str(directory)],check=True)
    print('Built '+args.tag)

if __name__=='__main__':main()
