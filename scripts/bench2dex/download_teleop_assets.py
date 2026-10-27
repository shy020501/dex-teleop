"""Download only the three kinematic URDFs required by existing Wuji DexPilot/UR5 IK."""
from pathlib import Path
from urllib.parse import quote
from urllib.request import urlopen
import hashlib
import json

root=Path(__file__).resolve().parents[2]
files={'Right_Hand.urdf':30034,'Left_Hand.urdf':31325,'Multi_UR5_wuji_with_flange.urdf':100799}
manifest=[]
for name,size in files.items():
    relative='URDF-Zoo/ur5+wuji/urdf/'+name
    path=root/'assets/bench2dex'/relative
    if not path.exists():
        path.parent.mkdir(parents=True,exist_ok=True)
        with urlopen('https://huggingface.co/datasets/Bench2Dex/Assets/resolve/main/'+quote(relative,safe='/')) as response:
            data=response.read()
        if len(data)!=size:raise RuntimeError('Unexpected URDF size: '+relative)
        path.write_bytes(data)
    data=path.read_bytes()
    if len(data)!=size:raise RuntimeError('Unexpected local URDF size: '+relative)
    manifest.append({'path':relative,'bytes':len(data),'sha256':hashlib.sha256(data).hexdigest()})
(root/'logs/teleop-assets-manifest.json').write_text(json.dumps(manifest,indent=2)+'\n')
print('Teleop URDFs ready:',sum(x['bytes'] for x in manifest),'bytes')
