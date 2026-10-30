"""Array-based preview clipping; interpolate only triangles crossing the edge.

Same convex clipper and UV/color/normal semantics as composite_world._clipped_mesh.
Avoid constructing Python dictionaries for every interior vertex of dense BSPs.
"""
def outside_triangle(vertices, boundary):
    """Partition a triangle outside a convex CCW boundary without overlaps."""
    from .composite_world import _interpolate
    pending = list(vertices)
    result = []
    for a, b in zip(boundary, boundary[1:] + boundary[:1]):
        if not pending:
            break
        inner, outer = [], []
        def side(v):
            return (b[0]-a[0])*(v['co'][1]-a[1])-(b[1]-a[1])*(v['co'][0]-a[0])
        for previous, current in zip(pending[-1:] + pending[:-1], pending):
            p, c = side(previous), side(current)
            if (p >= 0) != (c >= 0):
                cut = _interpolate(previous, current, p/(p-c))
                inner.append(cut); outer.append(cut)
            (inner if c >= 0 else outer).append(current)
        result.extend([outer[0], outer[i], outer[i+1]] for i in range(1, len(outer)-1))
        pending = inner
    return result


def clipped_mesh(obj, owner, boundary, *, outside=False):
    import bpy
    import numpy as np
    from mathutils import Vector
    from .composite_world import clip_triangle
    source=obj.data;source.calc_loop_triangles()
    to_owner=owner.matrix_world.inverted()@obj.matrix_world
    inverse=to_owner.inverted()
    def array(collection, field, width, dtype=np.float32):
        data=np.empty(len(collection)*width,dtype=dtype)
        collection.foreach_get(field,data)
        return data.reshape((-1,width)) if width>1 else data
    positions=array(source.vertices,'co',3)
    matrix=np.array(to_owner,dtype=np.float64)
    local=positions@matrix[:3,:3].T+matrix[:3,3]
    triangles=array(source.loop_triangles,'vertices',3,np.int32)
    loops=array(source.loop_triangles,'loops',3,np.int32)
    material=array(source.loop_triangles,'material_index',1,np.int32)
    polygon=array(source.loop_triangles,'polygon_index',1,np.int32)
    smooth=array(source.polygons,'use_smooth',1,np.bool_)[polygon]
    inside_vertex=np.ones(len(positions),dtype=np.bool_)
    wholly_outside=np.zeros(len(triangles),dtype=np.bool_)
    for a,b in zip(boundary,boundary[1:]+boundary[:1]):
        side=(b[0]-a[0])*(local[:,1]-a[1])-(b[1]-a[1])*(local[:,0]-a[0])
        inside_vertex &= side>=-1e-7
        wholly_outside |= (side[triangles]<-1e-7).all(axis=1)
    inside=inside_vertex[triangles].all(axis=1)
    crossing=np.flatnonzero(~inside & ~wholly_outside)
    selected = wholly_outside if outside else inside
    inside_tri=np.flatnonzero(selected)
    used,remap=np.unique(triangles[selected].reshape(-1),return_inverse=True)
    base_positions=positions[used]
    faces=remap.reshape((-1,3)).tolist()
    corner_indices=loops[selected].reshape(-1)
    normals=array(source.corner_normals,'vector',3)
    uv={layer.name:array(layer.data,'uv',2) for layer in source.uv_layers}
    colors={layer.name:(layer.domain,array(layer.data,'color',4)) for layer in source.color_attributes
            if layer.domain in {'POINT','CORNER'}}
    extra_positions=[];extra_normals=[];extra_uv={name:[] for name in uv};extra_colors={name:[] for name in colors}
    extra_material=[];extra_smooth=[]
    for ti in crossing:
        payload=[]
        for vi,li in zip(triangles[ti],loops[ti]):
            payload.append(dict(co=tuple(float(v) for v in local[vi]),normal=tuple(normals[li]),
                uvs={name:tuple(values[li]) for name,values in uv.items()},
                colors={name:tuple(values[li if domain=='CORNER' else vi]) for name,(domain,values) in colors.items()}))
        fragments = outside_triangle(payload,boundary) if outside else clip_triangle(payload,boundary,_validated=True)
        for tri in fragments:
            start=len(base_positions)+len(extra_positions)
            faces.append((start,start+1,start+2))
            extra_material.append(material[ti]);extra_smooth.append(smooth[ti])
            for vertex in tri:
                extra_positions.append(tuple(inverse@Vector(vertex['co'])))
                n=np.array(vertex['normal']);length=np.linalg.norm(n)
                extra_normals.append(n/length if length>1e-12 else (0,0,1))
                for name in uv:extra_uv[name].append(vertex['uvs'][name])
                for name in colors:extra_colors[name].append(vertex['colors'][name])
    def joined(first,extra,width,dtype=np.float32):
        return np.concatenate((np.asarray(first,dtype=dtype).reshape((-1,width)),
                               np.asarray(extra,dtype=dtype).reshape((-1,width))),axis=0)
    points=joined(base_positions,extra_positions,3)
    face_array=np.array(faces,dtype=np.int32).reshape((-1,3))
    derived=bpy.data.meshes.new('WZ09_CorePreview_compact')
    try:
        derived.vertices.add(len(points));derived.vertices.foreach_set('co',points.reshape(-1))
        derived.loops.add(len(face_array)*3);derived.loops.foreach_set('vertex_index',face_array.reshape(-1))
        derived.polygons.add(len(face_array))
        derived.polygons.foreach_set('loop_start',np.arange(len(face_array),dtype=np.int32)*3)
        derived.polygons.foreach_set('loop_total',np.full(len(face_array),3,dtype=np.int32))
        derived.update(calc_edges=True)
        for mat in source.materials:derived.materials.append(mat)
        derived.polygons.foreach_set('material_index',np.concatenate((material[inside_tri],extra_material)).astype(np.int32))
        derived.polygons.foreach_set('use_smooth',np.concatenate((smooth[inside_tri],extra_smooth)).astype(np.bool_))
        for name,values in uv.items():
            layer=derived.uv_layers.new(name=name)
            layer.data.foreach_set('uv',joined(values[corner_indices],extra_uv[name],2).reshape(-1))
        for name,(domain,values) in colors.items():
            indices=corner_indices if domain=='CORNER' else triangles[selected].reshape(-1)
            layer=derived.color_attributes.new(name=name,type='FLOAT_COLOR',domain='CORNER')
            layer.data.foreach_set('color',joined(values[indices],extra_colors[name],4).reshape(-1))
        if len(face_array):
            derived.normals_split_custom_set(joined(normals[corner_indices],extra_normals,3))
        derived['wz09_core_derived_preview']=True
        derived['wz09_clip_crossing_triangles']=len(crossing)
        return derived
    except Exception:
        bpy.data.meshes.remove(derived)
        raise
