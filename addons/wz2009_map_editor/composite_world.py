"""Explicit map cores, reversible preview clipping and queued composite imports.

Pure clipping is available without Blender. No source mesh or glTF is edited.
An authored core is not a surveyed extent, terrain classifier or collider.
"""
import hashlib
import json
import math
import uuid

try:
    import bpy
    from bpy.props import BoolProperty, EnumProperty, FloatProperty, PointerProperty
except ImportError:
    bpy = None

_EPS = 1e-7
_LOAD_ACTIVE = False


def _finite(value, name, positive=False):
    if type(value) not in (int, float) or not math.isfinite(value) or (positive and value <= 0):
        raise ValueError(f'{name}: finite' + (' positive' if positive else '') + ' number required')
    return float(value)


def convex_polygon(points):
    """Validate explicit convex XY vertices and return counterclockwise order."""
    if len(points) < 3 or len(points) > 256:
        raise ValueError('Core boundary needs 3–256 convex polygon vertices')
    polygon = []
    for point in points:
        if len(point) != 2:
            raise ValueError('Core vertices require exactly X/Y')
        polygon.append(tuple(_finite(v, 'Core coordinate') for v in point))
    area = sum(a[0]*b[1]-b[0]*a[1] for a,b in zip(polygon,polygon[1:]+polygon[:1]))/2
    if abs(area) <= _EPS:
        raise ValueError('Core boundary has zero area')
    if area < 0:
        polygon.reverse()
    for i,a in enumerate(polygon):
        b = polygon[(i+1) % len(polygon)]
        if math.dist(a,b) <= _EPS:
            raise ValueError('Core boundary repeats a vertex')
        if any((b[0]-a[0])*(p[1]-a[1])-(b[1]-a[1])*(p[0]-a[0]) < -_EPS for p in polygon):
            raise ValueError('Core must be convex and cannot cross itself; concave shapes are not simplified')
    return polygon


def rectangle_polygon(width_m, depth_m):
    w,d = _finite(width_m,'Width',True)/2, _finite(depth_m,'Depth',True)/2
    return [(-w,-d),(w,-d),(w,d),(-w,d)]


def _interpolate(a,b,t):
    if isinstance(a,dict):
        return {key:_interpolate(a[key],b[key],t) for key in a}
    if isinstance(a,(tuple,list)):
        return tuple(_interpolate(x,y,t) for x,y in zip(a,b))
    return a+(b-a)*t


def clip_triangle(vertices,boundary,*,_validated=False):
    """Clip crossing triangles exactly, interpolating nested numeric attributes.

    Vertices have co=(x,y,z), optionally normals, UVs and colors. Result preserves
    winding. This uses edge intersections rather than triangle-centre rejection.
    """
    polygon=boundary if _validated else convex_polygon(boundary)
    if len(vertices)!=3:
        raise ValueError('Exactly three triangle vertices required')
    subject=[]
    for vertex in vertices:
        co=vertex.get('co',())
        if len(co)!=3:
            raise ValueError('Triangle requires three-dimensional coordinates')
        for value in co:
            _finite(value,'Triangle coordinate')
        subject.append(vertex)
    for a,b in zip(polygon,polygon[1:]+polygon[:1]):
        if not subject:
            break
        def side(vertex):
            p=vertex['co']
            return (b[0]-a[0])*(p[1]-a[1])-(b[1]-a[1])*(p[0]-a[0])
        clipped=[]
        previous=subject[-1]
        previous_d=side(previous)
        for current in subject:
            current_d=side(current)
            if (current_d>=-_EPS)!=(previous_d>=-_EPS):
                divisor=previous_d-current_d
                t=previous_d/divisor if abs(divisor)>1e-20 else 0
                clipped.append(_interpolate(previous,current,min(1,max(0,t))))
            if current_d>=-_EPS:
                clipped.append(current)
            previous,previous_d=current,current_d
        subject=[]
        for vertex in clipped:
            if not subject or math.dist(vertex['co'],subject[-1]['co'])>_EPS:
                subject.append(vertex)
        if len(subject)>1 and math.dist(subject[0]['co'],subject[-1]['co'])<=_EPS:
            subject.pop()
    result=[]
    for index in range(1,len(subject)-1):
        tri=[subject[0],subject[index],subject[index+1]]
        a,b,c=[v['co'] for v in tri]
        u,v=[b[i]-a[i] for i in range(3)],[c[i]-a[i] for i in range(3)]
        cross=(u[1]*v[2]-u[2]*v[1],u[2]*v[0]-u[0]*v[2],u[0]*v[1]-u[1]*v[0])
        if sum(x*x for x in cross)>1e-20:
            result.append(tri)
    return result


def point_inside(point,polygon,*,_validated=False):
    polygon=polygon if _validated else convex_polygon(polygon)
    return all((b[0]-a[0])*(point[1]-a[1])-(b[1]-a[1])*(point[0]-a[0])>=-_EPS
               for a,b in zip(polygon,polygon[1:]+polygon[:1]))


def _owner(obj):
    from .authoring_helpers import resolve_owner_map
    return resolve_owner_map(obj)


def _object_key(value):
    return value.as_pointer() if hasattr(value,'as_pointer') else id(value)


def _foreign_scene_index(scene,objects):
    """Index candidate object sharing once, without Object.users_scene scans.

    Blender's users_scene property materializes every scene's entire object
    list per call. Crop/restore may inspect tens of thousands of source meshes.
    The operation-scoped snapshot is linear in candidates and foreign scenes.
    None retains the small offline-fake fallback without a bpy scene registry.
    """
    scenes=getattr(getattr(bpy,'data',None),'scenes',None)
    if scenes is None:
        return None
    wanted={_object_key(obj) for obj in objects}
    if not wanted:
        return set()
    foreign=set()
    for owner in scenes:
        if _object_key(owner)==_object_key(scene):
            continue
        for obj in owner.objects:
            key=_object_key(obj)
            if key in wanted:
                foreign.add(key)
    return foreign


def _require_local(scene,obj,*,_foreign_keys=None):
    if getattr(obj,'library',None) is not None or getattr(obj,'override_library',None) is not None:
        raise ValueError('Choose local editable editor objects')
    foreign=_foreign_keys if _foreign_keys is not None else _foreign_scene_index(scene,[obj])
    shared=(_object_key(obj) in foreign if foreign is not None else
            any(_object_key(owner)!=_object_key(scene) for owner in getattr(obj,'users_scene',(scene,))))
    if shared:
        raise ValueError('Object belongs to multiple scenes; make it local before changing its preview')


def _region_local(region):
    owner=_owner(region)
    if owner is None:
        raise ValueError('Core region needs a map-instance parent')
    transform=owner.matrix_world.inverted()@region.matrix_world
    points=[transform@vertex.co for vertex in region.data.vertices]
    if not points or max(p.z for p in points)-min(p.z for p in points)>1e-4:
        raise ValueError('Core must be horizontal in map-local coordinates')
    return owner,convex_polygon([(p.x,p.y) for p in points]),sum(p.z for p in points)/len(points)


def _signature(region):
    owner,polygon,height=_region_local(region)
    payload=[str(owner.get('wz09_id','')),polygon,height,float(region.get('wz09_transition_m',40))]
    return hashlib.sha256(json.dumps(payload,sort_keys=True).encode()).hexdigest()


def create_core(ctx,owner=None,*,width_m=180,depth_m=180,transition_m=40,
                shape='RECTANGLE',confirmed=False,biome='soil'):
    """Create explicit core at cursor; huge source AABB never defines its size."""
    if bpy is None:
        raise RuntimeError('Blender required')
    from mathutils import Matrix
    owner=_owner(owner or ctx.active_object)
    if owner is None:
        raise ValueError('Select a map proxy or map object first')
    _require_local(ctx.scene,owner)
    if not any(o==owner for o in ctx.scene.objects):
        raise ValueError('Map must belong to active scene')
    transition=_finite(transition_m,'Transition width')
    if transition<=0:
        raise ValueError('Transition width must be positive')
    polygon=rectangle_polygon(width_m,depth_m)
    if shape=='CIRCLE':
        polygon=[(math.cos(i*math.tau/64)*width_m/2,math.sin(i*math.tau/64)*depth_m/2) for i in range(64)]
    elif shape!='RECTANGLE':
        raise ValueError('Core shape must be RECTANGLE or CIRCLE')
    if biome not in {'sand','soil','grass','snow','rock'}:
        raise ValueError('Choose a documented biome')
    mesh=bpy.data.meshes.new('WZ09_CoreBoundary')
    mesh.from_pydata([(x,y,0) for x,y in polygon],[],[tuple(range(len(polygon)))])
    mesh.update()
    obj=bpy.data.objects.new('Map core - '+owner.name,mesh)
    ctx.scene.collection.objects.link(obj)
    obj.parent=owner
    obj.matrix_parent_inverse=Matrix.Identity(4)
    obj.location=owner.matrix_world.inverted()@ctx.scene.cursor.location
    obj.display_type='WIRE'
    obj.show_in_front=True
    obj.hide_render=True
    obj.color=(.12,.9,.6,1)
    obj['wz09_editor_overlay']=True
    obj['wz09_region_kind']='map_core'
    obj['wz09_region_id']=str(uuid.uuid4())
    obj['wz09_core_shape']=shape
    obj['wz09_transition_m']=transition
    obj['wz09_biome']=biome
    obj['wz09_core_confirmed']=bool(confirmed)
    ctx.view_layer.update()
    if confirmed:
        obj['wz09_core_confirmed_sha']=_signature(obj)
    return obj


def serialize_regions(scene):
    """Terrain-compatible world-XY regions, carried with their owning maps."""
    records=[]
    seen=set()
    for obj in scene.objects:
        if obj.get('wz09_region_kind')!='map_core':
            continue
        identity=str(obj.get('wz09_region_id',''))
        if not identity or identity in seen:
            raise ValueError('Core ID missing or duplicated; repair IDs before export')
        seen.add(identity)
        owner,local,local_z=_region_local(obj)
        from mathutils import Vector
        world=[owner.matrix_world@Vector((x,y,local_z)) for x,y in local]
        if max(p.z for p in world)-min(p.z for p in world)>1e-4:
            raise ValueError('Terrain cores require horizontal map rotation; pitched maps need explicit ground extraction')
        transition=_finite(obj.get('wz09_transition_m',40),'Transition width')
        if transition<=0:
            raise ValueError('Core transition width must be positive')
        biome=str(obj.get('wz09_biome','soil'))
        if biome not in {'sand','soil','grass','snow','rock'}:
            raise ValueError('Core biome is invalid')
        record={'id':identity,'map_instance_id':str(owner.get('wz09_id','')),
            'shape':str(obj.get('wz09_core_shape','POLYGON')),
            'polygon':[list(p) for p in convex_polygon([(p.x,p.y) for p in world])],
            'polygon_local_m':[list(p) for p in local],
            'height_m':sum(p.z for p in world)/len(world),'biome':biome,'transition_m':transition,
            'confirmed':bool(obj.get('wz09_core_confirmed')) and obj.get('wz09_core_confirmed_sha')==_signature(obj),
            'source':'explicit_editor_design_region','physical_scale_verified':False,'runtime_integrated':False}
        local_heights=obj.get('wz09_core_boundary_heights_local_json')
        world_heights=obj.get('wz09_core_boundary_heights_json')
        if local_heights or world_heights:
            if obj.get('wz09_core_boundary_shape_sha')!=_signature(obj):
                raise ValueError('Core changed after ground sampling; resample its boundary heights')
            heights=json.loads(local_heights or world_heights)
            if len(heights)!=len(local):
                raise ValueError('Boundary-height samples no longer match edited core vertices; resample the ground')
            heights=[_finite(v,'Boundary height') for v in heights]
            record['boundary_heights_m']=[(owner.matrix_world@Vector((x,y,z))).z for (x,y),z in zip(local,heights)] if local_heights else heights
        if obj.get('wz09_core_ground_sample_json'):
            record['ground_capture']=json.loads(obj['wz09_core_ground_sample_json'])
        if obj.get('wz09_core_boundary_profile_json'):
            # Keep the actual Float32 core footprint unchanged. A source-bound
            # profile adds double-precision samples on those same edges only.
            if __package__:
                from .terrain_edges import profile_region_record
            else:
                from terrain_edges import profile_region_record
            record=profile_region_record(obj,owner,record,scene=scene)
        records.append(record)
    return sorted(records,key=lambda item:item['id'])


def map_region_records(scene):
    return serialize_regions(scene)


def region_bounds_world(region):
    owner,local,height=_region_local(region)
    from mathutils import Vector
    points=[owner.matrix_world@Vector((x,y,height)) for x,y in local]
    return min(p.x for p in points),min(p.y for p in points),max(p.x for p in points),max(p.y for p in points)


def _preview_objects(scene,owner):
    from .map_browser import is_map_preview_object
    return [obj for obj in scene.objects if is_map_preview_object(obj) and obj.type=='MESH'
            and _owner(obj)==owner and not obj.get('wz09_asset_placement_id')]


def restore_background(scene,owner,*,_objects=None,_foreign_keys=None):
    """Restore saved mesh pointers and visibility. Archives remain preserved."""
    objects=_preview_objects(scene,owner) if _objects is None else _objects
    foreign=_foreign_keys if _foreign_keys is not None else _foreign_scene_index(scene,objects)
    for obj in objects:
        _require_local(scene,obj,_foreign_keys=foreign)
    restored=0
    for obj in objects:
        original=getattr(obj,'wz09_core_original_mesh',None)
        if original is not None:
            derivative=obj.data
            obj.data=original
            obj.wz09_core_original_mesh=None
            if derivative!=original and derivative.users==0 and derivative.get('wz09_core_derived_preview'):
                bpy.data.meshes.remove(derivative)
            restored+=1
        if 'wz09_core_visibility_json' in obj:
            state=json.loads(obj['wz09_core_visibility_json'])
            obj.hide_set(bool(state['viewport']))
            obj.hide_render=bool(state['render'])
            del obj['wz09_core_visibility_json']
        for field in ('wz09_core_crop_region','wz09_core_crop_status'):
            if field in obj:
                del obj[field]
    return restored


def _clipped_mesh(obj,owner,boundary):
    """Exact clipping on a derivative; preserve corner UV/color/normal/material."""
    from mathutils import Vector
    mesh=obj.data
    mesh.calc_loop_triangles()
    to_owner=owner.matrix_world.inverted()@obj.matrix_world
    from_owner=to_owner.inverted()
    normals=mesh.corner_normals
    uvs=list(mesh.uv_layers)
    colors=[layer for layer in mesh.color_attributes if layer.domain in {'POINT','CORNER'}]
    positions,faces,face_records=[],[],[]
    for triangle in mesh.loop_triangles:
        payload=[]
        for vi,li in zip(triangle.vertices,triangle.loops):
            payload.append({'co':tuple(to_owner@mesh.vertices[vi].co),'normal':tuple(normals[li].vector),
                'uvs':{layer.name:tuple(layer.data[li].uv) for layer in uvs},
                'colors':{layer.name:tuple(layer.data[li if layer.domain=='CORNER' else vi].color) for layer in colors}})
        for tri in clip_triangle(payload,boundary,_validated=True):
            start=len(positions)
            positions.extend(tuple(from_owner@Vector(v['co'])) for v in tri)
            faces.append((start,start+1,start+2))
            face_records.append((triangle.material_index,mesh.polygons[triangle.polygon_index].use_smooth,tri))
    derived=bpy.data.meshes.new('WZ09_CorePreview')
    try:
        derived.from_pydata(positions,[],faces)
        derived.update()
        for material in mesh.materials:
            derived.materials.append(material)
        new_uvs={layer.name:derived.uv_layers.new(name=layer.name) for layer in uvs}
        new_colors={layer.name:derived.color_attributes.new(name=layer.name,type='FLOAT_COLOR',domain='CORNER') for layer in colors}
        corner_normals=[]
        for polygon,(material,smooth,tri) in zip(derived.polygons,face_records):
            polygon.material_index=material
            polygon.use_smooth=smooth
            for li,vertex in zip(polygon.loop_indices,tri):
                for name,layer in new_uvs.items():
                    layer.data[li].uv=vertex['uvs'][name]
                for name,layer in new_colors.items():
                    layer.data[li].color=vertex['colors'][name]
                normal=vertex['normal']
                length=math.sqrt(sum(x*x for x in normal))
                corner_normals.append(tuple(x/length for x in normal) if length>1e-12 else (0,0,1))
        if corner_normals:
            derived.normals_split_custom_set(corner_normals)
        derived['wz09_core_derived_preview']=True
        derived['wz09_core_source_data_name']=mesh.name_full
        derived.update()
        return derived
    except Exception:
        bpy.data.meshes.remove(derived)
        raise


def crop_background(scene,region):
    """Crop visuals horizontally at explicit core; no ground-name guessing.

    Crossing structures also clip. Cut edges remain uncapped. A visual preview
    crop never becomes collision or gameplay geometry. Transition space is for
    newly authored terrain rather than retention of enormous original backdrops.
    """
    from mathutils import Vector
    owner,boundary,_=_region_local(region)
    if not bool(region.get('wz09_core_confirmed')) or region.get('wz09_core_confirmed_sha')!=_signature(region):
        raise ValueError('Inspect the core and explicitly confirm it before cropping')
    objects=_preview_objects(scene,owner)
    foreign=_foreign_scene_index(scene,objects)
    for obj in objects:
        _require_local(scene,obj,_foreign_keys=foreign)
    restore_background(scene,owner,_objects=objects,_foreign_keys=foreign)
    plans,created=[],[]
    try:
        for obj in objects:
            transform=owner.matrix_world.inverted()@obj.matrix_world
            corners=[transform@Vector(p) for p in obj.bound_box]
            if all(point_inside((p.x,p.y),boundary,_validated=True) for p in corners):
                continue
            # All AABB corners outside does not imply nonintersection. Reject
            # whole objects only with a proven separating polygon edge.
            outside=any(all((b[0]-a[0])*(p.y-a[1])-(b[1]-a[1])*(p.x-a[0])<-_EPS for p in corners)
                        for a,b in zip(boundary,boundary[1:]+boundary[:1]))
            if outside:
                plans.append((obj,None))
            else:
                derived=_clipped_mesh(obj,owner,boundary)
                if len(derived.polygons):
                    created.append(derived)
                    plans.append((obj,derived))
                else:
                    bpy.data.meshes.remove(derived)
                    plans.append((obj,None))
        for obj,derived in plans:
            obj['wz09_core_visibility_json']=json.dumps({'viewport':obj.hide_get(),'render':obj.hide_render})
            obj['wz09_core_crop_region']=str(region['wz09_region_id'])
            obj['wz09_core_crop_status']='visual_only_uncapped'
            if derived is None or not derived.polygons:
                obj.hide_set(True)
                obj.hide_render=True
            else:
                obj.wz09_core_original_mesh=obj.data
                obj.data=derived
        return {'objects_inspected':len(objects),
            'outside_hidden':sum(1 for _,mesh in plans if mesh is None or not mesh.polygons),
            'meshes_clipped':sum(1 for _,mesh in plans if mesh is not None and len(mesh.polygons)>0),
            'source_meshes_unchanged':True,'collision_verified':False}
    except Exception:
        restore_background(scene,owner,_objects=objects,_foreign_keys=foreign)
        for mesh in created:
            try:
                if mesh.users==0:
                    bpy.data.meshes.remove(mesh)
            except ReferenceError:
                pass  # restore_background already released this derivative.
        raise


def create_composite_layout(ctx,spacing_m=400,region_gap_m=1600):
    """Add missing proxies in current scene; never reposition existing maps."""
    from .layout_geometry import design_layout
    from .map_library import MAP_NAMES
    import wz2009_map_editor as addon
    present={str(o.get('wz09_source_map')) for o in ctx.scene.objects if o.get('wz09_type')=='map_instance'}
    created=[]
    for record in design_layout(spacing_m,region_gap_m,tuple(ctx.scene.cursor.location)):
        if record['source_map'] in present:
            continue
        obj=addon.make_empty(ctx,dict(MAP_NAMES)[record['source_map']],'map_instance','map_instance',record['position_m'])
        obj['wz09_source_map']=record['source_map']
        obj['wz09_source_scale']=0.0
        obj['wz09_map_provenance_json']=json.dumps(record['provenance'],sort_keys=True)
        created.append(obj)
    ctx.scene['wz09_composite_status']=f'{len(created)} map proxies added; existing positions preserved'
    return created


if bpy is not None:
    class WZ09_OT_composite_layout(bpy.types.Operator):
        bl_idname='wz09.composite_layout'
        bl_label='Create all 16 map slots'
        bl_description='Add missing proxies to the world draft; preserve existing map positions and IDs'
        bl_options={'REGISTER','UNDO'}
        def execute(self,ctx):
            try:
                create_composite_layout(ctx,ctx.scene.wz09_world_spacing,ctx.scene.wz09_world_region_gap)
                return {'FINISHED'}
            except (ValueError,RuntimeError) as exc:
                self.report({'ERROR'},str(exc)[:240]);return {'CANCELLED'}

    class WZ09_OT_composite_load(bpy.types.Operator):
        bl_idname='wz09.composite_load'
        bl_label='Load maps sequentially'
        bl_description='Load exports into their proxies; Escape stops after the current map. A glTF import cannot be interrupted'
        selected_only:BoolProperty(default=False)
        _timer=None
        def execute(self,ctx):
            global _LOAD_ACTIVE
            if _LOAD_ACTIVE:
                self.report({'ERROR'},'A map loading queue is already running');return {'CANCELLED'}
            from .map_browser import _catalog
            maps=[obj for obj in ctx.scene.objects if obj.get('wz09_type')=='map_instance']
            if self.selected_only:
                maps=[obj for obj in maps if obj.select_get()]
            from .map_browser import is_map_preview_object
            loaded={_owner(obj).get('wz09_id') for obj in ctx.scene.objects
                    if is_map_preview_object(obj) and _owner(obj) is not None}
            entries={r['code']:r for r in _catalog(ctx.scene) if r.get('bundle_available')}
            self._jobs=[(str(obj['wz09_id']),str(obj['wz09_source_map']),entries[str(obj['wz09_source_map'])]['bundle'])
                for obj in maps if str(obj.get('wz09_source_map')) in entries and obj.get('wz09_id') not in loaded]
            if not self._jobs:
                self.report({'ERROR'},'No empty proxies with verified exports. Inspect the library and create map slots.');return {'CANCELLED'}
            self._scene=ctx.scene
            self._index,self._failures=0,[]
            self._scene['wz09_composite_cancel_requested']=False
            ctx.window_manager.progress_begin(0,len(self._jobs))
            self._timer=ctx.window_manager.event_timer_add(.2,window=ctx.window)
            ctx.window_manager.modal_handler_add(self)
            _LOAD_ACTIVE=True
            return {'RUNNING_MODAL'}
        def modal(self,ctx,event):
            if event.type=='ESC':
                self._scene['wz09_composite_cancel_requested']=True
            if ctx.scene!=self._scene or self._scene.get('wz09_composite_cancel_requested'):
                return self._finish(ctx,True)
            if event.type!='TIMER' or event.timer!=self._timer:
                return {'PASS_THROUGH'}
            from .map_browser import import_map_preview
            identity,code,bundle=self._jobs[self._index]
            owner=next((obj for obj in self._scene.objects if obj.get('wz09_id')==identity),None)
            self._scene['wz09_composite_status']=f'{self._index + 1}/{len(self._jobs)}: {code} loading - stop after this map'
            if owner is None:
                self._failures.append(code+': Proxy fehlt')
            else:
                try:
                    import_map_preview(ctx,code,bundle,target=owner,placement='ORIGIN')
                except Exception as exc:
                    self._failures.append(code+': '+str(exc)[:160])
            self._index+=1
            ctx.window_manager.progress_update(self._index)
            for area in ctx.screen.areas:
                area.tag_redraw()
            if self._index>=len(self._jobs):
                return self._finish(ctx,False)
            return {'RUNNING_MODAL'}
        def _finish(self,ctx,cancelled):
            global _LOAD_ACTIVE
            if self._timer is not None:
                ctx.window_manager.event_timer_remove(self._timer)
                self._timer=None
            ctx.window_manager.progress_end()
            _LOAD_ACTIVE=False
            self._scene['wz09_composite_status']=f"{('Gestoppt' if cancelled else 'Fertig')}: {self._index - len(self._failures)} maps loaded, {len(self._failures)} Fehler"
            self._scene['wz09_composite_errors_json']=json.dumps(self._failures)
            return {'FINISHED'}
        def cancel(self,ctx):
            self._finish(ctx,True)

    class WZ09_OT_composite_cancel(bpy.types.Operator):
        bl_idname='wz09.composite_cancel'
        bl_label='Stop after current map'
        def execute(self,ctx):
            ctx.scene['wz09_composite_cancel_requested']=True
            return {'FINISHED'}

    class WZ09_OT_core_create(bpy.types.Operator):
        bl_idname='wz09.core_create'
        bl_label='Create map core at cursor'
        bl_description='Explicit protected area, independent of large backgrounds. Edit with G/S/R or Tab'
        bl_options={'REGISTER','UNDO'}
        def execute(self,ctx):
            try:
                obj=create_core(ctx,width_m=ctx.scene.wz09_core_width,depth_m=ctx.scene.wz09_core_depth,
                    transition_m=ctx.scene.wz09_core_transition,shape=ctx.scene.wz09_core_shape,biome=ctx.scene.wz09_core_biome)
                bpy.ops.object.select_all(action='DESELECT')
                obj.select_set(True);ctx.view_layer.objects.active=obj
                return {'FINISHED'}
            except (ValueError,RuntimeError) as exc:
                self.report({'ERROR'},str(exc)[:240]);return {'CANCELLED'}

    class WZ09_OT_core_confirm(bpy.types.Operator):
        bl_idname='wz09.core_confirm'
        bl_label='Confirm inspected area'
        bl_description='Confirm the authored core. Confirm again after shape changes; this is not a native survey'
        bl_options={'REGISTER','UNDO'}
        def execute(self,ctx):
            obj=ctx.active_object
            if obj is None or obj.get('wz09_region_kind')!='map_core':
                self.report({'ERROR'},'Select a map core');return {'CANCELLED'}
            try:
                _require_local(ctx.scene,obj)
                if obj.mode=='EDIT':
                    obj.update_from_editmode()
                ctx.view_layer.update()
                obj['wz09_core_confirmed_sha']=_signature(obj)
                obj['wz09_core_confirmed']=True
                return {'FINISHED'}
            except ValueError as exc:
                self.report({'ERROR'},str(exc)[:240]);return {'CANCELLED'}

    class WZ09_OT_core_crop(bpy.types.Operator):
        bl_idname='wz09.core_crop'
        bl_label='Hide background outside core'
        bl_description='Clip the preview exactly to the confirmed core; original meshes are retained. Buildings at the edge are also clipped'
        bl_options={'REGISTER','UNDO'}
        restore:BoolProperty(default=False)
        def execute(self,ctx):
            try:
                obj=ctx.active_object
                owner=_owner(obj)
                if owner is None:
                    raise ValueError('Select a map or map core')
                if self.restore:
                    count=restore_background(ctx.scene,owner)
                    self.report({'INFO'},f'Original background restored; {count} Meshbindungen restauriert')
                else:
                    if obj.get('wz09_region_kind')!='map_core':
                        raise ValueError('Select the confirmed map core')
                    result=crop_background(ctx.scene,obj)
                    self.report({'INFO'},f'{result["outside_hidden"]} ausgeblendet · {result["meshes_clipped"]} Vorschaukopien beschnitten')
                return {'FINISHED'}
            except (ValueError,RuntimeError) as exc:
                self.report({'ERROR'},str(exc)[:240]);return {'CANCELLED'}

    class WZ09_OT_composite_visibility(bpy.types.Operator):
        bl_idname='wz09.composite_visibility'
        bl_label='Map visibility'
        bl_options={'REGISTER','UNDO'}
        action:EnumProperty(items=[('SOLO','This map only',''),('SHOW_ALL','Show all maps',''),('HIDE','Diese Map ausblenden','')])
        def execute(self,ctx):
            owner=_owner(ctx.active_object)
            if self.action!='SHOW_ALL' and owner is None:
                self.report({'ERROR'},'Select a map or map object');return {'CANCELLED'}
            for obj in ctx.scene.objects:
                object_owner=_owner(obj)
                if object_owner is None:
                    continue
                if self.action=='HIDE':
                    if object_owner==owner:
                        obj.hide_set(True)
                else:
                    crop_hidden=('wz09_core_visibility_json' in obj and obj.get('wz09_core_crop_status') and obj.hide_render)
                    obj.hide_set(bool(crop_hidden) or (self.action=='SOLO' and object_owner!=owner))
            return {'FINISHED'}

    class WZ09_PT_composite_world(bpy.types.Panel):
        bl_label='World composition - map cores'
        bl_idname='WZ09_PT_composite_world'
        bl_space_type='VIEW_3D'
        bl_region_type='UI'
        bl_category='WZ2009'
        bl_parent_id='WZ09_PT_main'
        @classmethod
        def poll(cls,ctx):
            return getattr(ctx.scene,'wz09_workspace','MAPS') in {'WORLD','MAPS'}
        def draw(self,ctx):
            layout,scene=self.layout,ctx.scene
            row=layout.row(align=True)
            row.prop(scene,'wz09_world_spacing',text='Map spacing')
            row.prop(scene,'wz09_world_region_gap',text='Regions')
            layout.operator('wz09.composite_layout',icon='OUTLINER_COLLECTION')
            row=layout.row(align=True)
            row.enabled=not _LOAD_ACTIVE
            row.operator('wz09.composite_load',text='Load all',icon='IMPORT').selected_only=False
            row.operator('wz09.composite_load',text='Load selection',icon='RESTRICT_SELECT_OFF').selected_only=True
            if _LOAD_ACTIVE:
                layout.operator('wz09.composite_cancel',icon='CANCEL')
            layout.label(text=str(scene.get('wz09_composite_status','Use verified exports from the map library.'))[:90],icon='INFO')
            row=layout.row(align=True)
            for action,label,icon in [('SOLO','Solo','SOLO_ON'),('SHOW_ALL','All','HIDE_OFF'),('HIDE','Ausblenden','HIDE_ON')]:
                row.operator('wz09.composite_visibility',text=label,icon=icon).action=action
            box=layout.box()
            box.label(text='Define the play area explicitly',icon='MOD_BOOLEAN')
            box.prop(scene,'wz09_core_shape',text='Shape')
            row=box.row(align=True)
            row.prop(scene,'wz09_core_width',text='Width')
            row.prop(scene,'wz09_core_depth',text='Depth')
            box.prop(scene,'wz09_core_transition',text='Terrain transition')
            box.prop(scene,'wz09_core_biome',text='Ground')
            box.operator('wz09.core_create',icon='MESH_PLANE')
            obj=ctx.active_object
            if obj is not None and obj.get('wz09_region_kind')=='map_core':
                box.label(text='G: move - S: scale - Tab: edit vertices',icon='EDITMODE_HLT')
                box.prop(obj,'["wz09_transition_m"]',text='Transition')
                box.prop(obj,'["wz09_biome"]',text='Ground profile')
                box.operator('wz09.core_confirm',icon='CHECKMARK')
                box.operator('wz09.core_crop',icon='MOD_MASK').restore=False
            box.operator('wz09.core_crop',text='Restore original background',icon='LOOP_BACK').restore=True
            box.label(text='The core excludes the large original background.',icon='INFO')
            box.label(text='Clipping affects previews; collision is unverified.',icon='INFO')

    CLASSES=(WZ09_OT_composite_layout,WZ09_OT_composite_load,WZ09_OT_composite_cancel,WZ09_OT_core_create,
             WZ09_OT_core_confirm,WZ09_OT_core_crop,WZ09_OT_composite_visibility,WZ09_PT_composite_world)
else:
    CLASSES=()


def register():
    if bpy is None:
        return
    bpy.types.Object.wz09_core_original_mesh=PointerProperty(type=bpy.types.Mesh)
    props={
        'wz09_world_spacing':FloatProperty(name='Map spacing',default=400,min=1,subtype='DISTANCE'),
        'wz09_world_region_gap':FloatProperty(name='Region spacing',default=1600,min=1,subtype='DISTANCE'),
        'wz09_core_width':FloatProperty(name='Core width',default=180,min=.1,subtype='DISTANCE'),
        'wz09_core_depth':FloatProperty(name='Core depth',default=180,min=.1,subtype='DISTANCE'),
        'wz09_core_transition':FloatProperty(name='Transition width',default=40,min=.1,subtype='DISTANCE'),
        'wz09_core_shape':EnumProperty(name='Core shape',items=[('RECTANGLE','Rectangle','Press Tab to edit vertices'),('CIRCLE','Circle / ellipse','64-seitige editierbare Grenze')],default='RECTANGLE'),
        'wz09_core_biome':EnumProperty(name='Ground profile',items=[(x,x.title(),'') for x in ('sand','soil','grass','snow','rock')],default='soil')}
    for name,prop in props.items():
        setattr(bpy.types.Scene,name,prop)
    for cls in CLASSES:
        bpy.utils.register_class(cls)


def unregister():
    global _LOAD_ACTIVE
    _LOAD_ACTIVE=False
    if bpy is None:
        return
    for cls in reversed(CLASSES):
        bpy.utils.unregister_class(cls)
    for name in ('wz09_world_spacing','wz09_world_region_gap','wz09_core_width','wz09_core_depth','wz09_core_transition','wz09_core_shape','wz09_core_biome'):
        if hasattr(bpy.types.Scene,name):
            delattr(bpy.types.Scene,name)
    if hasattr(bpy.types.Object,'wz09_core_original_mesh'):
        del bpy.types.Object.wz09_core_original_mesh
