"""User-invoked public editor startup; no hard-coded user or game paths."""
import json
from pathlib import Path
import sys

ROOT=Path(__file__).resolve().parents[1]


def main():
    import bpy
    if bpy.app.background: raise ValueError('Use the reconstruction CLI for headless preparation')
    ready=ROOT/'.local/ready.json'
    target=None
    if ready.is_file():
        record=json.loads(ready.read_text(encoding='utf-8'))
        import hashlib
        expected=hashlib.sha256((ROOT/'project/manifest.json').read_bytes()).hexdigest()
        path=Path(record['path']).resolve()
        if not path.is_relative_to(ROOT.resolve()): raise ValueError('Saved workspace is outside this editor folder')
        if record.get('project_sha256')==expected and path.is_file(): target=path
    if target: bpy.ops.wm.open_mainfile(filepath=str(target))
    else: bpy.ops.wm.read_factory_settings(use_empty=True)
    previous=sys.modules.get('wz2009_map_editor')
    if previous and hasattr(bpy.types,'WZ09_PT_main'): previous.unregister()
    for name in list(sys.modules):
        if name=='wz2009_map_editor' or name.startswith('wz2009_map_editor.'): del sys.modules[name]
    sys.path.insert(0,str(ROOT/'addons'))
    import wz2009_map_editor as addon
    addon.register()
    bpy.context.scene.wz09_project_root=str(ROOT)
    from wz2009_map_editor import map_residency
    if target: map_residency.initialize_workshop_view(bpy.context)
    for area in bpy.context.window.screen.areas:
        if area.type=='VIEW_3D':
            area.spaces.active.show_region_ui=True
            for region in area.regions:
                if region.type=='UI':
                    try: region.active_panel_category='WZ2009'
                    except (AttributeError,TypeError): pass


if __name__=='__main__': main()
