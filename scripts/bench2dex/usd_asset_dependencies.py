"""Read USD asset dependencies with Isaac Sim's installed USD libraries, without starting Kit/GPU."""
import json
import re
import sys
from pathlib import Path

# The parent supplies isolated library paths before starting this interpreter.
from pxr import Sdf

file=Path(sys.argv[1])
layer=Sdf.Layer.FindOrOpen(str(file))
if layer is None:raise RuntimeError('Cannot parse USD: '+str(file))
refs=set(layer.GetExternalReferences()) | set(layer.GetExternalAssetDependencies())
refs.update(re.findall(r'@([^@]+)@',layer.ExportToString()))
print(json.dumps(sorted(ref for ref in refs if ref)))
