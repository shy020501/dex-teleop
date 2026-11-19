"""Optional sudo for Docker only; source setup and host files retain the caller's UID.

Set DEX_TELEOP_DOCKER_SUDO=true in an interactive terminal when the account
can use sudo docker but has no direct daemon access. Docker/sudo may prompt
on that terminal; no password is stored or passed by these scripts.
"""
from contextlib import contextmanager
import os
from pathlib import Path
import shlex
import shutil
import tempfile


def docker_prefix(env=None):
    env = os.environ if env is None else env
    setting = env.get('DEX_TELEOP_DOCKER_SUDO', 'false').lower()
    if setting not in ('true', 'false', '1', '0'):
        raise ValueError('DEX_TELEOP_DOCKER_SUDO must be true or false')
    if setting in ('false', '0'):
        return ['docker']
    docker = shutil.which('docker', path=env.get('PATH'))
    sudo = shutil.which('sudo', path=env.get('PATH'))
    if not docker or not sudo:
        raise ValueError('DEX_TELEOP_DOCKER_SUDO requires docker and sudo on PATH')
    return [str(Path(sudo).resolve()), '--', str(Path(docker).resolve())]


@contextmanager
def upstream_docker_environment(env=None):
    """Adapt the pinned upstream build's literal `docker` calls without editing it."""
    env = dict(os.environ if env is None else env)
    prefix = docker_prefix(env)
    if prefix == ['docker']:
        yield env
        return
    # Scope this private shim to the upstream subprocess, not the user's shell
    # or Python parent. Absolute paths prevent recursion into the shim itself.
    with tempfile.TemporaryDirectory(prefix='dexbench-docker-access-') as directory:
        wrapper = Path(directory)/'docker'
        wrapper.write_text('#!/bin/sh\nexec '+shlex.join(prefix)+' "$@"\n')
        wrapper.chmod(0o700)
        env['PATH'] = directory+os.pathsep+env.get('PATH', os.defpath)
        yield env
