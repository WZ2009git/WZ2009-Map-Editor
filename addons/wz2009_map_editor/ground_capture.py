"""Explicit ground-face capture; never classify roofs or terrain by heuristics.

Only user-selected Edit Mode faces supply height rays. Captures are hidden
derived editor overlays and preserve their source/selection fingerprint.
"""
import hashlib
import json
import math
import uuid

try:
    import bpy
    from bpy.props import PointerProperty
except ImportError:
    bpy=None


def _number(value,name):
    if type(value) not in (int,float) or not math.isfinite(value):
        raise ValueError(name+': finite number required')
    return float(value)


def boundary_heights(polygon,min_z,max_z,ray_cast,minimum_up=.05):
    """Sample only an explicit supplied BVH, with atomic all-hit validation.

    ray_cast takes origin,direction,distance and returns position,normal,index,
    distance as mathutils.BVHTree does. Missing/vertical/nonfinite hits reject the
    complete capture, rather than inventing fallback ground heights.
    """
    low,high=_number(min_z,'Ground minimum'),_number(max_z,'Ground maximum')
    if high<low:
        raise ValueError('Ground height bounds are reversed')
    minimum=_number(minimum_up,'Minimum upward normal')
    if not 0<minimum<=1:
        raise ValueError('Minimum upward normal must be within (0,1]')
    if len(polygon)<3:
        raise ValueError('Ground sampling needs at least three core vertices')
    margin=max(1,(high-low)*.01)
    result=[]
    for index,point in enumerate(polygon):
        if len(point)!=2:
            raise ValueError('Core sample requires XY coordinates')
        x,y=[_number(v,'Sample coordinate') for v in point]
        hit=ray_cast((x,y,high+margin),(0,0,-1),high-low+2*margin)
        if hit is None or len(hit)!=4 or hit[0] is None:
            raise ValueError(f'Core corner {index+1}: no selected ground below boundary; extend explicit ground selection or move core')
        position,normal,face,distance=hit
        if len(position)!=3 or len(normal)!=3:
            raise ValueError(f'Core corner {index+1}: invalid ground ray result')
        position=[_number(v,'Ground hit') for v in position]
        normal=[_number(v,'Ground normal') for v in normal]
        distance=_number(distance,'Ground distance')
        length=math.sqrt(sum(v*v for v in normal))
        if length<=1e-12 or abs(normal[2])/length<minimum:
            raise ValueError(f'Core corner {index+1}: selected surface is too vertical')
        if distance<0 or not low-1e-4<=position[2]<=high+1e-4 or math.hypot(position[0]-x,position[1]-y)>1e-3:
            raise ValueError(f'Core corner {index+1}: ground ray left selected patch bounds')
        result.append(position[2])
    return result


def capture_fingerprint(vertices,faces,sources):
    """Bind exact derived coordinates/topology and stable source IDs, not names."""
    payload={'vertices':vertices,'faces':faces,'sources':sources}
    return hashlib.sha256(json.dumps(payload,sort_keys=True,separators=(',',':'),allow_nan=False).encode()).hexdigest()


def capture_selected_ground(ctx,core=None):
    """Read selected bmesh faces, sample all corners, then add one new overlay."""
    if bpy is None:
        raise RuntimeError('Blender required')
    import bmesh
    from mathutils import Matrix,Vector
    from mathutils.bvhtree import BVHTree
    from . import composite_world
    from .authoring_helpers import resolve_owner_map
    active=ctx.active_object
    if active is None or active.type!='MESH' or active.mode!='EDIT':
        raise ValueError('Select ground faces explicitly in Edit Mode, then sample the ground boundary')
    owner=resolve_owner_map(active)
    if owner is None:
        raise ValueError('Selected ground needs a map-instance parent')
    if core is None:
        core=getattr(ctx.scene,'wz09_ground_core',None)
    if core is None:
        candidates=[obj for obj in ctx.scene.objects if obj.get('wz09_region_kind')=='map_core' and resolve_owner_map(obj)==owner]
        if len(candidates)!=1:
            raise ValueError('Choose the target map core; automatic selection requires exactly one core for this map')
        core=candidates[0]
    if core.get('wz09_region_kind')!='map_core' or resolve_owner_map(core)!=owner:
        raise ValueError('Target core must belong to selected ground map')
    composite_world._require_local(ctx.scene,core)
    _,polygon,_=composite_world._region_local(core)
    ctx.view_layer.update()
    vertices,faces,sources=[],[],[]
    objects=getattr(ctx,'objects_in_mode_unique_data',(active,))
    for obj in objects:
        if obj.type!='MESH' or obj.mode!='EDIT':
            continue
        bm=bmesh.from_edit_mesh(obj.data)
        selected=[face for face in bm.faces if face.select and not face.hide]
        if not selected:
            continue
        if resolve_owner_map(obj)!=owner:
            raise ValueError('Capture ground from only one map at a time')
        composite_world._require_local(ctx.scene,obj)
        transform=owner.matrix_world.inverted()@obj.matrix_world
        vertex_map={}
        for face in selected:
            points=[transform@v.co for v in face.verts]
            # Polygon normal from transformed positions, independent of source
            # matrix scaling. Reject explicit walls; do not call a roof ground.
            normal=Vector((0,0,0))
            for a,b in zip(points,points[1:]+points[:1]):
                normal.x+=(a.y-b.y)*(a.z+b.z)
                normal.y+=(a.z-b.z)*(a.x+b.x)
                normal.z+=(a.x-b.x)*(a.y+b.y)
            if normal.length<=1e-12 or abs(normal.normalized().z)<.05:
                raise ValueError('Ground selection contains degenerate or nearly vertical faces; select only intended ground')
            indices=[]
            for vert,point in zip(face.verts,points):
                if vert not in vertex_map:
                    vertex_map[vert]=len(vertices)
                    vertices.append(tuple(float(v) for v in point))
                indices.append(vertex_map[vert])
            faces.append(tuple(indices))
        sources.append({'preview_id':str(obj.get('wz09_preview_id','')),
            'geometry_id':str(obj.get('wz09_geometry_id','')),
            'mesh_vertex_count':len(obj.data.vertices),'selected_face_count':len(selected),
            'source_preview_sha256':str(owner.get('wz09_preview_sha256',''))})
    if not faces:
        raise ValueError('No visible ground faces selected')
    signature=composite_world._signature(core)
    fingerprint=capture_fingerprint(vertices,faces,sources)
    bvh=BVHTree.FromPolygons([Vector(p) for p in vertices],faces,all_triangles=False,epsilon=0.0)
    heights=boundary_heights(polygon,min(p[2] for p in vertices),max(p[2] for p in vertices),bvh.ray_cast)
    # Everything above is read-only. No partial capture exists on failed rays.
    mesh=bpy.data.meshes.new('WZ09_SelectedGround')
    obj=None
    fields=('wz09_core_boundary_heights_local_json','wz09_core_boundary_shape_sha','wz09_core_ground_patch_id',
            'wz09_core_ground_sample_json','wz09_core_confirmed')
    previous={key:core[key] for key in fields if key in core}
    try:
        mesh.from_pydata(vertices,[],faces)
        mesh.update()
        obj=bpy.data.objects.new('WZ09_SelectedGround',mesh)
        ctx.scene.collection.objects.link(obj)
        obj.parent=owner
        obj.matrix_parent_inverse=Matrix.Identity(4)
        obj.matrix_basis=Matrix.Identity(4)
        obj['wz09_editor_overlay']=True
        obj['wz09_ground_patch']=True
        obj['wz09_ground_patch_id']=str(uuid.uuid4())
        obj['wz09_ground_core_id']=str(core['wz09_region_id'])
        obj['wz09_ground_fingerprint']=fingerprint
        obj['wz09_ground_source_json']=json.dumps(sources,sort_keys=True)
        obj.hide_render=True
        obj.hide_set(True)
        core['wz09_core_boundary_heights_local_json']=json.dumps(heights)
        core['wz09_core_boundary_shape_sha']=signature
        core['wz09_core_ground_patch_id']=str(obj['wz09_ground_patch_id'])
        core['wz09_core_ground_sample_json']=json.dumps({'kind':'explicit_selected_ground_faces',
            'patch_id':obj['wz09_ground_patch_id'],'fingerprint':fingerprint,'source_meshes':sources,
            'selected_faces':len(faces),'boundary_samples':len(heights),
            'collision_verified':False,'physical_scale_verified':False,'runtime_integrated':False},sort_keys=True)
        core['wz09_core_confirmed']=False
        return {'patch':obj,'core':core,'samples':len(heights),'faces':len(faces),'fingerprint':fingerprint}
    except Exception:
        for key in fields:
            if key in previous:
                core[key]=previous[key]
            elif key in core:
                del core[key]
        if obj is not None:
            bpy.data.objects.remove(obj,do_unlink=True)
        if mesh.users==0:
            bpy.data.meshes.remove(mesh)
        raise


if bpy is not None:
    class WZ09_OT_ground_capture(bpy.types.Operator):
        bl_idname='wz09.ground_capture'
        bl_label='Sample selected ground boundary'
        bl_description='Read only explicitly selected ground faces; roofs are not classified as terrain. Missing corner hits cancel the operation'
        bl_options={'REGISTER','UNDO'}
        def execute(self,ctx):
            try:
                result=capture_selected_ground(ctx)
                ctx.scene['wz09_ground_capture_status']=f"{result['faces']} ground faces - {result['samples']} boundary heights saved; confirm the core next"
                self.report({'INFO'},ctx.scene['wz09_ground_capture_status'])
                return {'FINISHED'}
            except (ValueError,RuntimeError) as exc:
                self.report({'ERROR'},str(exc)[:240]);return {'CANCELLED'}

    class WZ09_PT_ground_capture(bpy.types.Panel):
        bl_label='Capture ground from explicit selection'
        bl_idname='WZ09_PT_ground_capture'
        bl_space_type='VIEW_3D'
        bl_region_type='UI'
        bl_category='WZ2009'
        bl_parent_id='WZ09_PT_main'
        @classmethod
        def poll(cls,ctx):
            return getattr(ctx.scene,'wz09_workspace','MAPS')=='WORLD'
        def draw(self,ctx):
            layout=self.layout
            layout.prop(ctx.scene,'wz09_ground_core',text='Target core')
            layout.label(text='1. Define the map core.',icon='MESH_PLANE')
            layout.label(text='2. Tab: select ground faces only.',icon='FACESEL')
            layout.operator('wz09.ground_capture',icon='SNAP_FACE')
            layout.label(text='3. Object Mode: inspect and confirm the core.',icon='CHECKMARK')
            if ctx.scene.get('wz09_ground_capture_status'):
                layout.label(text=str(ctx.scene['wz09_ground_capture_status'])[:85],icon='INFO')
    CLASSES=(WZ09_OT_ground_capture,WZ09_PT_ground_capture)
else:
    CLASSES=()


def register():
    if bpy is None:
        return
    bpy.types.Scene.wz09_ground_core=PointerProperty(name='Target core',type=bpy.types.Object,
        poll=lambda self,obj:obj.get('wz09_region_kind')=='map_core')
    for cls in CLASSES:
        bpy.utils.register_class(cls)


def unregister():
    if bpy is None:
        return
    for cls in reversed(CLASSES):
        bpy.utils.unregister_class(cls)
    if hasattr(bpy.types.Scene,'wz09_ground_core'):
        del bpy.types.Scene.wz09_ground_core
