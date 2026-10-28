from dex_teleop.paths import WORKSPACE
SOURCE_ROOT = WORKSPACE/'third_party/DexMimicGen'
ASSET_ROOT = (WORKSPACE/'assets/dexmimicgen').resolve()
DATA_ROOT = (WORKSPACE/'data/dexmimicgen').resolve()
SOURCES = {
    'dexmimicgen': ('https://github.com/NVlabs/dexmimicgen.git', '940e8a1b3ad70eb1925ada6b364b197de6bb2af9', SOURCE_ROOT, 'dexmimicgen/models/assets'),
    'robosuite': ('https://github.com/ARISE-Initiative/robosuite.git', '824ac14cefcfb7ec125fe5eb2e0bad7364466154', SOURCE_ROOT/'dependencies/robosuite', 'robosuite/models/assets'),
}
