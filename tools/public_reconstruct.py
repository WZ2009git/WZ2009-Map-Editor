"""Build a user-local workspace after asset conversion; no GUI/game launch."""
import argparse
from pathlib import Path
import sys

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'addons'))

if __name__=='__main__':
    import bpy
    import wz2009_map_editor as addon
    from wz2009_map_editor import public_workspace,installation_setup
    parser=argparse.ArgumentParser();parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args(sys.argv[sys.argv.index('--')+1:])
    addon.register()
    public_workspace.rebuild(ROOT,ROOT/'.local/assets.json',args.output,
                             status=lambda **values:installation_setup.atomic_json(ROOT/'.local/progress.json',values))
