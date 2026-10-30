"""Explicit runtime-package UI, reusing the versioned project build adapter.

Ordinary World JSON export never calls this module. The operator uses current
source-bound maps and already exported geometry, without starting Blender/game
processes or silently generating a terrain/GLB revision.
"""
import datetime
import copy
import hashlib
import importlib
import importlib.util
import json
from pathlib import Path
import uuid

_CLASSES = []


def _sha256(path):
    digest=hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda:stream.read(1024*1024),b''):digest.update(block)
    return digest.hexdigest()


def _portable_geometry(world,source,folder,root):
    """Copy only the already verified whole-scene GLB into this fresh package."""
    source=Path(source).resolve();folder=Path(folder).resolve();root=Path(root).resolve()
    source.relative_to(root);folder.relative_to(root/'exports')
    records=world.get('geometry',[])
    expected=_sha256(source)
    for record in records:
        referenced=(source.parent/record['path']).resolve()
        if referenced!=source or record.get('sha256')!=expected:
            raise ValueError('Geometry preflight does not bind the same unchanged GLB')
    if not records:raise ValueError('No verified GLB geometry available to package')
    relative='geometry/'+expected+'.glb';destination=folder/relative
    destination.parent.mkdir(parents=True,exist_ok=True)
    source_before=source.stat();digest=hashlib.sha256();created=False
    try:
        with source.open('rb') as incoming,destination.open('xb') as outgoing:
            created=True
            for block in iter(lambda:incoming.read(1024*1024),b''):
                digest.update(block);outgoing.write(block)
        source_after=source.stat()
        if (digest.hexdigest()!=expected or _sha256(source)!=expected or _sha256(destination)!=expected
                or source_before.st_size!=source_after.st_size or source_before.st_mtime_ns!=source_after.st_mtime_ns):
            raise ValueError('Source GLB changed while packaging')
    except Exception:
        # Only this newly created derivative may be removed; originals stay read-only.
        if created and destination.is_file():destination.unlink()
        raise
    result=copy.deepcopy(world)
    for record in result['geometry']:record['path']=relative
    return result,dict(path=str(source),sha256=expected,bytes=source_before.st_size,
                       packaged_path=relative,packaged_copy_verified=True)


def _addon():
    return importlib.import_module('wz2009_map_editor')


def _helper(root):
    path = root/'tools/karvorsk_runtime_build.py'
    if not path.is_file():
        raise ValueError('Runtime adapter missing: tools/karvorsk_runtime_build.py is required in the selected editor project')
    spec = importlib.util.spec_from_file_location('wz09_project_runtime_build', path)
    if spec is None or spec.loader is None:
        raise ValueError('The existing runtime adapter cannot be loaded')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _preconditions(scene, world):
    if len(world.get('maps', [])) != 16 or len({m['id'] for m in world['maps']}) != 16:
        raise ValueError('Runtime package requires the 16 existing maps with their stable IDs')
    if any(not m.get('provenance', {}).get('preview_bundle', {}).get('reference_sha256') for m in world['maps']):
        raise ValueError('All maps require verified native source references')
    tiles = [o for o in scene.objects if o.get('wz09_terrain_tile_json') and not o.get('wz09_archived_generation')]
    if not tiles:
        raise ValueError('Generate a terrain revision first; the runtime package does not generate terrain automatically')
    if not scene.get('wz09_glb_absolute') or not world.get('geometry'):
        raise ValueError('Export the current authored geometry to a new GLB first')
    if not scene.get('wz09_cinematics_json'):
        raise ValueError('A confirmed intro timeline with aircraft and handoff event is required')


def export_runtime_package(ctx, *, addon_api=None, helper=None, products_api=None, suffix=None):
    """One explicit, fresh editor-only export transaction; injectable for tests."""
    api = addon_api or _addon()
    products = products_api or importlib.import_module('wz2009_map_editor.product_export')
    root = Path(api.project_root(ctx.scene)).resolve()
    if not (root/'AGENTS.md').is_file() or not (root/'schema/world-v1.schema.json').is_file():
        raise ValueError('Select the standalone editor project containing AGENTS.md and the world schema')
    stamp = suffix or (datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%dT%H%M%S')+'-'+uuid.uuid4().hex[:6])
    if not isinstance(stamp, str) or not stamp or any(c not in 'abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_' for c in stamp):
        raise ValueError('Invalid runtime output version')
    folder = (root/'exports'/('karvorsk-runtime-'+stamp)).resolve()
    if not folder.is_relative_to(root/'exports') or folder.exists():
        raise FileExistsError('Runtime package requires a new versioned output folder')
    target = folder/'world.json'
    scene = ctx.scene
    saved = (scene.frame_current, getattr(scene, 'frame_subframe', 0.), scene.camera)
    manager = getattr(ctx, 'window_manager', None)
    if manager is not None:
        manager.progress_begin(0, 5)
    def stage(number, label):
        scene['wz09_runtime_export_status'] = label
        if manager is not None:
            manager.progress_update(number)
    def validate(world,base_dir):
        result = api.validate_world(world, base_dir=base_dir, diagnostic_limits=api.diagnostic_limits(scene))
        if not result['ok']:
            raise ValueError('Runtime preflight: '+str(result['errors'][0]))
    try:
        stage(0, 'Check current sources and GLB')
        if not scene.get('wz09_glb_absolute'):
            raise ValueError('Export the current authored geometry to a new GLB first')
        source_glb=Path(api.output_path(scene,str(scene['wz09_glb_absolute']))).resolve()
        source_glb.relative_to(root)
        if not source_glb.is_file() or source_glb.suffix.lower()!='.glb':
            raise ValueError('Existing verified source GLB is missing')
        # This target is never written. Its directory makes the existing source
        # GLB reference portable during both read-only source prevalidations.
        preflight=source_glb.parent/'runtime-preflight-world.json'
        before = api.document_for_export(ctx, preflight, include_tactical=False)
        _preconditions(scene, before)
        validate(before,source_glb.parent)
        adapter = helper or _helper(root)
        stage(1, 'Bind verified map registrations')
        calibration = adapter.attach_calibrations(ctx, root=root)
        world = api.document_for_export(ctx, preflight, include_tactical=False)
        validate(world,source_glb.parent)
        authoring = products.collect(scene, world)
        folder.mkdir(parents=True, exist_ok=False)
        world,geometry_source=_portable_geometry(world,source_glb,folder,root)
        validate(world,folder)
        stage(2, 'Export 30 Hz intro, terrain and tactical map')
        runtime = adapter.build(ctx, world, folder, root=root)
        stage(3, 'Bind editor products to runtime products')
        documents = authoring+runtime['documents']
        published = products.publish(world, documents, target, root)
        kinds = {r['kind'] for r in published.get('products', [])}
        required = set(products.RUNTIME_KINDS)
        if not required <= kinds:
            raise ValueError('Runtime package does not contain all four runtime products')
        validate(published,folder)
        if _sha256(source_glb)!=geometry_source['sha256']:
            raise ValueError('Source GLB changed during runtime export')
        stage(4, 'Write complete world manifest atomically')
        api.atomic_write(target, published, root=root)
        result = dict(path=str(target), output_dir=str(folder), product_count=len(published.get('products', [])),
            runtime_product_count=len(required), calibration=calibration,
            runtime_evidence_path=str(runtime['evidence_path']), game_started=False,
            geometry_source=geometry_source,collision_verified=False, runtime_integrated=False)
        scene['wz09_runtime_last_export_json'] = json.dumps(result, sort_keys=True)
        stage(5, 'Runtime package saved: '+folder.name)
        return result
    except Exception as exc:
        scene['wz09_runtime_export_status'] = 'Export angehalten: '+str(exc)[:220]
        raise
    finally:
        scene.camera = saved[2]
        scene.frame_set(saved[0], subframe=saved[1])
        if getattr(ctx, 'view_layer', None) is not None:
            ctx.view_layer.update()
        if manager is not None:
            manager.progress_end()


def register():
    import bpy
    class WZ09_OT_export_runtime_package(bpy.types.Operator):
        bl_idname = 'wz09.export_runtime_package'
        bl_label = 'Runtime package - 30 Hz'
        bl_description = 'Export the 30 Hz intro, terrain, tactical map and bound editor products into a new version'
        def execute(self, ctx):
            try:
                result = export_runtime_package(ctx)
                self.report({'INFO'}, 'Runtime package saved: '+result['output_dir'])
                return {'FINISHED'}
            except (OSError, ValueError, RuntimeError, KeyError, ImportError) as exc:
                self.report({'ERROR'}, str(exc)[:220])
                return {'CANCELLED'}
    class WZ09_PT_runtime_package(bpy.types.Panel):
        bl_idname = 'WZ09_PT_runtime_package'
        bl_label = 'Runtime package'
        bl_space_type = 'VIEW_3D'
        bl_region_type = 'UI'
        bl_category = 'WZ2009'
        bl_parent_id = 'WZ09_PT_main'
        @classmethod
        def poll(cls, ctx):
            return getattr(ctx.scene, 'wz09_workspace', '') == 'EXPORT'
        def draw(self, ctx):
            layout = self.layout
            layout.label(text='Export current terrain and GLB first', icon='INFO')
            layout.operator('wz09.export_runtime_package', text='Runtime package - 30 Hz', icon='EXPORT')
            status = ctx.scene.get('wz09_runtime_export_status')
            if status:
                layout.label(text=str(status)[:85])
    _CLASSES.extend((WZ09_OT_export_runtime_package, WZ09_PT_runtime_package))
    for cls in _CLASSES:
        bpy.utils.register_class(cls)


def unregister():
    import bpy
    for cls in reversed(_CLASSES):
        bpy.utils.unregister_class(cls)
    _CLASSES.clear()
