#!/usr/bin/env python3
"""Regenerate asset identities from the pinned source; no network or payload fetch."""
import hashlib
import json
import re
from pathlib import Path
import sys
ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT))
from dex_teleop.backends.dexbench.paths import SOURCE_ROOT,SOURCE_REVISION
from dex_teleop.backends.dexbench.task_info import TASKS


def build_manifest(source=SOURCE_ROOT):
    root=source/'assets'
    index=json.loads((root/'env/scene_bundle_index.json').read_text())
    catalog=json.loads((root/'object_library/catalog.json').read_text())['assets']
    categories={
        'db_r1':{'produce'},'db_r2':{'beverage'},'db_r3':{'packaged-food'},
        'db_l1':{'deformable','parcel'},'db_l2':{'parcel'},'db_l3':{'parcel'},'db_l4':{'parcel'},
        'db_m1':set(),'db_m2':set(),'db_m3':{'hand-tool'},'db_m4':set(),'db_m5':{'industrial-part'},
        'db_h1':set(),'db_h2':set(),'db_h3':{'cutlery'},
    }
    extras={
        'db_m1':{'lg_fh25eae_washer','lg_fh25eae_detergent_drawer'},
        'db_m2':{'lg_mnext3_liner_connector_task'},
        'db_m3':{'lg_fx25_base_cabinet','center_hinge_station'},
        'db_m4':{'lg_fx25_base_cabinet','lg_fx25_leveling_leg'},
        'db_m5':{'kit_box_fc','parts_bin_fc'},
        'db_h2':{'milk_pitcher_d00527_x15','steel_jug_1l','steel_pitcher_footed','steel_pitcher_large','steel_water_jug_tall'},
    }
    files={}
    for path in sorted(root.rglob('*')):
        if not path.is_file():continue
        if path.is_symlink():raise ValueError('Pinned asset tree cannot contain symlinks')
        payload=path.read_bytes();relative=path.relative_to(root).as_posix()
        pointer=payload.startswith(b'version https://git-lfs.github.com/spec/v1\n')
        if pointer:
            fields=dict(line.split(b' ',1) for line in payload.splitlines())
            digest=fields[b'oid'].decode().removeprefix('sha256:');size=int(fields[b'size'])
        else:digest=hashlib.sha256(payload).hexdigest();size=len(payload)
        files[relative]=dict(sha256=digest,size=size,lfs=pointer)
    tasks={}
    for task,info in TASKS.items():
        group=info['scene_group']
        scene_prefix=index['bundles'][group]['path'].split('/')[1]
        packages=set(extras.get(task,set()))
        packages.update(index['bundles'][group].get('fixture_packages',[]))
        # Reference views can restore external catalog fixtures even when the
        # integrated package also embeds the original source geometry.
        for view in (root/'env'/scene_prefix).rglob('*.usda'):
            for reference in re.findall(r'@([^@]+)@',view.read_text(errors='replace')):
                target=(view.parent/reference.split('[',1)[0]).resolve()
                if target.is_relative_to(root.resolve()/'object_library'):
                    packages.add(target.relative_to(root.resolve()).parts[1])
        for item in catalog:
            if categories[task].intersection(item.get('categories',[])):
                packages.add(Path(item['path']).parts[1])
        selected=[]
        for path,entry in files.items():
            if not entry['lfs']:continue
            if (path.startswith('embodiments/simready/rby1m_wujihand2_beta2/')
                or path.startswith('env/'+scene_prefix+'/')
                or (path.startswith('object_library/') and path.split('/')[1] in packages)):
                selected.append(path)
        tasks[task]=selected
    return dict(format_version=1,source_revision=SOURCE_REVISION,files=files,tasks=tasks)

if __name__=='__main__':
    result=build_manifest()
    destination=ROOT/'configs/dexbench-assets.lock.json'
    destination.write_text(json.dumps(result,indent=2)+'\n')
    print(destination)
    for task,files in result['tasks'].items():print(task,len(files),sum(result['files'][p]['size'] for p in files))
