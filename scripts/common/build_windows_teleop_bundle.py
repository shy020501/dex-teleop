"""Build a standalone Windows input client without simulator code or SDK binaries."""
from pathlib import Path
import shutil
from zipfile import ZipFile, ZIP_DEFLATED

ROOT = Path(__file__).resolve().parents[2]


def build_bundle(root=ROOT, output=None):
    root = Path(root)
    publish_legacy_name = output is None
    files = [root/'dex_teleop'/name for name in ('__init__.py', 'paths.py', 'windows_client.py')]
    for package in ('core', 'devices'):
        files.extend((root/'dex_teleop'/package).rglob('*.py'))
    files.append(root/'configs/client.yaml')
    files.extend(root/'windows'/name for name in (
        'Update-Teleop.ps1', 'Setup-Teleop.ps1', 'Run-Teleop.ps1',
        'Start-TeleopTunnel.ps1', 'requirements-teleop.txt', 'README.md'))
    output = Path(output or root/'dist/dex_teleop_windows.zip')
    output.parent.mkdir(parents=True, exist_ok=True)
    with ZipFile(output, 'w', ZIP_DEFLATED) as archive:
        for path in sorted(files):
            archive.write(path, path.relative_to(root))
    if publish_legacy_name:
        # Earlier setup instructions use this filename. Never leave a stale GUI
        # client behind when publishing the current console-only distribution.
        shutil.copyfile(output, output.with_name('bench2dex_windows_teleop.zip'))
    return output, len(files)


if __name__ == '__main__':
    output, count = build_bundle()
    print(f'{output}: {count} files, {output.stat().st_size} bytes')
