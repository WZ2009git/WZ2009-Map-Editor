bl_info = {
    'name': 'WZ2009 MAP EDITOR', 'author': 'WARZONE 2009 project',
    'version': (0, 10, 0), 'blender': (5, 0, 0), 'location':'3D View > N-Panel > WZ2009',
    'description':'Non-destructive Karvorsk maps, gameplay and waypoint authoring',
    'category':'3D View'
}
import bpy
import uuid
import hashlib
import os
import json
import math
import struct
import tempfile
from pathlib import Path
from mathutils import Matrix
from bpy.props import (EnumProperty, StringProperty, FloatProperty, BoolProperty, FloatVectorProperty)
from bpy_extras.io_utils import ImportHelper
from .nav_format import read_botwarfare_csv, scaled_nodes, NavFormatError
from .scene_io import export_blender_scene, atomic_write, safe_output_path, migrate_edges, MAPS
from .calibration import load_calibration, validate_calibration, verify_calibration_source, transform_source
from .validation import report as validate_world
from .tactical_map import export_tactical
from .source_bundle import capture_gltf_bundle
from .editing import prune_orphan_edges, remove_nav_nodes, duplicate_map_instance
from .authoring_helpers import resolve_owner_map, aircraft_routes, next_aircraft_keyframe

COLS={'map_instance':'WZ09_MAPS','nav_node':'WZ09_NAV', 'preview':'WZ09_PREVIEWS',
      'geometry':'WZ09_GEOMETRY','marker':'WZ09_MARKERS'}


def project_root(scene):
    configured = str(scene.wz09_project_root).strip()
    root = Path(bpy.path.abspath(configured)).resolve() if configured else Path(__file__).resolve().parents[2]
    if not (root/'AGENTS.md').is_file() or not (root/'schema/world-v1.schema.json').is_file():
        raise ValueError('Select the standalone WZ2009 MAP EDITOR project folder first')
    return root


def output_path(scene, value):
    root = project_root(scene)
    path = Path(bpy.path.abspath(value)) if value.startswith('//') else Path(value)
    return safe_output_path(path if path.is_absolute() else root/path, root=root)


def diagnostic_limits(scene):
    return {'max_edge_distance_m':getattr(scene,'wz09_nav_max_distance_m',40),
            'max_edge_delta_z_m':getattr(scene,'wz09_nav_max_delta_z_m',3)}


def map_calibration(obj, source=None):
    text = obj.get('wz09_calibration_json', '')
    if not text:
        raise ValueError('Load a landmark calibration JSON for this map before importing')
    data = validate_calibration(json.loads(text))
    if source is not None:
        path = Path(source).resolve()
        records=json.loads(obj.get('wz09_calibrations_json','[]'))
        for candidate in records:
            if Path(candidate['source_path']).resolve()==path:
                data=validate_calibration(candidate);break
        if Path(data['source_path']).resolve() != path:
            raise ValueError('Calibration is bound to another source file; use matching landmark evidence')
        if hashlib.sha256(path.read_bytes()).hexdigest() != data['source_sha256']:
            raise ValueError('Source hash changed since calibration')
    verify_calibration_source(data)
    return data


def selected_nodes(ctx):
    nodes = [o for o in ctx.selected_objects if o.get('wz09_type') == 'nav_node']
    if len(nodes) != 2:
        raise ValueError('Select exactly two WZ09 nav nodes; the active node is the source')
    nodes.sort(key=lambda o: (o != ctx.active_object, str(o['wz09_id'])))
    return nodes


def custom_geometry(scene):
    return [o for o in scene.objects if o.type in {'MESH', 'CURVE'}
            and bool(o.get('wz09_custom_geometry', False)) and not o.get('wz09_preview_only')]


def geometry_fingerprint(ctx, meshes):
    from .preview_render import _material_digest
    from .terrain_edges import validate_current as validate_edge_closures
    validate_edge_closures(ctx.scene)
    digest = hashlib.sha256()
    material_cache,image_cache={},{}
    triangles = 0
    for obj in sorted(meshes, key=lambda o: str(o.get('wz09_geometry_id', ''))):
        if not obj.get('wz09_geometry_id'):
            raise ValueError('Custom geometry needs a persistent wz09_geometry_id')
        evaluated = obj.evaluated_get(ctx.evaluated_depsgraph_get())
        mesh = evaluated.to_mesh()
        try:
            if not mesh or not mesh.vertices or not mesh.polygons:
                raise ValueError('Custom geometry is empty')
            digest.update(str(obj['wz09_geometry_id']).encode())
            digest.update(obj.name_full.encode())
            for row in obj.matrix_world:
                digest.update(struct.pack('<4d', *row))
            for vertex in mesh.vertices:
                if not all(math.isfinite(v) for v in vertex.co):
                    raise ValueError('Custom geometry contains nonfinite coordinates')
                digest.update(struct.pack('<3d', *vertex.co))
            mesh.calc_loop_triangles()
            for triangle in mesh.loop_triangles:
                a, b, c = (mesh.vertices[i].co for i in triangle.vertices)
                if (b-a).cross(c-a).length < 1e-10:
                    raise ValueError('Custom geometry contains a degenerate triangle')
                digest.update(struct.pack('<3I', *triangle.vertices))
                triangles += 1
            for polygon in mesh.polygons:digest.update(struct.pack('<I',polygon.material_index))
            for layer in mesh.uv_layers:
                digest.update(layer.name.encode())
                for item in layer.data:digest.update(struct.pack('<2d',*item.uv))
            for normal in mesh.corner_normals:digest.update(struct.pack('<3d',*normal.vector))
            for attr in mesh.color_attributes:
                digest.update((attr.name+attr.domain+attr.data_type).encode())
                for item in attr.data:digest.update(struct.pack('<4d',*item.color))
            for slot in obj.material_slots:
                value=_material_digest(slot.material,material_cache,image_cache)
                digest.update((value or 'no-material').encode())
        finally:
            evaluated.to_mesh_clear()
    return digest.hexdigest(), triangles


def scene_bounds(scene):
    if getattr(scene,'wz09_use_work_bounds',False):
        bounds=list(scene.wz09_work_bounds)
        if not all(math.isfinite(v) for v in bounds) or bounds[0]>=bounds[2] or bounds[1]>=bounds[3]:
            raise ValueError('Work bounds must have positive finite XY extents')
        return bounds
    points = []
    for obj in scene.objects:
        if obj.get('wz09_type') or obj.get('wz09_preview_only') or obj.get('wz09_custom_geometry'):
            if obj.type in {'MESH', 'CURVE'}:
                from mathutils import Vector
                points.extend(obj.matrix_world @ Vector(corner) for corner in obj.bound_box)
            else:
                points.append(obj.matrix_world.translation)
    if not points:
        raise ValueError('No authored objects for tactical bounds')
    return [min(p.x for p in points)-2, min(p.y for p in points)-2,
            max(p.x for p in points)+2, max(p.y for p in points)+2]


def document_for_export(ctx, path, include_tactical=True):
    migrate_edges(ctx.scene)
    document = export_blender_scene(ctx.scene)
    meshes = custom_geometry(ctx.scene)
    gp = ctx.scene.get('wz09_glb_absolute')
    if meshes and not gp:
        raise ValueError('Export the current custom geometry GLB before the world JSON')
    if gp:
        gp = output_path(ctx.scene, str(gp))
        if not gp.is_file():
            raise ValueError('Referenced custom geometry GLB is missing')
        fingerprint, _ = geometry_fingerprint(ctx, meshes)
        if fingerprint != ctx.scene.get('wz09_glb_fingerprint'):
            raise ValueError('Geometry changed; export a new GLB version first')
        sha = hashlib.sha256(gp.read_bytes()).hexdigest()
        if sha != ctx.scene.get('wz09_glb_sha256'):
            raise ValueError('Referenced GLB hash changed; select a new export version')
        segments = []
        map_ids = {m['id'] for m in document['maps']}
        for obj in meshes:
            src, dst = str(obj.get('wz09_connector_from', '')), str(obj.get('wz09_connector_to', ''))
            if src or dst:
                if src not in map_ids or dst not in map_ids or src == dst:
                    raise ValueError('Connector endpoints must identify two different map instances')
                segment={'id':str(obj['wz09_geometry_id']),'from_map':src,'to_map':dst,'status':'proposed'}
                if obj.get('wz09_geometry_kind'):segment['kind']=str(obj['wz09_geometry_kind'])
                portals=[str(obj.get(key,'')) for key in ('wz09_connector_from_portal','wz09_connector_to_portal')]
                if any(portals):
                    if not all(portals):raise ValueError('Connector requires both explicit portal IDs')
                    segment.update(from_portal=portals[0],to_portal=portals[1])
                segments.append(segment)
        document['geometry'] = [{'id': str(ctx.scene['wz09_geometry_id']),
            'path': os.path.relpath(gp, path.parent).replace('\\', '/'), 'sha256': sha,
            'coordinate_system': 'gltf_y_up_meters', 'connector': {'segments': segments}}]
    if include_tactical and ctx.scene.get('wz09_tactical_json'):
        tactical = json.loads(ctx.scene['wz09_tactical_json'])
        if state_fingerprint(ctx, document) != ctx.scene.get('wz09_tactical_fingerprint'):
            raise ValueError('World changed; export a new tactical map version first')
        absolute = output_path(ctx.scene, tactical['path'])
        tactical['path'] = os.path.relpath(absolute, path.parent).replace('\\', '/')
        if tactical.get('manifest_path'):
            sidecar=output_path(ctx.scene,tactical['manifest_path'])
            tactical['manifest_path']=os.path.relpath(sidecar,path.parent).replace('\\','/')
        if tactical.get('image'):
            image=output_path(ctx.scene,tactical['image']['path'])
            tactical['image']['path']=os.path.relpath(image,path.parent).replace('\\','/')
        document['tactical_map'] = tactical
    return document


def state_fingerprint(ctx, document):
    stable = dict(document, tactical_map={'status': 'not_generated'})
    state={'world':stable,'bounds':scene_bounds(ctx.scene)}
    if getattr(ctx.scene,'wz09_render_tactical_preview',False) or ctx.scene.get('wz09_tactical_preview_json'):
        from .preview_render import preview_fingerprint
        state['preview']=preview_fingerprint(ctx.scene)
    return hashlib.sha256(json.dumps(state,
        sort_keys=True, allow_nan=False).encode()).hexdigest()


def coll(name,scene):
    if not scene.get('wz09_scene_id'):scene['wz09_scene_id']=str(uuid.uuid4())
    scene_id=str(scene['wz09_scene_id'])
    for collection in scene.collection.children:
        if (collection.get('wz09_collection_kind')==name and collection.get('wz09_scene_id')==scene_id
            or collection.name==name and collection.users==1 and not collection.get('wz09_scene_id')):
            collection['wz09_collection_kind']=name;collection['wz09_scene_id']=scene_id
            return collection
    collection=bpy.data.collections.new(name)
    collection['wz09_collection_kind']=name;collection['wz09_scene_id']=scene_id
    scene.collection.children.link(collection)
    return collection

def make_empty(context,name,kind,collection_key,location=(0,0,0),parent=None,uid=None,activate=True):
    o=bpy.data.objects.new(name,None)
    o.empty_display_type='SPHERE' if kind=='nav_node' else 'CUBE'
    o.empty_display_size=0.35 if kind=='nav_node' else 1.5
    coll(COLS[collection_key],context.scene).objects.link(o)
    o['wz09_type']=kind;o['wz09_id']=uid or str(uuid.uuid4())
    if parent is not None:o.parent=parent
    o.location=location
    if activate:
        bpy.ops.object.select_all(action='DESELECT');o.select_set(True)
        context.view_layer.objects.active=o
    return o

class WZ09_OT_init(bpy.types.Operator):
    bl_idname='wz09.initialize';bl_label='Initialize Karvorsk';bl_options={'REGISTER','UNDO'}
    def execute(self,ctx):
        for name in COLS.values():coll(name,ctx.scene)
        for obj in ctx.scene.objects:
            if obj.get('wz09_preview_only') and not obj.get('wz09_preview_id'):
                obj['wz09_preview_id']=str(uuid.uuid4())
        ctx.scene.unit_settings.system='METRIC';ctx.scene.unit_settings.scale_length=1.0
        self.report({'INFO'},'WZ2009 collections ready (no game files touched)');return {'FINISHED'}

class WZ09_OT_add_map(bpy.types.Operator):
    bl_idname='wz09.add_map';bl_label='Add Map Instance';bl_options={'REGISTER','UNDO'}
    def execute(self,ctx):
        code=ctx.scene.wz09_map_choice
        o=make_empty(ctx,code,'map_instance','map_instance',ctx.scene.cursor.location)
        o['wz09_source_map']=code;o['wz09_source_scale']=0.0
        self.report({'INFO'},'Map instance is a marker, NOT imported geometry/collision');return {'FINISHED'}

class WZ09_OT_add_marker(bpy.types.Operator):
    bl_idname='wz09.add_marker';bl_label='Add Gameplay Marker';bl_options={'REGISTER','UNDO'}
    def execute(self,ctx):
        kind=ctx.scene.wz09_marker_choice
        parent=resolve_owner_map(ctx.active_object)
        location=parent.matrix_world.inverted() @ ctx.scene.cursor.location if parent is not None else ctx.scene.cursor.location
        frame=next_aircraft_keyframe(ctx.scene.objects,ctx.scene.wz09_aircraft_route_id,
            ctx.scene.wz09_aircraft_interval_s) if kind=='aircraft_keyframe' else None
        o=make_empty(ctx,kind,kind,'marker',location,parent)
        o['wz09_profile']='default'
        o['wz09_properties_json']='{}'
        if kind=='loot_crate':o['wz09_loot_table']='standard'
        if kind=='buy_station':o['wz09_shop_catalog']='standard'
        if kind=='poi':o['wz09_poi_name']='Unnamed POI'
        if frame is not None:
            o['wz09_route_id']=frame['route_id'];o['wz09_route_sequence']=frame['sequence'];o['wz09_route_time_s']=frame['time_s']
        return {'FINISHED'}


class WZ09_OT_duplicate_map(bpy.types.Operator):
    bl_idname='wz09.duplicate_map';bl_label='Duplicate Map at Cursor';bl_options={'REGISTER','UNDO'}
    def execute(self,ctx):
        try:
            copied=duplicate_map_instance(ctx,resolve_owner_map(ctx.active_object))
            copied.location=ctx.scene.cursor.location
            bpy.ops.object.select_all(action='DESELECT');copied.select_set(True)
            ctx.view_layer.objects.active=copied;ctx.view_layer.update()
            self.report({'INFO'},'Map hierarchy copied with new IDs and internal links; previews share source meshes')
            return {'FINISHED'}
        except (ValueError,RuntimeError) as exc:
            self.report({'ERROR'},str(exc)[:220]);return {'CANCELLED'}


class WZ09_OT_delete_nav(bpy.types.Operator):
    bl_idname='wz09.delete_nav';bl_label='Delete Nav Nodes and Links';bl_options={'REGISTER','UNDO'}
    def execute(self,ctx):
        try:
            result=remove_nav_nodes(ctx.scene,ctx.selected_objects)
            self.report({'INFO'},f'Removed {result["nodes_removed"]} nodes and {result["edges_removed"]} incident links')
            return {'FINISHED'}
        except (ValueError,RuntimeError) as exc:
            self.report({'ERROR'},str(exc)[:220]);return {'CANCELLED'}


class WZ09_OT_repair_orphans(bpy.types.Operator):
    bl_idname='wz09.repair_orphans';bl_label='Remove Orphan Nav Links';bl_options={'REGISTER','UNDO'}
    def execute(self,ctx):
        try:
            count=prune_orphan_edges(ctx.scene)
            self.report({'INFO'},f'Removed {count} links with missing endpoints; surviving IDs unchanged')
            return {'FINISHED'}
        except ValueError as exc:
            self.report({'ERROR'},str(exc)[:220]);return {'CANCELLED'}


class WZ09_OT_rust_registration(bpy.types.Operator):
    bl_idname='wz09.load_rust_registration';bl_label='Load Verified Rust Registration';bl_options={'REGISTER','UNDO'}
    def execute(self,ctx):
        try:
            path=project_root(ctx.scene)/'examples/mp_rust.export-registration-0.3.0.json'
            selected=resolve_owner_map(ctx.active_object)
            if selected is None or selected.get('wz09_source_map')!='mp_rust':
                raise ValueError('Select the Rust map or one of its children')
            return bpy.ops.wz09.load_calibration(filepath=str(path))
        except (OSError,ValueError,RuntimeError) as exc:
            self.report({'ERROR'},str(exc)[:220]);return {'CANCELLED'}


class WZ09_OT_work_bounds(bpy.types.Operator):
    bl_idname='wz09.work_bounds';bl_label='Work Area from Selected Nodes';bl_options={'REGISTER','UNDO'}
    def execute(self,ctx):
        try:
            nodes=[o for o in ctx.selected_objects if o.get('wz09_type')=='nav_node']
            if len(nodes)<2:raise ValueError('Select two or more nav nodes to define the work area')
            points=[o.matrix_world.translation for o in nodes]
            margin=ctx.scene.wz09_work_margin
            bounds=[min(p.x for p in points)-margin,min(p.y for p in points)-margin,
                    max(p.x for p in points)+margin,max(p.y for p in points)+margin]
            from .preview_render import square_bounds
            ctx.scene.wz09_work_bounds=square_bounds(bounds)
            ctx.scene.wz09_use_work_bounds=True
            self.report({'INFO'},'Explicit editor work area set; this is not a collision or playable-area boundary')
            return {'FINISHED'}
        except ValueError as exc:
            self.report({'ERROR'},str(exc)[:220]);return {'CANCELLED'}


class WZ09_OT_repair_preview(bpy.types.Operator):
    bl_idname='wz09.repair_preview_winding';bl_label='Repair Preview Face Winding';bl_options={'REGISTER','UNDO'}
    def execute(self,ctx):
        try:
            from .preview_geometry import repair_preview_winding
            result=repair_preview_winding(ctx.scene)
            ctx.view_layer.update()
            self.report({'INFO'},f'Preview copies: {result["meshes_copied"]} meshes, {result["faces_reversed"]} faces corrected; sources preserved')
            return {'FINISHED'}
        except (ValueError,RuntimeError) as exc:
            self.report({'ERROR'},str(exc)[:220]);return {'CANCELLED'}

class WZ09_OT_add_nav(bpy.types.Operator):
    bl_idname='wz09.add_nav';bl_label='Add Nav Node';bl_options={'REGISTER','UNDO'}
    def execute(self,ctx):
        parent=resolve_owner_map(ctx.active_object)
        location = parent.matrix_world.inverted() @ ctx.scene.cursor.location if parent is not None else ctx.scene.cursor.location
        o=make_empty(ctx,'nav_manual','nav_node','nav_node',location,parent)
        o['wz09_owner_map']=str(parent['wz09_id']) if parent is not None else ''
        o['wz09_nav_kind']='walk';o['wz09_nav_source']='manual';o['wz09_links']=''
        return {'FINISHED'}


class WZ09_OT_calibrate(bpy.types.Operator, ImportHelper):
    bl_idname='wz09.load_calibration';bl_label='Load Landmark Calibration'
    bl_options={'REGISTER','UNDO'}
    filename_ext='.json';filter_glob:StringProperty(default='*.json',options={'HIDDEN'})
    def execute(self,ctx):
        parent=resolve_owner_map(ctx.active_object)
        try:
            if not parent or parent.get('wz09_type')!='map_instance':
                raise ValueError('Select a map instance first')
            data=load_calibration(self.filepath)
            previous=parent.get('wz09_calibration_json')
            compatible=True
            if previous:
                old=validate_calibration(json.loads(previous))
                compatible=([a.lstrip('+') for a in old['axis_mapping']]==[a.lstrip('+') for a in data['axis_mapping']]
                    and math.isclose(old['source_units_to_meter'],data['source_units_to_meter'],rel_tol=1e-8)
                    and all(math.isclose(a,b,abs_tol=1e-8) for a,b in zip(old['offset_m'],data['offset_m'])))
            if previous and any(o.parent==parent for o in ctx.scene.objects):
                if not compatible:
                    raise ValueError('Children use another transform; use a new map instance')
            if not previous or not any(o.parent==parent for o in ctx.scene.objects):
                parent['wz09_calibration_json']=json.dumps(data,sort_keys=True)
            records=json.loads(parent.get('wz09_calibrations_json','[]')) if compatible else []
            records=[record for record in records if record['source_path']!=data['source_path']]+[data]
            parent['wz09_calibrations_json']=json.dumps(records,sort_keys=True)
            provenance=json.loads(parent.get('wz09_map_provenance_json','{}'))
            provenance['calibration_sources']=records
            parent['wz09_map_provenance_json']=json.dumps(provenance,sort_keys=True)
            parent['wz09_source_scale']=data['source_units_to_meter']
            ctx.scene.wz09_nav_scale=data['source_units_to_meter']
            registered=data.get('basis')=='export_registration'
            self.report({'WARNING'} if data['synthetic'] or registered else {'INFO'},
                'Native/glTF registration verified; independent physical scale remains unverified' if registered else
                'Synthetic test calibration; real units remain unverified' if data['synthetic'] else 'Landmark evidence loaded; source hash verified')
            return {'FINISHED'}
        except (OSError,ValueError,KeyError) as exc:
            self.report({'ERROR'},str(exc)[:220]);return {'CANCELLED'}

class WZ09_OT_import_waypoints(bpy.types.Operator,ImportHelper):
    bl_idname='wz09.import_bot_waypoints';bl_label='Import Bot Warfare CSV'
    bl_options={'REGISTER','UNDO'}
    filename_ext='.csv';filter_glob:StringProperty(default='*.csv',options={'HIDDEN'})
    def execute(self,ctx):
        p=resolve_owner_map(ctx.active_object)
        if not p or p.get('wz09_type')!='map_instance':
            self.report({'ERROR'},'Select a WZ09 Map Instance before import');return {'CANCELLED'}
        created=[]
        try:
            calibration=map_calibration(p,self.filepath)
            scale=calibration['source_units_to_meter']
            raw=read_botwarfare_csv(self.filepath)
            parsed=scaled_nodes(raw,str(p['wz09_id']),scale)
            codes=[o.get('wz09_id') for o in ctx.scene.objects]
            if any(n['id'] in codes for n in parsed):
                raise NavFormatError('Some waypoint IDs already exist: import once per map instance')
            for obj in ctx.scene.objects:
                if obj.get('wz09_type')=='nav_node' and obj.get('wz09_owner_map')==str(p['wz09_id']):
                    prior=json.loads(obj.get('wz09_nav_provenance_json','{}'))
                    if prior.get('source_path')==str(Path(self.filepath).resolve()):
                        raise NavFormatError('This waypoint source already belongs to this map instance')
            edges=migrate_edges(ctx.scene)
            positions=[transform_source(row['origin_source_units'],scale,calibration) for row in raw]
            for n in parsed:
                position=positions[n['original_index']]
                o=make_empty(ctx,'wp_'+str(n['original_index']),'nav_node','nav_node',position,p,n['id'],activate=False)
                created.append(o)
                o['wz09_owner_map']=n['owner_map'];o['wz09_nav_kind']=n['kind'];o['wz09_nav_source']='iw4_bot_warfare'
                original=raw[n['original_index']]
                o['wz09_nav_properties_json']=json.dumps({'original_index':n['original_index'],
                    'origin_source_units':original['origin_source_units'],'angles_deg':original['angles'],
                    'jav_point_source_units':original['jav_point']})
                o['wz09_nav_provenance_json']=json.dumps({'format':'iw4_bot_warfare_csv',
                    'source_path':str(Path(self.filepath).resolve()),'source_sha256':calibration['source_sha256'],
                    'calibration_synthetic':calibration['synthetic']})
            for original in raw:
                for child in original['children']:
                    edges.append({'id':str(uuid.uuid4()),'from':f'{p["wz09_id"]}:wp:{original["index"]}',
                        'to':f'{p["wz09_id"]}:wp:{child}','kind':'walk','status':'proposed'})
            ctx.scene['wz09_edges_json']=json.dumps(edges,sort_keys=True)
            p['wz09_source_scale']=scale
            self.report({'INFO'},f'Imported {len(parsed)} proposed waypoint nodes; collision remains unverified')
            return {'FINISHED'}
        except (OSError,ValueError,RuntimeError,KeyError) as exc:
            for obj in created:bpy.data.objects.remove(obj,do_unlink=True)
            self.report({'ERROR'},str(exc)[:220]);return {'CANCELLED'}

class WZ09_OT_link_nodes(bpy.types.Operator):
    bl_idname='wz09.link_nodes';bl_label='Link 2 Selected Nav Nodes';bl_options={'REGISTER','UNDO'}
    bidirectional:BoolProperty(name='Bidirectional',default=True)
    def execute(self,ctx):
        try:
            nodes=selected_nodes(ctx);edges=migrate_edges(ctx.scene)
            pairs=[(nodes[0],nodes[1])]
            if self.bidirectional:pairs.append((nodes[1],nodes[0]))
            for src,dst in pairs:
                a,b=str(src['wz09_id']),str(dst['wz09_id'])
                if not any(e['from']==a and e['to']==b for e in edges):
                    edges.append({'id':str(uuid.uuid4()),'from':a,'to':b,
                        'kind':ctx.scene.wz09_edge_kind,'status':'proposed'})
            ctx.scene['wz09_edges_json']=json.dumps(edges,sort_keys=True)
            self.report({'INFO'},'Proposed link added; active node is source (collision unverified)')
            return {'FINISHED'}
        except ValueError as exc:
            self.report({'ERROR'},str(exc)[:220]);return {'CANCELLED'}


class WZ09_OT_unlink_nodes(bpy.types.Operator):
    bl_idname='wz09.unlink_nodes';bl_label='Remove Selected Link';bl_options={'REGISTER','UNDO'}
    def execute(self,ctx):
        try:
            src,dst=selected_nodes(ctx);edges=migrate_edges(ctx.scene)
            pairs={(str(src['wz09_id']),str(dst['wz09_id']))}
            if ctx.scene.wz09_bidirectional:pairs.add((str(dst['wz09_id']),str(src['wz09_id'])))
            ctx.scene['wz09_edges_json']=json.dumps([e for e in edges if (e['from'],e['to']) not in pairs],sort_keys=True)
            return {'FINISHED'}
        except ValueError as exc:
            self.report({'ERROR'},str(exc));return {'CANCELLED'}

class WZ09_OT_import_obj(bpy.types.Operator,ImportHelper):
    bl_idname='wz09.import_visual_obj';bl_label='Import Visual OBJ (C2M preview)';filename_ext='.obj'
    bl_options={'REGISTER','UNDO'}
    filter_glob:StringProperty(default='*.obj',options={'HIDDEN'})
    def execute(self,ctx):
        p=resolve_owner_map(ctx.active_object)
        if not p or p.get('wz09_type')!='map_instance':
            self.report({'ERROR'},'Select Map Instance before importing visual OBJ');return {'CANCELLED'}
        try:
            from .obj_bundle import capture_obj_bundle
            calibration=map_calibration(p,self.filepath)
            bundle=capture_obj_bundle(self.filepath)
        except (ValueError,OSError) as exc:
            self.report({'ERROR'},str(exc)[:220]);return {'CANCELLED'}
        pre=set(bpy.data.objects)
        try:bpy.ops.wm.obj_import(filepath=self.filepath,forward_axis='Y',up_axis='Z')
        except Exception as e:
            self.report({'ERROR'},str(e)[:200]);return {'CANCELLED'}
        imported=set(bpy.data.objects)-pre
        if not imported:
            self.report({'ERROR'},'Importer created no objects');return {'CANCELLED'}
        dst=coll(COLS['preview'],ctx.scene)
        for o in imported:
            for c in list(o.users_collection):c.objects.unlink(o)
            dst.objects.link(o)
            source_matrix=o.matrix_world.copy()
            axes=calibration['axis_mapping'];scale=calibration['source_units_to_meter']
            transform=Matrix.Identity(4)
            for i,axis in enumerate(axes):
                for j in range(3):transform[i][j]=0
                transform[i]['xyz'.index(axis[-1])]=(-1 if axis.startswith('-') else 1)*scale
                transform[i][3]=calibration['offset_m'][i]
            o.parent=p;o.matrix_parent_inverse=Matrix.Identity(4);o.matrix_basis=transform@source_matrix
            o['wz09_preview_only']=True
            o['wz09_preview_id']=str(uuid.uuid4())
            o['wz09_source_sha256']=calibration['source_sha256']
        provenance=json.loads(p.get('wz09_map_provenance_json','{}'))
        provenance['obj_preview_bundle']=bundle
        p['wz09_map_provenance_json']=json.dumps(provenance,sort_keys=True)
        self.report({'WARNING'},f'{len(imported)} visual preview objects; original collision NOT modified')
        return {'FINISHED'}


class WZ09_OT_import_gltf(bpy.types.Operator,ImportHelper):
    bl_idname='wz09.import_visual_gltf';bl_label='Import glTF Preview (meters)'
    bl_options={'REGISTER','UNDO'}
    filename_ext='.gltf';filter_glob:StringProperty(default='*.gltf;*.glb',options={'HIDDEN'})
    def execute(self,ctx):
        parent=resolve_owner_map(ctx.active_object)
        if not parent or parent.get('wz09_type')!='map_instance':
            self.report({'ERROR'},'Select a map instance first');return {'CANCELLED'}
        before=set(bpy.data.objects)
        try:
            bundle=capture_gltf_bundle(self.filepath,include_images=True)
            if parent.get('wz09_calibration_json'):
                calibration=map_calibration(parent)
                registration=calibration.get('registration')
                if registration and Path(registration['reference_path']).resolve()!=Path(self.filepath).resolve():
                    raise ValueError('Map registration belongs to another glTF preview')
            # glTF defines meters/Y-up; Blender performs its standard axis conversion.
            # This preview does not calibrate MW2 source coordinates or CSV data.
            bpy.ops.import_scene.gltf(filepath=self.filepath)
            imported=set(bpy.data.objects)-before
            if not imported:raise ValueError('glTF importer created no objects')
            roots=[o for o in imported if o.parent not in imported]
            dst=coll(COLS['preview'],ctx.scene)
            from .preview_linking import move_imported_objects
            move_imported_objects(imported,dst,bpy_module=bpy)
            for obj in imported:
                obj['wz09_preview_only']=True
                obj['wz09_preview_id']=str(uuid.uuid4())
            for obj in roots:
                local=obj.matrix_world.copy();obj.parent=parent
                obj.matrix_parent_inverse=Matrix.Identity(4);obj.matrix_basis=local
            parent['wz09_preview_source']=str(Path(self.filepath).resolve())
            parent['wz09_preview_sha256']=hashlib.sha256(Path(self.filepath).read_bytes()).hexdigest()
            provenance=json.loads(parent.get('wz09_map_provenance_json','{}'))
            provenance['preview_bundle']=bundle
            parent['wz09_map_provenance_json']=json.dumps(provenance,sort_keys=True)
            self.report({'WARNING'},f'{len(imported)} preview objects; source calibration/collision remain unverified')
            return {'FINISHED'}
        except (OSError,ValueError,RuntimeError) as exc:
            for obj in set(bpy.data.objects)-before:bpy.data.objects.remove(obj,do_unlink=True)
            self.report({'ERROR'},str(exc)[:220]);return {'CANCELLED'}

class WZ09_OT_add_road(bpy.types.Operator):
    bl_idname='wz09.add_road';bl_label='Add Editable Road Segment';bl_options={'REGISTER','UNDO'}
    def execute(self,ctx):
        maps=sorted([o for o in ctx.selected_objects if o.get('wz09_type')=='map_instance'],key=lambda o:str(o['wz09_id']))
        bpy.ops.mesh.primitive_cube_add(size=1,location=ctx.scene.cursor.location)
        o=ctx.active_object
        o.name='WZ09_Road_Connector'
        for c in list(o.users_collection):c.objects.unlink(o)
        coll(COLS['geometry'],ctx.scene).objects.link(o)
        o.dimensions=(ctx.scene.wz09_road_length,ctx.scene.wz09_road_width,0.25)
        o['wz09_custom_geometry']=True
        o['wz09_geometry_id']=str(uuid.uuid4())
        o['wz09_connector_from']=str(maps[0]['wz09_id']) if len(maps)==2 else ''
        o['wz09_connector_to']=str(maps[1]['wz09_id']) if len(maps)==2 else ''
        self.report({'INFO'},'Editable road placeholder only: no collision or navmesh baked')
        return {'FINISHED'}

class WZ09_OT_export_geometry(bpy.types.Operator):
    bl_idname='wz09.export_custom_geometry';bl_label='Export Custom Geometry GLB'
    def execute(self,ctx):
        meshes=custom_geometry(ctx.scene)
        if not meshes:
            self.report({'ERROR'},'No approved custom geometry found in WZ09_GEOMETRY');return {'CANCELLED'}
        try:
            destination=output_path(ctx.scene,ctx.scene.wz09_geometry_path)
            fingerprint,triangles=geometry_fingerprint(ctx,meshes)
            if destination.exists():
                digest=hashlib.sha256(destination.read_bytes()).hexdigest()
                if (ctx.scene.get('wz09_glb_absolute')==str(destination)
                    and ctx.scene.get('wz09_glb_fingerprint')==fingerprint
                    and ctx.scene.get('wz09_glb_sha256')==digest):
                    self.report({'INFO'},'Unchanged GLB already exists; reused without overwriting')
                    return {'FINISHED'}
                raise ValueError('Output already exists; select a new GLB version')
            destination.parent.mkdir(parents=True,exist_ok=True)
        except (OSError,ValueError) as exc:
            self.report({'ERROR'},str(exc)[:220]);return {'CANCELLED'}
        selected=list(ctx.selected_objects);active=ctx.view_layer.objects.active
        try:
            bpy.ops.object.select_all(action='DESELECT')
            for o in meshes:o.select_set(True)
            ctx.view_layer.objects.active=meshes[0]
            with tempfile.TemporaryDirectory(prefix='.wz09-glb-',dir=destination.parent) as td:
                staged=Path(td)/'geometry.glb'
                result=bpy.ops.export_scene.gltf(filepath=str(staged),export_format='GLB',use_selection=True,use_active_scene=True)
                if result!={'FINISHED'} or not staged.is_file():raise ValueError('GLB exporter did not complete')
                payload=staged.read_bytes()
                if payload[:4]!=b'glTF' or len(payload)<20:raise ValueError('GLB exporter produced invalid output')
                with destination.open('xb') as target:target.write(payload)
        except Exception as e:
            self.report({'ERROR'},f'GLB export failed: {e}'[:200]);return {'CANCELLED'}
        finally:
            bpy.ops.object.select_all(action='DESELECT')
            for o in selected:
                if o.name in bpy.data.objects:o.select_set(True)
            if active and active.name in bpy.data.objects:ctx.view_layer.objects.active=active
        if not destination.is_file() or destination.stat().st_size==0:
            self.report({'ERROR'},'GLB missing or empty');return {'CANCELLED'}
        ctx.scene['wz09_glb_absolute']=str(destination)
        ctx.scene['wz09_glb_fingerprint']=fingerprint
        ctx.scene['wz09_glb_sha256']=hashlib.sha256(destination.read_bytes()).hexdigest()
        if not ctx.scene.get('wz09_geometry_id'):ctx.scene['wz09_geometry_id']=str(uuid.uuid4())
        self.report({'INFO'},f'Exported {len(meshes)} objects / {triangles} triangles; collision unverified')
        return {'FINISHED'}

class WZ09_OT_export_json(bpy.types.Operator):
    bl_idname='wz09.export_json';bl_label='Export World JSON'
    def execute(self,ctx):
        try:
            path=output_path(ctx.scene,ctx.scene.wz09_export_path)
            document=document_for_export(ctx,path)
            validation=validate_world(document,base_dir=path.parent,diagnostic_limits=diagnostic_limits(ctx.scene))
            if not validation['ok']:raise ValueError(validation['errors'][0])
            from . import product_export
            documents=product_export.collect(ctx.scene,document)
            document=product_export.publish(document,documents,path,project_root(ctx.scene))
            validation=validate_world(document,base_dir=path.parent,diagnostic_limits=diagnostic_limits(ctx.scene))
            if not validation['ok']:raise ValueError(validation['errors'][0])
            result=atomic_write(path,document,root=project_root(ctx.scene))
            self.report({'INFO'},f'Wrote {result} (editor-only; no playability claim)')
            return {'FINISHED'}
        except (OSError,ValueError) as e:
            self.report({'ERROR'},str(e)[:200]);return {'CANCELLED'}


class WZ09_OT_preflight(bpy.types.Operator):
    bl_idname='wz09.preflight';bl_label='Validate World (Offline)'
    def execute(self,ctx):
        try:
            path=output_path(ctx.scene,ctx.scene.wz09_export_path)
            document=document_for_export(ctx,path)
            from . import product_export
            product_export.collect(ctx.scene,document)
            result=validate_world(document,base_dir=path.parent,diagnostic_limits=diagnostic_limits(ctx.scene))
            ctx.scene['wz09_preflight_json']=json.dumps(result,sort_keys=True)
            if not result['ok']:raise ValueError(result['errors'][0])
            stats=result['stats']
            self.report({'WARNING'} if result['warnings'] else {'INFO'},
                f'Valid authoring data: {stats["nav_nodes"]} nodes, {stats["nav_components"]} weak components; {len(result["warnings"])} warnings')
            return {'FINISHED'}
        except (OSError,ValueError) as exc:
            self.report({'ERROR'},str(exc)[:220]);return {'CANCELLED'}


class WZ09_OT_tactical(bpy.types.Operator):
    bl_idname='wz09.export_tactical';bl_label='Export Tactical SVG'
    def execute(self,ctx):
        try:
            target=output_path(ctx.scene,ctx.scene.wz09_tactical_path)
            world_path=output_path(ctx.scene,ctx.scene.wz09_export_path)
            document=document_for_export(ctx,world_path,include_tactical=False)
            validation=validate_world(document,base_dir=world_path.parent,diagnostic_limits=diagnostic_limits(ctx.scene))
            if not validation['ok']:raise ValueError(validation['errors'][0])
            bounds=scene_bounds(ctx.scene);background=None
            if ctx.scene.wz09_render_tactical_preview:
                from .preview_render import render_orthographic,preview_fingerprint,square_bounds
                preview_path=target.with_suffix('.png')
                existing=json.loads(ctx.scene.get('wz09_tactical_preview_json','null'))
                fingerprint=preview_fingerprint(ctx.scene)
                expected_bounds=square_bounds(bounds)
                if preview_path.exists():
                    if (not existing or output_path(ctx.scene,existing['path'])!=preview_path
                        or existing.get('source_fingerprint')!=fingerprint
                        or hashlib.sha256(preview_path.read_bytes()).hexdigest()!=existing['sha256']
                        or any(abs(a-b)>1e-6 for a,b in zip(existing['bounds_m'],expected_bounds))):
                        raise ValueError('Existing preview image differs; choose a new Tactical filename')
                    background=existing
                else:
                    background=render_orthographic(ctx,preview_path,bounds,root=project_root(ctx.scene))
                    ctx.scene['wz09_tactical_preview_json']=json.dumps(background,sort_keys=True)
                bounds=background['bounds_m']
            record=export_tactical(document,target,bounds=bounds,root=project_root(ctx.scene),background=background)
            ctx.scene['wz09_tactical_json']=json.dumps(record,sort_keys=True)
            ctx.scene['wz09_tactical_fingerprint']=state_fingerprint(ctx,document)
            self.report({'INFO'},'Georeferenced overlay and orthographic preview exported; collision unverified' if background
                else 'Georeferenced schematic exported; terrain/collision not inferred')
            return {'FINISHED'}
        except (OSError,ValueError) as exc:
            self.report({'ERROR'},str(exc)[:220]);return {'CANCELLED'}

class WZ09_PT_main(bpy.types.Panel):
    bl_label='WZ2009 MAP EDITOR';bl_idname='WZ09_PT_main'
    bl_space_type='VIEW_3D';bl_region_type='UI';bl_category='WZ2009'
    def draw(self,ctx):
        from . import editor_ux
        a=self.layout;s=ctx.scene;selected=ctx.active_object
        from . import installation_setup
        if installation_setup.is_public_project() and not s.get('wz09_public_project'):
            installation_setup.draw(a,ctx)
            return
        if s.get('wz09_library_workspace'):
            from . import map_residency
            map_residency.draw(a, ctx)
            edit=a.box(); edit.label(text='2. Edit map geometry',icon='EDITMODE_HLT')
            edit.operator('wz09.city_select_root', text='Select whole map (G / R)')
            edit.operator('wz09.city_mesh_edit', text='Edit selected mesh faces')
            edit.label(text='B: box select. L: linked faces.')
            edit.operator('wz09.city_extract', text='Extract selected faces as a part')
            edit.operator('wz09.city_split_loose', text='Split selected mesh into parts')
            edit.label(text='Split can be cancelled with Esc.')
            edit.label(text='Tab: finish editing before saving.')
            if selected and selected.get('wz09_compact_records_json'):
                a.operator('wz09.restore_compact_objects', text='Edit selected asset group')
            if not s.wz09_map_advanced: return
        split=a.row();split.scale_y=1.6
        split.operator('wz09.city_split_loose',text='Split into connected parts',icon='MOD_EXPLODE')
        row=a.row(align=True)
        row.operator('wz09.city_assets_load',text='Load map details',icon='IMPORT')
        row.operator('wz09.city_assets_unload',text='Save and unload map details',icon='HIDE_ON')
        a.operator('wz09.city_bake_preview',text='Rebuild map preview',icon='OUTLINER_OB_MESH')
        a.label(text='KARVORSK · MAP WORKSHOP',icon='WORLD')
        from . import mode_buttons
        mode_buttons.draw_buttons(a)
        grid=a.grid_flow(row_major=True,columns=3,even_columns=True,align=True)
        for value,label,ico in (('MAPS','Maps','WORLD'),('ASSETS','Assets','ASSET_MANAGER'),
                                ('WORLD','World','WORLD_DATA'),('ACTIONS','Aktionen','LIGHTPROBE_VOLUME'),
                                ('AUDIO','Audio','SPEAKER'),
                                ('PLACE','Marker','CURSOR'),('GEOMETRY','Wege','MESH_CUBE'),
                                ('ANIMATION','Animation','ACTION'),('CINEMATICS','Film','CAMERA_DATA'),
                                ('POI','Locationsnamen','FONT_DATA'),
                                ('NAV','Nav','TRACKING'),('EXPORT','Export','EXPORT')):
            grid.prop_enum(s,'wz09_workspace',value,text=label,icon=editor_ux.icon(ico))
        editor_ux.draw_quick_tools(ctx,a)
        mode=s.wz09_workspace
        if mode=='CINEMATICS' and s.get('wz09_cinematics_json'):
            from . import intro_controls
            intro_controls.draw_controls(a,ctx)
        if mode=='MAPS':
            if s.get('wz09_gulag_manifest'):
                a.label(text='Gulag campaign scenery - separate scene',icon='WORLD')
                a.label(text='Select props directly; G move / R rotate / S scale')
                a.operator('wz09.campaign_export',text='Export edited campaign environment',icon='EXPORT')
                a.label(text='Mission runtime and edited collision pending',icon='INFO')
            else:
                from . import city_editing
                city_editing.draw(a,ctx)
            a.operator('wz09.campaign_open_local',text='Open Gulag in separate scene',icon='IMPORT')
            if s.get('wz09_royale_scene_name'):
                a.operator('wz09.campaign_return_royale',text='Return to Karvorsk',icon='WORLD')
            a.prop(s,'wz09_show_advanced_import',text='Manual import / source evidence',icon='PREFERENCES')
            if s.wz09_show_advanced_import:
                a.prop(s,'wz09_map_choice',text='Source')
                a.operator('wz09.add_map',text='Add Empty Map Proxy',icon='ADD')
                a.operator('wz09.duplicate_map',icon='DUPLICATE')
                a.operator('wz09.load_calibration',icon='FILE_FOLDER')
                a.operator('wz09.load_rust_registration',icon='CHECKMARK')
                row=a.row(align=True)
                row.operator('wz09.import_visual_obj',text='OBJ',icon='IMPORT')
                row.operator('wz09.import_visual_gltf',text='glTF',icon='IMPORT')
                a.operator('wz09.repair_preview_winding',icon='MOD_NORMALEDIT')
        elif mode=='ASSETS':
            from . import asset_workspace
            asset_workspace.draw(a,ctx)
            from .asset_browser import draw_browser
            draw_browser(a,ctx)
        elif mode=='PLACE':
            a.operator('wz09.add_marker',text='Add Marker at 3D Cursor',icon='ADD')
            if selected and selected.get('wz09_type') in {'loot_crate','buy_station','poi','player_spawn','loadout_drop','mission','aircraft_keyframe'}:
                box=a.box();box.label(text='Selected Marker',icon='PROPERTIES')
                box.prop(selected,'["wz09_profile"]',text='Profile')
                for kind,prop,label in (('loot_crate','wz09_loot_table','Loot table'),('buy_station','wz09_shop_catalog','Shop catalog'),('poi','wz09_poi_name','POI name')):
                    if selected.get('wz09_type')==kind:box.prop(selected,'["'+prop+'"]',text=label)
                if 'wz09_properties_json' in selected:box.prop(selected,'["wz09_properties_json"]',text='Extra properties')
        elif mode=='NAV':
            a.operator('wz09.import_bot_waypoints',icon='IMPORT')
            a.operator('wz09.add_nav',text='Node at 3D Cursor',icon='ADD')
            a.prop(s,'wz09_edge_kind',text='New Link');a.prop(s,'wz09_bidirectional')
            row=a.row(align=True)
            row.operator('wz09.link_nodes',text='Connect',icon='LINKED').bidirectional=s.wz09_bidirectional
            row.operator('wz09.unlink_nodes',text='Unlink',icon='UNLINKED')
            a.operator('wz09.delete_nav',icon='TRASH');a.operator('wz09.repair_orphans',icon='FILE_REFRESH')
        elif mode=='GEOMETRY':
            a.prop(s,'wz09_road_length');a.prop(s,'wz09_road_width')
            a.operator('wz09.add_road',icon='ADD')
            if selected and selected.get('wz09_custom_geometry'):
                a.prop(selected,'["wz09_connector_from"]',text='From Map ID')
                a.prop(selected,'["wz09_connector_to"]',text='To Map ID')
        elif mode=='EXPORT':
            a.prop(s,'wz09_project_root',text='Editor project')
            a.operator('wz09.initialize',icon='FILE_REFRESH')
            a.prop(s,'wz09_geometry_path',text='GLB')
            a.operator('wz09.export_custom_geometry',icon='MESH_DATA')
            a.prop(s,'wz09_tactical_path',text='Tactical SVG')
            a.operator('wz09.work_bounds',icon='FULLSCREEN_ENTER');a.prop(s,'wz09_work_margin')
            a.prop(s,'wz09_use_work_bounds')
            if s.wz09_use_work_bounds:a.prop(s,'wz09_work_bounds',text='XY bounds')
            a.prop(s,'wz09_render_tactical_preview')
            a.operator('wz09.export_tactical',icon='IMAGE_DATA')
            a.prop(s,'wz09_export_path',text='JSON')
            a.operator('wz09.preflight',icon='CHECKMARK')
            a.operator('wz09.export_json',icon='EXPORT')
            a.operator('wz09.inspect_manifest',icon='VIEWZOOM')
            result=json.loads(s.get('wz09_preflight_json','{}'))
            if result:
                a.label(text=('Validation passed' if result.get('ok') else 'Validation needs attention'),icon='CHECKMARK' if result.get('ok') else 'ERROR')
        a.separator()
        a.operator('wz09.save_version',text='Save New Editor Version',icon='FILE_TICK')

classes=[WZ09_OT_init,WZ09_OT_add_map,WZ09_OT_add_marker,WZ09_OT_duplicate_map,
         WZ09_OT_delete_nav,WZ09_OT_repair_orphans,WZ09_OT_rust_registration,WZ09_OT_work_bounds,WZ09_OT_repair_preview,WZ09_OT_add_nav,
         WZ09_OT_calibrate,WZ09_OT_import_waypoints,WZ09_OT_link_nodes,WZ09_OT_unlink_nodes,
         WZ09_OT_import_obj,WZ09_OT_import_gltf,WZ09_OT_add_road,WZ09_OT_export_geometry,
         WZ09_OT_export_json,WZ09_OT_preflight,WZ09_OT_tactical,WZ09_PT_main]

def register():
    for cls in classes:bpy.utils.register_class(cls)
    bpy.types.Scene.wz09_workspace=EnumProperty(name='Workspace',items=[(v,l,'') for v,l in [('MAPS','Maps'),('ASSETS','Assets'),('WORLD','World'),('ACTIONS','Aktionen'),('AUDIO','Audio'),('PLACE','Place'),('GEOMETRY','Build'),('ANIMATION','Animation'),('CINEMATICS','Cinematics'),('POI','Locationsnamen'),('NAV','Navigation'),('EXPORT','Export')]],default='MAPS')
    bpy.types.Scene.wz09_show_advanced_import=BoolProperty(name='Manual import / source evidence',default=False)
    from .map_library import map_items
    bpy.types.Scene.wz09_map_choice=EnumProperty(items=map_items(),name='Map',default='mp_rust')
    bpy.types.Scene.wz09_marker_choice=EnumProperty(items=[(c,c,c) for c in ('loot_crate','buy_station','player_spawn','loadout_drop','mission','poi','aircraft_keyframe')],name='Marker',default='loot_crate')
    bpy.types.Scene.wz09_nav_scale=FloatProperty(name='Explicit map scale',default=0.0,min=0.0,precision=6,
        description='Verified MW2 source units to Blender meters. Zero blocks import.')
    bpy.types.Scene.wz09_project_root=StringProperty(name='Standalone editor project',default='',subtype='DIR_PATH')
    bpy.types.Scene.wz09_export_path=StringProperty(name='JSON file',default='exports/karvorsk.world.json',subtype='FILE_PATH')
    bpy.types.Scene.wz09_geometry_path=StringProperty(name='Custom GLB file',default='exports/karvorsk_connectors.glb',subtype='FILE_PATH')
    bpy.types.Scene.wz09_tactical_path=StringProperty(name='Tactical SVG',default='exports/karvorsk.tactical.svg',subtype='FILE_PATH')
    bpy.types.Scene.wz09_edge_kind=EnumProperty(name='Proposed edge kind',items=[(v,v,v) for v in ('walk','drop','climb','jump','mantle','break_glass')],default='walk')
    bpy.types.Scene.wz09_bidirectional=BoolProperty(name='Both directions',default=True)
    bpy.types.Scene.wz09_road_length=FloatProperty(name='Road length (m)',default=12,min=0.1)
    bpy.types.Scene.wz09_road_width=FloatProperty(name='Road width (m)',default=6,min=0.1)
    bpy.types.Scene.wz09_use_work_bounds=BoolProperty(name='Use explicit work area',default=False)
    bpy.types.Scene.wz09_work_bounds=FloatVectorProperty(name='XY work bounds',size=4,default=(-50,-50,50,50))
    bpy.types.Scene.wz09_work_margin=FloatProperty(name='Work area margin',default=5,min=0.1)
    bpy.types.Scene.wz09_render_tactical_preview=BoolProperty(name='Include orthographic preview image',default=False)
    from . import integration_ui,nav_workflow,aircraft_workflow,editor_ux,map_browser,asset_browser
    integration_ui.register()
    for cls in aircraft_workflow.classes:bpy.utils.register_class(cls)
    nav_workflow.register()
    editor_ux.register()
    map_browser.register()
    asset_browser.register()
    from . import composite_world,ground_capture,terrain_workflow,audio_zones,action_elements,native_semantics
    for module in (composite_world,ground_capture,terrain_workflow,audio_zones,action_elements,native_semantics):module.register()
    from . import authoring_overlay
    authoring_overlay.register()
    from . import native_visuals,native_audio_preview
    native_visuals.register();native_audio_preview.register()
    from . import asset_workspace,prefab_authoring,animation_studio,cinematic_studio,poi_labels,terrain_authoring,terrain_sculpt,terrain_routes,runtime_controls,preview_budget_ui,intro_controls
    for module in (asset_workspace,prefab_authoring,animation_studio,cinematic_studio,poi_labels,terrain_authoring,terrain_sculpt,terrain_routes,runtime_controls,preview_budget_ui,intro_controls):module.register()
    from . import cinematic_presentation
    cinematic_presentation.register()
    from . import mode_buttons
    mode_buttons.register()
    from . import kh_editor_tools
    kh_editor_tools.register()
    from . import campaign_environment
    campaign_environment.register()
    from . import city_editing
    city_editing.register()
    from . import map_residency
    map_residency.register()
    from . import installation_setup
    installation_setup.register()

def unregister():
    from . import installation_setup
    installation_setup.unregister()
    from . import map_residency
    map_residency.unregister()
    from . import city_editing
    city_editing.unregister()
    from . import kh_editor_tools
    from . import campaign_environment
    campaign_environment.unregister()
    kh_editor_tools.unregister()
    from . import mode_buttons
    mode_buttons.unregister()
    from . import cinematic_presentation
    cinematic_presentation.unregister()
    from . import asset_workspace,prefab_authoring,animation_studio,cinematic_studio,poi_labels,terrain_authoring,terrain_sculpt,terrain_routes,runtime_controls,preview_budget_ui,intro_controls
    for module in (intro_controls,preview_budget_ui,runtime_controls,terrain_routes,terrain_sculpt,terrain_authoring,poi_labels,cinematic_studio,animation_studio,prefab_authoring,asset_workspace):module.unregister()
    from . import native_visuals,native_audio_preview
    native_audio_preview.unregister();native_visuals.unregister()
    from . import authoring_overlay
    authoring_overlay.unregister()
    from . import composite_world,ground_capture,terrain_workflow,audio_zones,action_elements,native_semantics
    for module in (native_semantics,action_elements,audio_zones,terrain_workflow,ground_capture,composite_world):module.unregister()
    from . import integration_ui,nav_workflow,aircraft_workflow,editor_ux,map_browser,asset_browser
    asset_browser.unregister()
    map_browser.unregister()
    editor_ux.unregister()
    nav_workflow.unregister()
    for cls in reversed(aircraft_workflow.classes):bpy.utils.unregister_class(cls)
    integration_ui.unregister()
    for name in ('wz09_workspace','wz09_show_advanced_import','wz09_map_choice','wz09_marker_choice','wz09_nav_scale','wz09_project_root',
                 'wz09_export_path','wz09_geometry_path','wz09_tactical_path','wz09_edge_kind',
                 'wz09_bidirectional','wz09_road_length','wz09_road_width','wz09_use_work_bounds',
                 'wz09_work_bounds','wz09_work_margin','wz09_render_tactical_preview'):
        if hasattr(bpy.types.Scene,name):delattr(bpy.types.Scene,name)
    for cls in reversed(classes):bpy.utils.unregister_class(cls)

if __name__=='__main__':register()
