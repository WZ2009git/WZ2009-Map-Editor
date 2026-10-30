"""Readable map library and main-thread import adapters for Blender."""
import json
from pathlib import Path

import bpy
from bpy.props import BoolProperty, EnumProperty, StringProperty

from .authoring_helpers import resolve_owner_map
from .map_library import MAP_CODES, MAP_NAMES, OfflineMapQueue, map_items
from .source_bundle import verify_preview_bundle


_QUEUE = OfflineMapQueue()
_JOB_SCENE = None
_CATALOG_CACHE = {}


def _addon():
    import wz2009_map_editor
    return wz2009_map_editor


def _activate(ctx, obj):
    bpy.ops.object.select_all(action='DESELECT')
    obj.select_set(True)
    ctx.view_layer.objects.active = obj
    ctx.view_layer.update()


def imported_snapshot(scene):
    result = []
    loaded = set()
    for obj in scene.objects:
        if not is_map_preview_object(obj):
            continue
        owner = resolve_owner_map(obj)
        if owner is not None:
            loaded.add(str(owner.get('wz09_id', '')))
    for obj in scene.objects:
        if obj.get('wz09_type') == 'map_instance':
            result.append({'source_map': str(obj.get('wz09_source_map', '')),
                           'preview_loaded': str(obj.get('wz09_id', '')) in loaded})
    return result


def is_map_preview_object(obj):
    """Native visual data, excluding authoring helpers on an otherwise empty map."""
    return bool(obj.get('wz09_preview_only') and not any(obj.get(key) for key in (
        'wz09_type','wz09_asset_placement_id','wz09_audio_zone','wz09_audio_contour',
        'wz09_action_element','wz09_action_contour','wz09_ground_patch','wz09_region_kind',
        'wz09_water_json','wz09_editor_overlay')))


def _descendant(obj, parent):
    current, visited = getattr(obj, 'parent', None), set()
    while current is not None:
        key = current.as_pointer()
        if key in visited:
            raise ValueError('Cyclic object parent hierarchy')
        visited.add(key)
        if current == parent:
            return True
        current = current.parent
    return False


def import_map_preview(ctx, map_code, bundle, *, target=None, placement='CURSOR'):
    """Fill a matching proxy or create a new map with the existing glTF importer.

    Existing target IDs, placement, calibration, navigation and children remain
    untouched. A second preview is rejected. The native visual winding repair
    works only on this import, with copied meshes, never source files.
    """
    if map_code not in MAP_CODES:
        raise ValueError('Choose one of the sixteen original MW2009 maps')
    if placement not in {'CURSOR', 'ORIGIN'}:
        raise ValueError('Map placement must be CURSOR or ORIGIN')
    verify_preview_bundle(bundle)
    if target is not None:
        target = resolve_owner_map(target)
        if target is None or target.get('wz09_source_map') != map_code:
            raise ValueError('Selected map proxy belongs to another source map')
        if target.library or getattr(target, 'override_library', None):
            raise ValueError('Choose an editable local map proxy')
        if not any(obj == target for obj in ctx.scene.objects):
            raise ValueError('Map proxy must belong to the active scene')
        if any(is_map_preview_object(obj) and _descendant(obj, target) for obj in ctx.scene.objects):
            raise ValueError('This map already contains visual geometry; create a new instance instead')
    addon = _addon()
    created = target is None
    before = {obj.as_pointer() for obj in bpy.data.objects}
    old_selection = list(ctx.selected_objects)
    old_active = ctx.view_layer.objects.active
    old_provenance = target.get('wz09_map_provenance_json') if target is not None else None
    old_source = target.get('wz09_preview_source') if target is not None else None
    old_sha = target.get('wz09_preview_sha256') if target is not None else None
    try:
        if created:
            location = tuple(ctx.scene.cursor.location) if placement == 'CURSOR' else (0, 0, 0)
            target = addon.make_empty(ctx, dict(MAP_NAMES)[map_code], 'map_instance', 'map_instance', location)
            target['wz09_source_map'] = map_code
            target['wz09_source_scale'] = 0.0
        _activate(ctx, target)
        result = bpy.ops.wz09.import_visual_gltf(filepath=bundle['reference_path'])
        if result != {'FINISHED'}:
            raise ValueError('The existing glTF preview importer rejected this bundle')
        imported = [obj for obj in bpy.data.objects if obj.as_pointer() not in before and obj != target]
        if not imported:
            raise ValueError('Map import created no preview objects')
        from .preview_geometry import repair_preview_winding
        repair = repair_preview_winding(ctx.scene, objects=imported)
        _activate(ctx, target)
        ctx.scene['wz09_last_native_preview'] = str(Path(bundle['reference_path']).resolve())
        return {'map': target, 'objects': len(imported), 'repair': repair, 'created': created,
                'physical_scale_verified': False, 'collision_validated': False}
    except Exception:
        # Roll back only objects created during this operation. Original mesh
        # datablocks and source files are never modified or deleted.
        for obj in list(bpy.data.objects):
            if obj.as_pointer() not in before:
                bpy.data.objects.remove(obj, do_unlink=True)
        if not created and target is not None:
            for key, value in (('wz09_map_provenance_json', old_provenance),
                               ('wz09_preview_source', old_source), ('wz09_preview_sha256', old_sha)):
                if value is None:
                    if key in target:
                        del target[key]
                else:
                    target[key] = value
        bpy.ops.object.select_all(action='DESELECT')
        for obj in old_selection:
            if obj.name in ctx.view_layer.objects:
                obj.select_set(True)
        if old_active is not None and old_active.name in ctx.view_layer.objects:
            ctx.view_layer.objects.active = old_active
        raise


def _catalog(scene):
    raw = scene.get('wz09_map_library_json', '[]')
    key = scene.as_pointer()
    cached = _CATALOG_CACHE.get(key)
    if cached is not None and cached[0] == raw:
        return cached[1]
    try:
        records = json.loads(raw)
    except (TypeError, ValueError):
        return []
    result = records if isinstance(records, list) else []
    _CATALOG_CACHE[key] = (raw, result)
    return result


def _update_imported_counts(scene, records):
    """Refresh instance counts after an operation, never per viewport redraw."""
    counts = {}
    for record in imported_snapshot(scene):
        item = counts.setdefault(record['source_map'], [0, 0])
        item[0] += 1
        item[1] += int(record['preview_loaded'])
    for entry in records:
        entry['instances'], entry['imported_instances'] = counts.get(entry['code'], [0, 0])
    return records


def _timer():
    scene = bpy.data.scenes.get(_JOB_SCENE or '')
    if scene is None:
        _QUEUE.cancel()
    for event in _QUEUE.poll():
        if scene is None:
            continue
        kind = event['kind']
        if kind == 'catalog':
            scene['wz09_map_library_json'] = json.dumps(_update_imported_counts(scene, event['maps']), sort_keys=True)
        elif kind == 'finished':
            scene['wz09_map_job_message'] = ('Gestoppt' if event['cancelled'] else 'Fertig') + (
                f': {len(event["completed"])} exportiert, {len(event["reused"])} wiederverwendet, '
                f'{len(event["failed"])} fehlend/fehlerhaft')
        elif kind in {'error', 'failed', 'progress', 'cancel_requested'}:
            scene['wz09_map_job_message'] = event.get('message', '')
            if kind in {'error', 'failed'}:
                scene['wz09_map_job_error'] = event.get('message', '')
        elif kind == 'exported':
            scene['wz09_last_native_preview'] = event['bundle']['reference_path']
    for window in bpy.context.window_manager.windows:
        for area in window.screen.areas:
            if area.type == 'VIEW_3D':
                area.tag_redraw()
    return .3 if _QUEUE.running else None


def start_map_job(ctx, codes=()):
    global _JOB_SCENE
    if _QUEUE.running:
        raise ValueError('An offline library job is already running')
    root = _addon().project_root(ctx.scene)
    _JOB_SCENE = ctx.scene.name
    ctx.scene['wz09_map_job_message'] = 'Checking local sources and hashes ...'
    ctx.scene['wz09_map_job_error'] = ''
    _QUEUE.start(root, codes, ctx.scene.wz09_asset_root, imported_snapshot(ctx.scene))
    if not bpy.app.timers.is_registered(_timer):
        bpy.app.timers.register(_timer, first_interval=.1)


class WZ09_OT_map_refresh(bpy.types.Operator):
    bl_idname = 'wz09.map_library_refresh'
    bl_label = 'Check map library'
    bl_description = 'Read local FF files and existing hashed exports; do not start a converter'

    def execute(self, ctx):
        try:
            start_map_job(ctx)
            return {'FINISHED'}
        except (OSError, ValueError, RuntimeError) as exc:
            self.report({'ERROR'}, str(exc)[:240])
            return {'CANCELLED'}


class WZ09_OT_map_export(bpy.types.Operator):
    bl_idname = 'wz09.map_library_export'
    bl_label = 'Prepare map previews'
    bl_description = 'Convert explicitly selected maps offline; reuse existing verified exports'
    all_maps: BoolProperty(default=False)

    def execute(self, ctx):
        try:
            codes = tuple(code for code, _ in MAP_NAMES) if self.all_maps else (ctx.scene.wz09_library_map,)
            start_map_job(ctx, codes)
            return {'FINISHED'}
        except (OSError, ValueError, RuntimeError) as exc:
            self.report({'ERROR'}, str(exc)[:240])
            return {'CANCELLED'}


class WZ09_OT_map_cancel(bpy.types.Operator):
    bl_idname = 'wz09.map_library_cancel'
    bl_label = 'Stop after current map'
    bl_description = 'Let the current conversion finish; do not start further jobs or delete files'

    def execute(self, ctx):
        _QUEUE.cancel()
        return {'FINISHED'}


class WZ09_OT_map_import(bpy.types.Operator):
    bl_idname = 'wz09.map_library_import'
    bl_label = 'Load map'
    bl_description = 'Insert an existing real map preview; a matching empty proxy retains its position and ID'
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, ctx):
        try:
            target = None
            code = ctx.scene.wz09_library_map
            if ctx.scene.wz09_library_fill_proxy:
                target = resolve_owner_map(ctx.active_object)
                if target is None:
                    raise ValueError('Select a map proxy to use Fill selected proxy')
                code = str(target.get('wz09_source_map', ''))
            entry = next((entry for entry in _catalog(ctx.scene) if entry['code'] == code), None)
            if entry is None or not entry.get('bundle_available'):
                raise ValueError('Check the library and prepare this map offline first')
            result = import_map_preview(ctx, code, entry['bundle'], target=target,
                                        placement=ctx.scene.wz09_library_placement)
            ctx.scene['wz09_map_library_json'] = json.dumps(_update_imported_counts(ctx.scene, _catalog(ctx.scene)), sort_keys=True)
            ctx.scene['wz09_map_job_message'] = f"{entry['name']}: {result['objects']} preview objects loaded"
            self.report({'INFO'}, ctx.scene['wz09_map_job_message'])
            return {'FINISHED'}
        except (OSError, ValueError, RuntimeError) as exc:
            self.report({'ERROR'}, str(exc)[:240])
            return {'CANCELLED'}


class WZ09_PT_map_library(bpy.types.Panel):
    bl_label = 'Map library - MW2009'
    bl_idname = 'WZ09_PT_map_library'
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = 'WZ2009'
    bl_parent_id = 'WZ09_PT_main'

    @classmethod
    def poll(cls, ctx):
        return getattr(ctx.scene,'wz09_workspace','MAPS')=='MAPS'

    def draw(self, ctx):
        layout, scene = self.layout, ctx.scene
        row = layout.row(align=True)
        row.prop(scene, 'wz09_library_map', text='')
        row.operator('wz09.map_library_refresh', text='', icon='FILE_REFRESH')
        entry = next((entry for entry in _catalog(scene) if entry.get('code') == scene.wz09_library_map), None)
        if entry:
            layout.label(text=entry['region'], icon='WORLD')
            row = layout.row(align=True)
            row.label(text='Original source', icon='CHECKMARK' if entry['native_available'] else 'X')
            row.label(text='Export', icon='CHECKMARK' if entry['bundle_available'] else 'X')
            if entry.get('last_error'):
                layout.label(text=str(entry['last_error'])[:70], icon='ERROR')
        else:
            layout.label(text='Check once, then load a map.', icon='INFO')
        loading = layout.column(align=True)
        loading.enabled = not _QUEUE.running
        loading.prop(scene, 'wz09_library_fill_proxy')
        if scene.wz09_library_fill_proxy:
            owner = resolve_owner_map(ctx.active_object)
            code = str(owner.get('wz09_source_map', '')) if owner is not None else ''
            loading.label(text='Proxy: '+dict(MAP_NAMES).get(code, 'Select map'), icon='OUTLINER_OB_EMPTY')
        else:
            loading.prop(scene, 'wz09_library_placement', expand=True)
        loading.operator('wz09.map_library_import', icon='IMPORT')
        row = loading.row(align=True)
        row.operator('wz09.map_library_export', text='Prepare selection', icon='PACKAGE').all_maps = False
        row.operator('wz09.map_library_export', text='All 16', icon='PACKAGE').all_maps = True
        if _QUEUE.running:
            layout.operator('wz09.map_library_cancel', icon='CANCEL')
        if scene.get('wz09_map_job_message'):
            layout.label(text=str(scene['wz09_map_job_message'])[:85], icon='TIME' if _QUEUE.running else 'INFO')
        records = _catalog(scene)
        if records:
            ready = sum(entry.get('bundle_available', False) for entry in records)
            loaded = sum(entry.get('imported_instances', 0) for entry in records)
            layout.label(text=f'{ready}/16 Exporte bereit · {loaded} instances loaded')
        layout.label(text='Preview geometry; navigation and collision unverified.', icon='INFO')


CLASSES = (WZ09_OT_map_refresh, WZ09_OT_map_export, WZ09_OT_map_cancel,
           WZ09_OT_map_import, WZ09_PT_map_library)
PROPS = {
    'wz09_library_map': lambda: EnumProperty(name='Original map', items=map_items(), default='mp_rust'),
    'wz09_library_fill_proxy': lambda: BoolProperty(name='Fill selected proxy', default=False,
        description='Fill an empty proxy with geometry from the same map; retain placement, ID and navigation'),
    'wz09_library_placement': lambda: EnumProperty(name='Insert', items=[
        ('CURSOR', 'Am 3D-Cursor', 'Neue Map am aktuellen 3D-Cursor anlegen'),
        ('ORIGIN', 'Am Ursprung', 'Neue Map am Weltursprung anlegen')], default='CURSOR'),
}


def register():
    for key, factory in PROPS.items():
        setattr(bpy.types.Scene, key, factory())
    for cls in CLASSES:
        bpy.utils.register_class(cls)


def unregister():
    _QUEUE.cancel()
    _CATALOG_CACHE.clear()
    if bpy.app.timers.is_registered(_timer):
        bpy.app.timers.unregister(_timer)
    for cls in reversed(CLASSES):
        bpy.utils.unregister_class(cls)
    for key in PROPS:
        if hasattr(bpy.types.Scene, key):
            delattr(bpy.types.Scene, key)
