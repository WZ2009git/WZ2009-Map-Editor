"""Editor preview of the current game's tile-based terrain render input.

Read-only port of the documented grid topology and hole decision in
session/src/karvorsk_collision.rs and sim/src/karvorsk_clip.rs (48c8926).
Does not replace, rebuild or certify gameplay collision.
"""
import hashlib
import math
import struct


def within(point, polygon, margin):
    x,y=point
    sides=[(b[0]-a[0])*(y-a[1])-(b[1]-a[1])*(x-a[0]) for a,b in zip(polygon,polygon[1:]+polygon[:1])]
    if all(s>=-1e-8 for s in sides) or all(s<=1e-8 for s in sides):
        return True
    for a,b in zip(polygon,polygon[1:]+polygon[:1]):
        dx,dy=b[0]-a[0],b[1]-a[1]
        t=max(0,min(1,((x-a[0])*dx+(y-a[1])*dy)/(dx*dx+dy*dy)))
        if math.hypot(x-a[0]-t*dx,y-a[1]-t*dy)<=margin:
            return True
    return False


def mesh_data(folder, terrain, loaded_map_ids):
    vertices,faces,surfaces=[],[],[]
    skipped=0; cores=[]
    for core in terrain['collision_cores']:
        if core['map_instance_id'] not in loaded_map_ids:
            continue
        angle=math.radians(core['yaw_deg']);c,s=math.cos(angle),math.sin(angle)
        ox,oy=core['position_m'][:2]
        cores.append([(ox+c*x-s*y,oy+s*x+c*y) for x,y in core['polygon_local_m']])
    def data(ref):
        path=(folder/ref['path']).resolve();path.relative_to(folder.resolve())
        payload=path.read_bytes()
        if len(payload)!=ref['byte_length'] or hashlib.sha256(payload).hexdigest()!=ref['sha256']:
            raise ValueError('Changed terrain source: '+ref['path'])
        return payload
    for tile in terrain['tiles']:
        heights=struct.unpack('<1089f',data(tile['height_samples']))
        holes=data(tile['hole_mask']);slots=data(tile['surface_mask'])
        if len(holes)!=1024 or len(slots)!=1024:
            raise ValueError('Unexpected terrain mask dimensions')
        base=len(vertices);ox,oy=tile['origin_m'][:2];cell=tile['cell_size_m']
        vertices.extend((ox+x*cell,oy+y*cell,heights[y*33+x]) for y in range(33) for x in range(33))
        for y in range(32):
            for x in range(32):
                i=y*32+x;center=(ox+(x+.5)*cell,oy+(y+.5)*cell)
                if holes[i] and any(within(center,poly,34) for poly in cores):
                    skipped+=1;continue
                a=base+y*33+x;b=a+1;c=a+33;d=c+1
                faces.extend(((a,b,d),(a,d,c)));surfaces.extend((slots[i],slots[i]))
    return vertices,faces,surfaces,dict(tile_count=len(terrain['tiles']),
        triangles=len(faces),open_native_cells=skipped,
        native_loaded_assumption=sorted(loaded_map_ids),source='runtime_collision_tiles')


def install(scene, folder, terrain, loaded_map_ids):
    import bpy
    vertices,faces,surfaces,stats=mesh_data(folder,terrain,loaded_map_ids)
    mesh=bpy.data.meshes.new('Current runtime terrain grid')
    mesh.from_pydata(vertices,[],faces);mesh.update()
    palette={'sand':(.45,.36,.22,1),'dirt':(.28,.24,.17,1),'grass':(.18,.24,.11,1),
             'snow':(.75,.79,.81,1),'rock':(.29,.27,.24,1),'asphalt':(.08,.08,.085,1)}
    for name in terrain['surface_table']:
        mat=bpy.data.materials.get('Runtime terrain · '+name)
        if mat is None:
            mat=bpy.data.materials.new('Runtime terrain · '+name)
            mat.diffuse_color=palette.get(name,(.25,.28,.18,1))
            mat.use_nodes=True
            bsdf=next(n for n in mat.node_tree.nodes if n.type=='BSDF_PRINCIPLED')
            bsdf.inputs['Base Color'].default_value=mat.diffuse_color
            bsdf.inputs['Roughness'].default_value=.95
        mesh.materials.append(mat)
    mesh.polygons.foreach_set('material_index',surfaces)
    obj=bpy.data.objects.new('KARVORSK · terrain as rendered by current game',mesh)
    scene.collection.objects.link(obj)
    obj['wz09_preview_only']=True
    obj['wz09_current_runtime_tile_preview']=True
    # Actual authoritative data remains the unchanged bound package. This mesh
    # is excluded from custom GLB export to avoid duplicating that terrain.
    for old in list(scene.objects):
        if old != obj and old.get('wz09_current_runtime_terrain'):
            old.hide_set(True);old.hide_render=True
            old['wz09_authoring_terrain']=True
            old['wz09_custom_geometry']=False
    return stats
