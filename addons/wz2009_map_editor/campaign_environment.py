"""Separate campaign scenery authoring; no Royale schema or mission emulation."""
import copy
import json
import re
import shutil
import tempfile
import uuid
from pathlib import Path

try:
    from .source_bundle import capture_gltf_bundle, sha256_file, verify_preview_bundle
except ImportError:
    from source_bundle import capture_gltf_bundle, sha256_file, verify_preview_bundle

FORMAT = 'wz09-campaign-environment-v1'
COLLISION_FORMAT = 'wz09-campaign-environment-v2'
_HASH = re.compile(r'^[0-9a-f]{64}$')
_CLASSES = []


def validate_metadata(document):
    if not isinstance(document, dict) or document.get('format') not in {FORMAT, COLLISION_FORMAT}:
        raise ValueError('Campaign environment v1 or v2 required')
    if document.get('source_zone') != 'iw4:gulag' or not _HASH.fullmatch(str(document.get('source_sha256', ''))):
        raise ValueError('Original iw4:gulag source hash required')
    if document.get('coordinate_system') != 'meters-Y-up':
        raise ValueError('glTF meters/Y-up required; Blender converts to Z-up')
    for field in ('runtime_integrated', 'mission_logic_supported'):
        if document.get(field) is not False:
            raise ValueError(field + ': campaign runtime/collision support is not implemented')
    if document['format'] == FORMAT:
        if document.get('native_collision_exported') is not False:
            raise ValueError('native_collision_exported: v1 has no collision sidecar')
    else:
        if document.get('native_collision_exported') is not True:
            raise ValueError('native_collision_exported: v2 requires the source sidecar')
        collision = document.get('collision')
        if (not isinstance(collision, dict) or set(collision) != {'path', 'format', 'coordinate_system'}
                or collision['format'] != 'wz09-native-collision-v1'
                or collision['coordinate_system'] != 'iw4-inches-Z-up'):
            raise ValueError('Native collision sidecar descriptor/coordinate system required')
        if document.get('collision_status') not in {'original_source', 'requires_rebuild'}:
            raise ValueError('Native collision status required')
        if document.get('edited') and document['collision_status'] != 'requires_rebuild':
            raise ValueError('Edited scenery requires collision rebuild; original collision cannot be claimed current')
    return document


def _local(base, value):
    if not isinstance(value, str) or not value or '\\' in value or ':' in value:
        raise ValueError('Local relative campaign resource path required')
    path = (base / value).resolve()
    if Path(value).is_absolute() or not path.is_relative_to(base) or path == base:
        raise ValueError('Campaign resource escapes its package')
    return path


def load_manifest(path):
    path = Path(path).resolve()
    if path.stat().st_size > 8 * 1024 * 1024:
        raise ValueError('Campaign manifest exceeds 8 MiB')
    document = validate_metadata(json.loads(path.read_text(encoding='utf-8')))
    records = document.get('files')
    if not isinstance(records, list) or not records or len(records) > 50000:
        raise ValueError('Campaign file bindings required')
    base = path.parent
    bound = {}
    for record in records:
        if not isinstance(record, dict) or set(record) != {'path', 'sha256'}:
            raise ValueError('Campaign file record requires path and sha256')
        target = _local(base, record['path'])
        if target in bound or not _HASH.fullmatch(str(record['sha256'])):
            raise ValueError('Duplicate resource or invalid campaign hash')
        if sha256_file(target) != record['sha256']:
            raise ValueError('Campaign resource changed: ' + record['path'])
        bound[target] = record['sha256']
    scene = _local(base, document.get('scene'))
    if scene not in bound:
        raise ValueError('Campaign scene is not hash-bound')
    if document['format'] == COLLISION_FORMAT:
        collision = _local(base, document['collision']['path'])
        if collision not in bound or collision == scene:
            raise ValueError('Native collision sidecar is not independently hash-bound')
    bundle = dict(reference_path=str(scene), reference_sha256=bound[scene],
                  resources=[dict(path=str(p), sha256=h) for p, h in bound.items() if p != scene])
    verify_preview_bundle(bundle)
    return document, scene


def bind_edited_scene(source, scene, *, source_base=None):
    """Bind a newly exported scene without asserting regenerated native collision."""
    validate_metadata(source)
    scene = Path(scene).resolve()
    bundle = capture_gltf_bundle(scene, include_images=True)
    result = copy.deepcopy(source)
    result.update(scene=scene.name, edited=True, environment_id=source.get('environment_id') or str(uuid.uuid4()))
    result['files'] = [dict(path=scene.name, sha256=bundle['reference_sha256'])]
    for record in bundle['resources']:
        result['files'].append(dict(path=Path(record['path']).resolve().relative_to(scene.parent).as_posix(), sha256=record['sha256']))
    if source['format'] == COLLISION_FORMAT:
        if source_base is None:
            raise ValueError('Original native collision source directory required')
        base = Path(source_base).resolve()
        original = _local(base, source['collision']['path'])
        records = [r for r in source.get('files', []) if _local(base, r['path']) == original]
        if len(records) != 1 or sha256_file(original) != records[0]['sha256']:
            raise ValueError('Original native collision source changed or is not bound')
        relative = 'collision/' + records[0]['sha256'] + '.json'
        target = scene.parent / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            raise ValueError('Collision sidecar output already exists; use a new version')
        shutil.copyfile(original, target)
        if sha256_file(target) != records[0]['sha256']:
            raise ValueError('Collision source changed while copying')
        result['collision']['path'] = relative
        result['collision_status'] = 'requires_rebuild'
        result['files'].append(dict(path=relative, sha256=records[0]['sha256']))
    result.pop('counts', None)  # Native import census is stale after author edits.
    result['source_export_counts'] = source.get('source_export_counts', source.get('counts', {}))
    result['limitations'] = list(dict.fromkeys(source.get('limitations', []) + [
        'Edited environment only: collision/navigation must be rebuilt before runtime integration']))
    return result


def import_environment(ctx, path):
    import bpy
    document, scene = load_manifest(path)
    before = set(bpy.data.objects)
    collection = bpy.data.collections.new('WZ09 Campaign Gulag')
    ctx.scene.collection.children.link(collection)
    try:
        bpy.ops.import_scene.gltf(filepath=str(scene))
        imported = set(bpy.data.objects) - before
        if not imported or not any(o.type == 'MESH' for o in imported):
            raise ValueError('Campaign importer produced no mesh')
        seen = set()
        for obj in imported:
            persistent = str(obj.get('wz09_campaign_object_id', '')) or str(uuid.uuid4())
            uuid.UUID(persistent)
            if persistent in seen:
                raise ValueError('Duplicate campaign object ID')
            seen.add(persistent)
            obj['wz09_campaign_object_id'] = persistent
            obj['wz09_campaign_environment'] = True
            for old in list(obj.users_collection):
                old.objects.unlink(obj)
            collection.objects.link(obj)
            obj.select_set(False)
        ctx.view_layer.objects.active = None
        document.setdefault('environment_id', str(uuid.uuid4()))
        collection['wz09_campaign_manifest'] = json.dumps(document, sort_keys=True)
        collection['wz09_campaign_source_manifest'] = str(Path(path).resolve())
        return collection
    except Exception:
        for obj in set(bpy.data.objects) - before:
            bpy.data.objects.remove(obj, do_unlink=True)
        bpy.data.collections.remove(collection)
        raise


def export_environment(ctx, collection, output):
    import bpy
    source = validate_metadata(json.loads(collection['wz09_campaign_manifest']))
    objects = list(collection.all_objects)
    if not objects or not any(o.type == 'MESH' for o in objects):
        raise ValueError('Campaign collection has no meshes')
    ids = [str(o.get('wz09_campaign_object_id', '')) for o in objects]
    if len(ids) != len(set(ids)) or any(not value for value in ids):
        raise ValueError('Campaign objects require unique persistent IDs')
    for value in ids:
        uuid.UUID(value)
    output = Path(output).resolve()
    output.mkdir(parents=True, exist_ok=True)
    folder = Path(tempfile.mkdtemp(prefix='gulag-edit-', dir=output))
    scene = folder / 'scene.glb'
    selected = list(ctx.selected_objects)
    active = ctx.view_layer.objects.active
    try:
        bpy.ops.object.select_all(action='DESELECT')
        for obj in objects:
            obj.select_set(True)
        bpy.ops.export_scene.gltf(filepath=str(scene), export_format='GLB', use_selection=True,
                                  export_extras=True, export_yup=True)
        source_base = Path(collection['wz09_campaign_source_manifest']).resolve().parent
        document = bind_edited_scene(source, scene, source_base=source_base)
        manifest = folder / 'campaign.json'
        manifest.write_text(json.dumps(document, indent=2), encoding='utf-8')
        load_manifest(manifest)
        return manifest
    finally:
        bpy.ops.object.select_all(action='DESELECT')
        for obj in selected:
            obj.select_set(True)
        ctx.view_layer.objects.active = active


def local_gulag_manifest():
    """Resolve an existing local export; never invent an original-asset path."""
    base = Path(__file__).resolve().parents[2] / 'exports' / 'campaign'
    candidates = sorted(base.glob('exports/gulag-campaign/*/campaign.json'),
                        key=lambda p: p.stat().st_mtime, reverse=True)
    if not candidates:
        raise ValueError('No local Gulag export found; import a verified campaign.json first')
    # Validate the selected version; a broken newest package is an error, not a silent fallback.
    load_manifest(candidates[0])
    return candidates[0]


def activate_campaign(ctx, scene):
    from mathutils import Vector
    window = ctx.window
    if window.scene != scene:
        views = []
        for index, area in enumerate(window.screen.areas):
            if area.type == 'VIEW_3D':
                view = area.spaces.active.region_3d
                views.append(dict(index=index, location=list(view.view_location),
                                  rotation=list(view.view_rotation), distance=view.view_distance,
                                  clip_end=area.spaces.active.clip_end))
        scene['wz09_royale_view_json'] = json.dumps(views)
    window.scene = scene
    mesh = max((o for o in scene.objects if o.type == 'MESH'),
               key=lambda o: len(o.data.polygons), default=None)
    if mesh:
        center = mesh.matrix_world @ (sum((Vector(v) for v in mesh.bound_box), Vector()) / 8)
        distance = max(50, max(mesh.dimensions) * 1.3)
        # Distant backdrop mountains dominate World bounds. Frame the actual
        # authored prop cluster for editing, while retaining the entire scene.
        positions = [o.matrix_world.translation for o in scene.objects
                     if o.type == 'MESH' and o.get('iw4l') and 'authored_slot' in o['iw4l']]
        if len(positions) >= 20:
            bounds = []
            for axis in range(3):
                values = sorted(p[axis] for p in positions)
                bounds.append((values[len(values)//20], values[min(len(values)-1,len(values)*19//20)]))
            center = Vector([(low+high)/2 for low, high in bounds])
            distance = max(75, max(high-low for low, high in bounds) * 1.5)
        for area in window.screen.areas:
            if area.type == 'VIEW_3D':
                view = area.spaces.active.region_3d
                view.view_location = center
                view.view_distance = distance
                area.spaces.active.clip_end = max(area.spaces.active.clip_end, distance*4, max(mesh.dimensions)*2)


def import_separate_scene(ctx, path):
    import bpy
    if not ctx.window:
        raise ValueError('A Blender window is required for a separate campaign scene')
    original = ctx.window.scene
    resolved = str(Path(path).resolve())
    existing = next((s for s in bpy.data.scenes if s.get('wz09_gulag_manifest') == resolved), None)
    if existing:
        activate_campaign(ctx, existing)
        return existing
    load_manifest(path)  # Fail before creating/switching scene on missing or changed data.
    scene = bpy.data.scenes.new('Gulag - separate campaign environment')
    ctx.window.scene = scene
    try:
        collection = import_environment(bpy.context, path)
        scene['wz09_gulag_manifest'] = resolved
        scene['wz09_royale_scene_name'] = original.name
        if hasattr(scene, 'wz09_workspace'):
            scene.wz09_workspace = 'MAPS'
        scene['wz09_campaign_runtime_integrated'] = False
        # Stable scene identity supports renaming without losing the return target.
        if not original.get('wz09_scene_id'):
            original['wz09_scene_id'] = str(uuid.uuid4())
        scene['wz09_royale_scene_id'] = original['wz09_scene_id']
        # Re-enter through the same UI path used for a cached import.
        ctx.window.scene = original
        activate_campaign(ctx, scene)
        return scene
    except Exception:
        ctx.window.scene = original
        bpy.data.scenes.remove(scene)
        raise


def register():
    import bpy
    from bpy.props import StringProperty
    from bpy_extras.io_utils import ImportHelper, ExportHelper

    class WZ09_OT_campaign_import(bpy.types.Operator, ImportHelper):
        bl_idname = 'wz09.campaign_import'
        bl_label = 'Import Gulag campaign environment'
        bl_options = {'REGISTER', 'UNDO'}
        filename_ext = '.json'
        filter_glob: StringProperty(default='*.json', options={'HIDDEN'})

        def execute(self, ctx):
            try:
                scene = import_separate_scene(ctx, self.filepath)
                self.report({'INFO'}, f'{len(scene.objects)} campaign objects in separate scene; mission runtime pending')
                return {'FINISHED'}
            except (OSError, ValueError, RuntimeError, KeyError) as exc:
                self.report({'ERROR'}, str(exc)[:220])
                return {'CANCELLED'}

    class WZ09_OT_campaign_export(bpy.types.Operator, ExportHelper):
        bl_idname = 'wz09.campaign_export'
        bl_label = 'Export edited campaign environment'
        filename_ext = '.json'

        def execute(self, ctx):
            try:
                candidates = [c for c in bpy.data.collections if c.get('wz09_campaign_manifest')
                              and ctx.active_object and ctx.active_object.name in c.all_objects]
                if len(candidates) != 1:
                    raise ValueError('Select an object in exactly one campaign environment collection')
                manifest = export_environment(ctx, candidates[0], Path(self.filepath).parent)
                self.report({'INFO'}, 'New environment version: ' + str(manifest))
                return {'FINISHED'}
            except (OSError, ValueError, RuntimeError, KeyError) as exc:
                self.report({'ERROR'}, str(exc)[:220])
                return {'CANCELLED'}

    class WZ09_OT_campaign_open_local(bpy.types.Operator):
        bl_idname = 'wz09.campaign_open_local'
        bl_label = 'Open local Gulag environment'
        def execute(self, ctx):
            try:
                scene = import_separate_scene(ctx, local_gulag_manifest())
                self.report({'INFO'}, f'{len(scene.objects)} campaign objects; mission runtime pending')
                return {'FINISHED'}
            except (OSError, ValueError, RuntimeError, KeyError) as exc:
                self.report({'ERROR'}, str(exc)[:220]); return {'CANCELLED'}

    class WZ09_OT_campaign_return_royale(bpy.types.Operator):
        bl_idname = 'wz09.campaign_return_royale'
        bl_label = 'Return to Karvorsk'
        def execute(self, ctx):
            identity = ctx.scene.get('wz09_royale_scene_id')
            scene = next((s for s in bpy.data.scenes if identity and s.get('wz09_scene_id') == identity), None)
            if scene is None:
                self.report({'ERROR'}, 'Original Karvorsk scene no longer exists'); return {'CANCELLED'}
            views = json.loads(ctx.scene.get('wz09_royale_view_json', '[]'))
            ctx.window.scene = scene
            for row in views:
                index = row['index']
                if index < len(ctx.window.screen.areas):
                    area = ctx.window.screen.areas[index]
                    if area.type == 'VIEW_3D':
                        view = area.spaces.active.region_3d
                        view.view_location = row['location']
                        view.view_rotation = row['rotation']
                        view.view_distance = row['distance']
                        area.spaces.active.clip_end = row.get('clip_end', area.spaces.active.clip_end)
            return {'FINISHED'}

    class WZ09_PT_campaign(bpy.types.Panel):
        bl_idname = 'WZ09_PT_campaign'
        bl_label = 'Campaign environment'
        bl_space_type = 'VIEW_3D'
        bl_region_type = 'UI'
        bl_category = 'WZ2009'
        bl_parent_id = 'WZ09_PT_main'

        def draw(self, ctx):
            self.layout.operator('wz09.campaign_open_local')
            self.layout.operator('wz09.campaign_import')
            self.layout.operator('wz09.campaign_export')
            self.layout.label(text='Scenery editing; mission runtime pending', icon='INFO')

    _CLASSES.extend((WZ09_OT_campaign_import, WZ09_OT_campaign_export,
                     WZ09_OT_campaign_open_local, WZ09_OT_campaign_return_royale, WZ09_PT_campaign))
    for cls in _CLASSES:
        bpy.utils.register_class(cls)


def unregister():
    import bpy
    for cls in reversed(_CLASSES):
        bpy.utils.unregister_class(cls)
    _CLASSES.clear()
