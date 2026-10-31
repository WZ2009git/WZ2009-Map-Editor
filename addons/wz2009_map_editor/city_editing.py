"""Reversible city selections and reference-only edit drafts, never game-ready patches.

Face IDs identify the retained Blender source, NOT original IW4 BSP surfaces.
The runtime must acquire the native surface mapping before consuming city parts.
"""
import hashlib
import json
import math
import uuid
from array import array
from pathlib import Path

REGISTRY = 'wz09_city_edit_registry_json'
SOURCE = 'wz09_city_edit_source_id'
PART = 'wz09_city_edit_part_id'
FACE = 'wz09_city_source_face'
FORMAT = 'wz09-native-edit-draft-v1'
_CLASSES = []


def registry(scene):
    return json.loads(scene.get(REGISTRY, '{"meshes":{},"parts":{},"models":{}}'))


def _write(scene, records):
    scene[REGISTRY] = json.dumps(records, sort_keys=True)


def retained_baselines(scene,identity):
    """Resolve undo baselines in this resident map, not another scene's cache."""
    import bpy
    meshes=[m for m in bpy.data.meshes if m.get('wz09_city_baseline_id')==identity]
    entry=registry(scene)['meshes'].get(identity)
    if entry:
        from . import map_residency
        tokens={c.get(map_residency.TOKEN) for c in getattr(getattr(scene,'collection',None),'children_recursive',())
                if c.get(map_residency.OWNER)==entry['owner_map'] and c.get(map_residency.LAYER)=='core' and c.get(map_residency.TOKEN)}
        scoped=[m for m in meshes if m.get(map_residency.TOKEN) in tokens]
        if tokens:return scoped
    return meshes


def map_root(obj):
    while obj is not None:
        if obj.get('wz09_type') == 'map_instance':
            return obj
        obj = obj.parent
    raise ValueError('Select a city or one of its original objects')


def source_binding(root):
    from .original_assets import source_requirement
    registration = json.loads(root.get('wz09_calibration_json', '{}')).get('registration', {})
    return source_requirement(root['wz09_source_map'], registration.get('native_source_sha256', ''))


def mesh_digest(mesh):
    """Geometry, UVs and material indices; object transforms are intentionally separate."""
    from .material_palette import fingerprint_layout
    palette,material_indices=fingerprint_layout(mesh)
    digest = hashlib.sha256()
    for items, prop, width, kind in ((mesh.vertices, 'co', 3, 'f'),
                                    (mesh.loops, 'vertex_index', 1, 'i'),
                                    (mesh.polygons, 'loop_total', 1, 'i'),
                                    (mesh.polygons, 'material_index', 1, 'i')):
        values = array(kind, [0]) * (len(items) * width)
        items.foreach_get(prop, values)
        if prop=='material_index' and material_indices is not None:
            if any(index<0 or index>=len(material_indices) for index in values):raise ValueError('Invalid compact material index')
            values=array('i',(material_indices[index] for index in values))
        digest.update(values.tobytes())
    for uv in mesh.uv_layers:
        values = array('f', [0]) * (len(uv.data) * 2)
        uv.data.foreach_get('uv', values)
        digest.update(values.tobytes())
    face_ids = mesh.attributes.get(FACE)
    if face_ids:
        values = array('i', [0]) * len(face_ids.data)
        face_ids.data.foreach_get('value', values)
        digest.update(values.tobytes())
    digest.update(json.dumps(palette).encode())
    return digest.hexdigest()


def _face_ids(mesh, count):
    attribute = mesh.attributes.get(FACE)
    if attribute is None or attribute.domain != 'FACE' or attribute.data_type != 'INT':
        raise ValueError('Missing retained Blender face identities')
    values = [row.value for row in attribute.data]
    if len(values) != len(set(values)) or any(value < 0 or value >= count for value in values):
        raise ValueError('Duplicated or invalid retained face identities')
    return set(values)


def _partition(source, entry, records):
    count = entry['baseline_face_count']
    covered = _face_ids(source.data, count)
    for row in records['parts'].values():
        if row['source_mesh_id'] != entry['id']:
            continue
        ids = set(row['source_face_ids'])
        if len(ids) != len(row['source_face_ids']) or covered.intersection(ids):
            raise ValueError('Overlapping retained face identities')
        covered.update(ids)
    if covered != set(range(count)):
        raise ValueError('Incomplete retained source face partition')


def _matrix(matrix):
    result = [[float(v) for v in row] for row in matrix]
    if not all(math.isfinite(v) for row in result for v in row):
        raise ValueError('Edit transform contains a non-finite value')
    if matrix.determinant() <= 1e-12:
        raise ValueError('Reflected or singular edit transforms are unsupported')
    return result


def begin_mesh_edit(ctx):
    import bpy
    import bmesh
    from . import overview_preview
    if ctx.mode != 'OBJECT':
        raise ValueError('Return to Object Mode first')
    root = map_root(ctx.active_object)
    values=json.loads(ctx.scene.get('wz09_city_view_states_json','{}'))
    values[str(root['wz09_id'])]='loaded';ctx.scene['wz09_city_view_states_json']=json.dumps(values)
    overview_preview.set_focus(ctx.scene, str(root['wz09_id']))
    candidates = [o for o in root.children_recursive if o.type == 'MESH'
                  and o.get('wz09_preview_only') and not o.get('wz09_editor_overlay')
                  and not o.get(PART) and not o.modifiers
                  and not o.get('wz09_compact_records_json')]
    # The native World is the large, uninstanced city mesh. Never use the overview.
    source = ctx.active_object
    from . import map_parts,map_residency
    resident=map_residency.resident(ctx.scene,root)
    if resident is not None and map_parts.ready(resident):
        if source.type!='MESH' or source.get('wz09_editor_overlay') or source.modifiers or not source.data.polygons:
            raise ValueError('Select an individual mesh part or prop before editing faces')
        candidates.append(source)
    if source not in candidates:
        if not candidates:
            raise ValueError('No editable city mesh found; restore a prop batch to edit props')
        source = max(candidates, key=lambda o: len(o.data.polygons))
    map_parts.make_asset_mesh_unique(source)
    bpy.ops.object.select_all(action='DESELECT')
    source.hide_set(False)
    source.select_set(True)
    ctx.view_layer.objects.active = source
    bpy.ops.object.mode_set(mode='EDIT')
    ctx.tool_settings.mesh_select_mode = (False, False, True)
    bm = bmesh.from_edit_mesh(source.data)
    bm.faces.index_update()
    for face in bm.faces:
        face.select_set(False)
    bmesh.update_edit_mesh(source.data)
    return source


def _subset(mesh, face_ids, keep=True):
    import bpy
    import bmesh
    bm = bmesh.new()
    try:
        bm.from_mesh(mesh)
        layer = bm.faces.layers.int.get(FACE)
        if layer is None:
            raise ValueError('Missing retained Blender face identities')
        remove = [face for face in bm.faces if (face[layer] in face_ids) != keep]
        bmesh.ops.delete(bm, geom=remove, context='FACES')
        isolated = [v for v in bm.verts if not v.link_faces]
        if isolated:
            bmesh.ops.delete(bm, geom=isolated, context='VERTS')
        result = bpy.data.meshes.new('City edit geometry')
        for material in mesh.materials:
            result.materials.append(material)
        bm.to_mesh(result)
        result.update()
        return result
    finally:
        bm.free()


def extract_selected(ctx):
    import bpy
    import bmesh
    from mathutils import Vector
    source = ctx.active_object
    if ctx.mode != 'EDIT_MESH' or len(ctx.objects_in_mode) != 1:
        raise ValueError('Edit one city mesh, select faces, then extract the selection')
    if source.get(PART) or source.get('wz09_editor_overlay') or source.modifiers:
        raise ValueError('Extract from the original city mesh, without modifiers')
    root = map_root(source)
    binding = source_binding(root)  # Validate original FF identity before mutation.
    bm = bmesh.from_edit_mesh(source.data)
    bm.faces.index_update()
    selected = [face.index for face in bm.faces if face.select]
    if not selected:
        raise ValueError('Select building faces first: B for a box or L for linked geometry')
    bpy.ops.object.mode_set(mode='OBJECT')
    records = registry(ctx.scene)
    uid = source.get(SOURCE)
    baseline = None
    working = None
    if uid:
        entry = records['meshes'][uid]
        if mesh_digest(source.data) != entry['remaining_geometry_sha256']:
            raise ValueError('City mesh changed outside tracked edits; preserve it in the .blend before exporting')
        _partition(source, entry, records)
        working = source.data
    else:
        uid = str(uuid.uuid4())
        working = source.data.copy()
        attribute = working.attributes.get(FACE) or working.attributes.new(FACE, 'INT', 'FACE')
        attribute.data.foreach_set('value', array('i', range(len(working.polygons))))
        baseline = working.copy()
        baseline.use_fake_user = True
        baseline['wz09_city_baseline_id'] = uid
        entry = dict(id=uid, owner_map=str(root['wz09_id']), source=binding,
                     preview_sha256=str(root.get('wz09_preview_sha256', '')),
                     baseline_geometry_sha256=mesh_digest(baseline),
                     native_surface_mapping='missing', baseline_face_count=len(baseline.polygons))
        records['meshes'][uid] = entry
    face_ids = {working.attributes[FACE].data[i].value for i in selected}
    part_mesh = remaining = None
    try:
        part_mesh = _subset(working, face_ids)
        remaining = _subset(working, face_ids, keep=False)
    except Exception:
        for mesh in (part_mesh, remaining, baseline, working if baseline else None):
            if mesh is not None and mesh.users == 0:
                bpy.data.meshes.remove(mesh)
        if baseline is not None and baseline.name in bpy.data.meshes:
            bpy.data.meshes.remove(baseline)
        raise
    center = sum((v.co for v in part_mesh.vertices), Vector()) / len(part_mesh.vertices)
    for vertex in part_mesh.vertices:
        vertex.co -= center
    part_mesh.update()
    part_id = str(uuid.uuid4())
    part = bpy.data.objects.new('City part - move with G', part_mesh)
    for collection in source.users_collection:
        collection.objects.link(part)
    part.parent = source
    part.location = center
    part[PART] = part_id
    part['wz09_preview_only'] = True
    part['wz09_city_part_source'] = uid
    records['parts'][part_id] = dict(id=part_id, source_mesh_id=uid,
                                    source_face_ids=sorted(face_ids),
                                    source_origin_m=list(center),
                                    geometry_sha256=mesh_digest(part_mesh))
    source.data = remaining
    source[SOURCE] = uid
    if baseline is not None:
        bpy.data.meshes.remove(working)
    entry['remaining_geometry_sha256'] = mesh_digest(remaining)
    _write(ctx.scene, records)
    bpy.ops.object.select_all(action='DESELECT')
    part.select_set(True)
    ctx.view_layer.objects.active = part
    ctx.scene['wz09_city_overview_stale'] = True
    return part


def restore_city(ctx):
    import bpy
    root = map_root(ctx.active_object)
    records = registry(ctx.scene)
    targets = [uid for uid, row in records['meshes'].items() if row['owner_map'] == root['wz09_id']]
    for uid in targets:
        sources = [o for o in ctx.scene.objects if o.get(SOURCE) == uid]
        baselines = retained_baselines(ctx.scene,uid)
        if len(sources) != 1 or len(baselines) != 1:
            raise ValueError('Retained source or baseline missing/duplicated; restore from the saved .blend')
    for uid in targets:
        source = next(o for o in ctx.scene.objects if o.get(SOURCE) == uid)
        baseline = retained_baselines(ctx.scene,uid)[0]
        source.data = baseline.copy()
        del source.data['wz09_city_baseline_id']
        del source[SOURCE]
        for part_id, part in list(records['parts'].items()):
            if part['source_mesh_id'] == uid:
                for obj in list(ctx.scene.objects):
                    if obj.get(PART) == part_id:
                        bpy.data.objects.remove(obj, do_unlink=True)
                del records['parts'][part_id]
        baseline.use_fake_user = False
        bpy.data.meshes.remove(baseline)
        del records['meshes'][uid]
    _write(ctx.scene, records)
    if targets:
        from . import city_view_cache,overview_preview
        values=city_view_cache.states(ctx.scene);values[str(root['wz09_id'])]='loaded'
        ctx.scene[city_view_cache.STATES]=json.dumps(values)
        overview_preview.set_focus(ctx.scene,str(root['wz09_id']))
    return len(targets)


def track_model(ctx):
    from .native_prop_edit import capture_model, verify_model
    obj = ctx.active_object
    while obj is not None:
        metadata = obj.get('iw4l')
        if metadata and 'authored_slot' in metadata and 'model' in metadata:
            break
        obj = obj.parent
    if obj is None:
        raise ValueError('Select an original prop with an authored slot; restore its compact batch first')
    root = map_root(obj)
    binding = source_binding(root)
    if obj.get('wz09_compacted_source_parts'):
        from .compact_preview import restore_model
        restore_model(obj)
    slot = int(metadata['authored_slot'])
    if slot < 0:
        raise ValueError('Invalid original model slot')
    uid = str(uuid.uuid5(uuid.NAMESPACE_URL, f"{root['wz09_id']}:{binding['native_sha256']}:{slot}"))
    records = registry(ctx.scene)
    if uid in records['models']:
        verify_model(obj, records['models'][uid]['hierarchy'], mesh_digest)
        return obj
    records['models'][uid] = dict(id=uid, owner_map=str(root['wz09_id']), source=binding,
                                  authored_slot=slot, expected_model=str(metadata['model']),
                                  hierarchy=capture_model(obj, mesh_digest))
    obj['wz09_tracked_native_model'] = uid
    _write(ctx.scene, records)
    ctx.scene['wz09_city_overview_stale'] = True
    return obj


def build_draft(scene):
    from mathutils import Matrix, Vector
    from .native_prop_edit import verify_model
    records = registry(scene)
    roots = {str(o['wz09_id']): o for o in scene.objects if o.get('wz09_type') == 'map_instance'}
    if len(roots) != sum(o.get('wz09_type') == 'map_instance' for o in scene.objects):
        raise ValueError('Duplicated map instance identity')
    sources, meshes, parts, models = {}, [], [], []
    objects = list(scene.objects)
    def unique(key, uid, optional=False):
        found = [o for o in objects if o.get(key) == uid]
        if len(found) > 1 or (not optional and not found):
            raise ValueError('Missing/duplicated edit identity; duplicated parts require new identities')
        return found[0] if found else None
    for uid, row in records['meshes'].items():
        obj = unique(SOURCE, uid)
        root = roots[row['owner_map']]
        if map_root(obj) != root or mesh_digest(obj.data) != row['remaining_geometry_sha256']:
            raise ValueError('Untracked city geometry/hierarchy change; draft export refused')
        _partition(obj, row, records)
        sources[row['source']['source_map']] = row['source']
        # Internal validation fingerprints are harmless metadata, never proprietary geometry.
        meshes.append(dict(row, map_local_matrix=_matrix(root.matrix_world.inverted() @ obj.matrix_world)))
    for uid, row in records['parts'].items():
        obj = unique(PART, uid, optional=True)
        if obj:
            source = unique(SOURCE, row['source_mesh_id'])
            if obj.parent != source or mesh_digest(obj.data) != row['geometry_sha256']:
                raise ValueError('Part geometry/hierarchy changed; only object transforms can be reference-only')
            if _face_ids(obj.data, records['meshes'][row['source_mesh_id']]['baseline_face_count']) != set(row['source_face_ids']):
                raise ValueError('Part source face identity mismatch')
        parts.append(dict(row, state='placed' if obj else 'removed',
                          source_local_delta=_matrix(obj.matrix_basis @ Matrix.Translation(-Vector(row['source_origin_m']))) if obj else None))
    for uid, row in records['models'].items():
        obj = unique('wz09_tracked_native_model', uid, optional=True)
        root = roots[row['owner_map']]
        if obj and (map_root(obj) != root or obj.get('iw4l', {}).get('authored_slot') != row['authored_slot']
                    or obj.get('iw4l', {}).get('model') != row['expected_model']):
            raise ValueError('Original prop binding/hierarchy changed')
        if obj:
            verify_model(obj, row['hierarchy'], mesh_digest)
        sources[row['source']['source_map']] = row['source']
        portable_row = {key: value for key, value in row.items() if key != 'hierarchy'}
        models.append(dict(portable_row, state='placed' if obj else 'removed',
                           map_local_matrix=_matrix(root.matrix_world.inverted() @ obj.matrix_world) if obj else None))
    return dict(format=FORMAT, runtime_integrated=False, collision_status='requires_rebuild',
                coordinate_system='Blender-Z-up-meters', sources=list(sources.values()),
                city_meshes=meshes, city_parts=parts, original_models=models,
                limitations=['City face IDs refer to retained Blender geometry, not native BSP surfaces',
                             'Runtime visual, collision, navigation and replication consumers are pending'])


def export_draft(scene, path):
    document = build_draft(scene)
    if not document['city_meshes'] and not document['original_models']:
        raise ValueError('No tracked edits; extract city faces or track an original prop first')
    # Exclusive creation: existing drafts and original sources are never overwritten.
    with Path(path).open('x', encoding='utf-8') as stream:
        json.dump(document, stream, indent=2)
    return document


def register():
    import bpy
    from bpy_extras.io_utils import ExportHelper

    class WZ09_OT_city_split_loose(bpy.types.Operator):
        bl_idname = 'wz09.city_split_loose'
        bl_label = 'Split into connected parts'
        bl_description = 'Split the selected mesh into connected components while preserving UV seams; the original can be restored'
        bl_options = {'REGISTER', 'UNDO'}
        @classmethod
        def poll(cls, ctx):
            if ctx.mode != 'OBJECT' or ctx.active_object is None:
                return False
            try:
                map_root(ctx.active_object);return True
            except ValueError:
                return False
        def execute(self, ctx):
            from .city_loose_parts import split_city
            try:
                if ctx.window:ctx.window.cursor_set('WAIT')
                count=split_city(ctx)
                self.report({'INFO'},f'{count} parts created. Click a part, G to move, X to delete. Original retained.')
                return {'FINISHED'}
            except (ValueError,RuntimeError,KeyError) as exc:
                self.report({'ERROR'},str(exc)[:220]);return {'CANCELLED'}
            finally:
                if ctx.window:ctx.window.cursor_set('DEFAULT')

        def invoke(self,ctx,event):
            from .city_loose_parts import split_city_steps
            self._steps=split_city_steps(ctx)
            self._timer=ctx.window_manager.event_timer_add(.05,window=ctx.window)
            ctx.window_manager.progress_begin(0,100)
            ctx.window_manager.modal_handler_add(self)
            return {'RUNNING_MODAL'}

        def _finish(self,ctx):
            ctx.window_manager.event_timer_remove(self._timer)
            ctx.window_manager.progress_end()
            ctx.workspace.status_text_set(None)
            for area in ctx.window.screen.areas:area.tag_redraw()

        def modal(self,ctx,event):
            import time
            if event.type=='ESC':
                self._steps.close();self._finish(ctx)
                self.report({'INFO'},'Split cancelled; original unchanged')
                return {'CANCELLED'}
            if event.type!='TIMER':
                return {'PASS_THROUGH'} if event.type in {'MIDDLEMOUSE','MOUSEMOVE','WHEELUPMOUSE','WHEELDOWNMOUSE'} else {'RUNNING_MODAL'}
            deadline=time.perf_counter()+.018
            try:
                while time.perf_counter()<deadline:
                    done,total=next(self._steps)
                    ctx.window_manager.progress_update(done*100/max(1,total))
                    ctx.workspace.status_text_set(f'Splitting: {done}/{total} – Esc: Abbrechen')
            except StopIteration as finished:
                self._finish(ctx)
                self.report({'INFO'},f'{finished.value} parts created. Original retained.')
                return {'FINISHED'}
            except (ValueError,RuntimeError,KeyError) as exc:
                self._steps.close();self._finish(ctx)
                self.report({'ERROR'},str(exc)[:220]);return {'CANCELLED'}
            return {'RUNNING_MODAL'}

    class WZ09_OT_city_assets_load(bpy.types.Operator):
        bl_idname='wz09.city_assets_load'
        bl_label='Load map details'
        bl_description='Load full editable geometry for the selected map; reuse existing loaded data'
        bl_options={'REGISTER','UNDO'}
        @classmethod
        def poll(cls,ctx):return WZ09_OT_city_split_loose.poll(ctx)
        def execute(self,ctx):
            from .city_view_cache import load_assets
            try:
                root=load_assets(ctx)
                from . import map_parts,map_residency
                collection=map_residency.resident(ctx.scene,root)
                message=('Editable map parts and individual prop placements are ready'
                         if collection and map_parts.ready(collection) else 'Map geometry loaded')
                self.report({'INFO'},message)
                return {'FINISHED'}
            except (ValueError,KeyError) as exc:
                self.report({'ERROR'},str(exc)[:220]);return {'CANCELLED'}

    class WZ09_OT_city_assets_unload(bpy.types.Operator):
        bl_idname='wz09.city_assets_unload'
        bl_label='Save and unload map details'
        bl_description='Return to the overview. In detail-library workspaces, save a new version and release unused detail data; legacy scenes only hide the geometry'
        bl_options={'REGISTER','UNDO'}
        @classmethod
        def poll(cls,ctx):return WZ09_OT_city_split_loose.poll(ctx)
        def execute(self,ctx):
            from .city_view_cache import unload_assets
            try:
                unload_assets(ctx)
                self.report({'INFO'},'Map details unloaded; saved data and current preview retained')
                return {'FINISHED'}
            except (ValueError,KeyError) as exc:
                self.report({'ERROR'},str(exc)[:220]);return {'CANCELLED'}

    class WZ09_OT_city_bake_preview(bpy.types.Operator):
        bl_idname='wz09.city_bake_preview'
        bl_label='Rebuild map preview'
        bl_description='Merge current static map geometry into a material-preserving preview; detailed parts remain editable'
        bl_options={'REGISTER','UNDO'}
        @classmethod
        def poll(cls,ctx):return WZ09_OT_city_split_loose.poll(ctx)
        def execute(self,ctx):
            from .city_view_cache import bake
            try:
                if ctx.window:ctx.window.cursor_set('WAIT')
                result=bake(ctx)
                self.report({'INFO'},f"{result['source_objects']} objects to 1 preview. Load map details to continue editing.")
                return {'FINISHED'}
            except (ValueError,RuntimeError,KeyError) as exc:
                self.report({'ERROR'},str(exc)[:220]);return {'CANCELLED'}
            finally:
                if ctx.window:ctx.window.cursor_set('DEFAULT')

    class WZ09_OT_city_select_root(bpy.types.Operator):
        bl_idname = 'wz09.city_select_root'
        bl_label = 'Select whole city (G / R)'
        bl_options = {'REGISTER', 'UNDO'}
        def execute(self, ctx):
            try:
                if ctx.mode != 'OBJECT':
                    raise ValueError('Return to Object Mode first')
                root = map_root(ctx.active_object)
                bpy.ops.object.select_all(action='DESELECT')
                root.hide_set(False)
                root.hide_select = False
                root.select_set(True)
                ctx.view_layer.objects.active = root
                self.report({'INFO'}, 'G moves the city and its objects; R rotates the city')
                return {'FINISHED'}
            except ValueError as exc:
                self.report({'ERROR'}, str(exc)); return {'CANCELLED'}

    class WZ09_OT_city_mesh_edit(bpy.types.Operator):
        bl_idname = 'wz09.city_mesh_edit'
        bl_label = 'Edit selected mesh faces'
        bl_description = 'Edit the selected prepared part or prop; shared prop geometry becomes independent for this placement'
        bl_options = {'REGISTER', 'UNDO'}
        def execute(self, ctx):
            try:
                edited=begin_mesh_edit(ctx)
                from . import map_parts,map_residency
                collection=map_residency.resident(ctx.scene,map_root(edited))
                message=('Edit vertices, edges or faces. Tab returns to Object Mode.' if collection and map_parts.ready(collection)
                         else 'Select faces with B or L, then Extract selection; buildings can contain disconnected faces')
                self.report({'INFO'},message)
                return {'FINISHED'}
            except (ValueError, RuntimeError) as exc:
                self.report({'ERROR'}, str(exc)); return {'CANCELLED'}

    class WZ09_OT_city_extract(bpy.types.Operator):
        bl_idname = 'wz09.city_extract'
        bl_label = 'Extract selection as editable part'
        bl_options = {'REGISTER', 'UNDO'}
        @classmethod
        def poll(cls, ctx): return ctx.mode == 'EDIT_MESH'
        def execute(self, ctx):
            try:
                extract_selected(ctx)
                self.report({'INFO'}, 'Part selected: G move, R rotate, S scale, X delete; original retained')
                return {'FINISHED'}
            except (ValueError, RuntimeError, KeyError) as exc:
                self.report({'ERROR'}, str(exc)); return {'CANCELLED'}

    class WZ09_OT_city_restore(bpy.types.Operator):
        bl_idname = 'wz09.city_restore'
        bl_label = 'Restore this city source geometry'
        bl_options = {'REGISTER', 'UNDO'}
        @classmethod
        def poll(cls, ctx): return ctx.mode == 'OBJECT' and ctx.active_object is not None
        def execute(self, ctx):
            try:
                restore_city(ctx); return {'FINISHED'}
            except (ValueError, KeyError) as exc:
                self.report({'ERROR'}, str(exc)); return {'CANCELLED'}

    class WZ09_OT_track_native_model(bpy.types.Operator):
        bl_idname = 'wz09.track_native_model'
        bl_label = 'Track selected original prop'
        bl_options = {'REGISTER', 'UNDO'}
        def execute(self, ctx):
            try:
                track_model(ctx); return {'FINISHED'}
            except (ValueError, KeyError) as exc:
                self.report({'ERROR'}, str(exc)); return {'CANCELLED'}

    class WZ09_OT_export_native_draft(bpy.types.Operator, ExportHelper):
        bl_idname = 'wz09.export_native_draft'
        bl_label = 'Export original-asset edit draft'
        filename_ext = '.json'
        def execute(self, ctx):
            try:
                export_draft(ctx.scene, self.filepath)
                self.report({'INFO'}, 'Reference-only draft saved; runtime and collision integration pending')
                return {'FINISHED'}
            except (OSError, ValueError, KeyError) as exc:
                self.report({'ERROR'}, str(exc)); return {'CANCELLED'}

    class WZ09_OT_verify_original_sources(bpy.types.Operator):
        bl_idname = 'wz09.verify_original_sources'
        bl_label = 'Verify original MW2 installation'
        def execute(self, ctx):
            from .original_assets import verify_requirements
            try:
                requirements = [source_binding(o) for o in ctx.scene.objects if o.get('wz09_type') == 'map_instance']
                if not requirements:
                    raise ValueError('No city source registrations found in this scene')
                by_map = {}
                for requirement in requirements:
                    code = requirement['source_map']
                    if code in by_map and by_map[code] != requirement:
                        raise ValueError('Conflicting original revisions for ' + code)
                    by_map[code] = requirement
                verified = verify_requirements(ctx.scene.wz09_original_game_dir, list(by_map.values()))
                ctx.scene['wz09_original_source_check'] = f'{len(verified)} original map fastfiles verified'
                self.report({'INFO'}, ctx.scene['wz09_original_source_check'])
                return {'FINISHED'}
            except (OSError, ValueError, KeyError) as exc:
                ctx.scene['wz09_original_source_check'] = 'Verification failed: ' + str(exc)[:150]
                self.report({'ERROR'}, str(exc)); return {'CANCELLED'}

    _CLASSES.extend((WZ09_OT_city_split_loose,WZ09_OT_city_assets_load,WZ09_OT_city_assets_unload,WZ09_OT_city_bake_preview,
                     WZ09_OT_city_select_root, WZ09_OT_city_mesh_edit, WZ09_OT_city_extract, WZ09_OT_city_restore,
                     WZ09_OT_track_native_model, WZ09_OT_export_native_draft, WZ09_OT_verify_original_sources))
    bpy.types.Scene.wz09_original_game_dir = bpy.props.StringProperty(name='Original MW2 folder', subtype='DIR_PATH')
    for cls in _CLASSES:
        bpy.utils.register_class(cls)


def unregister():
    import bpy
    for cls in reversed(_CLASSES):
        bpy.utils.unregister_class(cls)
    _CLASSES.clear()
    del bpy.types.Scene.wz09_original_game_dir


def draw(layout, ctx):
    box = layout.box()
    box.label(text='City and building editing', icon='EDITMODE_HLT')
    row = box.row(align=True)
    row.operator('wz09.current_edit_focus', text='Overview').overview = True
    row.operator('wz09.current_edit_focus', text='Focus selected city').overview = False
    box.operator('wz09.city_select_root', icon='OBJECT_ORIGIN')
    box.operator('wz09.city_mesh_edit', icon='FACESEL')
    box.label(text='B: box select; L: linked faces. Then:')
    box.operator('wz09.city_extract', icon='MOD_EXPLODE')
    obj = ctx.active_object
    if obj and obj.get('wz09_compact_records_json'):
        box.operator('wz09.restore_compact_objects', text='Expand selected asset group')
    box.operator('wz09.track_native_model')
    box.operator('wz09.city_restore', icon='LOOP_BACK')
    box.operator('wz09.export_native_draft', icon='EXPORT')
    box.label(text='Game application and collision rebuild pending', icon='INFO')
    if ctx.scene.get('wz09_city_overview_stale'):
        box.label(text='Edited cities retain full geometry in overview', icon='INFO')
    box.prop(ctx.scene, 'wz09_original_game_dir')
    box.operator('wz09.verify_original_sources', icon='CHECKMARK')
    if ctx.scene.get('wz09_original_source_check'):
        box.label(text=ctx.scene['wz09_original_source_check'][:65])
