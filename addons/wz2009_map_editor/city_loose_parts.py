"""One-click loose components, recognizing coincident vertices across UV seams.

Connectivity uses rounded positions only for grouping. Actual vertices, loops,
UVs, corner colours and split normals are copied unchanged, never welded.
"""
import collections
import json
import uuid


def face_components(mesh):
    parents = list(range(len(mesh.vertices)))
    positions = {}
    def find(index):
        while parents[index] != index:
            parents[index] = parents[parents[index]]
            index = parents[index]
        return index
    for vertex in mesh.vertices:
        key = tuple(round(float(v), 5) for v in vertex.co)
        previous = positions.get(key)
        if previous is None:
            positions[key] = vertex.index
        else:
            parents[find(vertex.index)] = find(previous)
    for face in mesh.polygons:
        first = find(face.vertices[0])
        for vertex in face.vertices[1:]:
            other = find(vertex)
            if first != other:
                parents[other] = first
    result = collections.defaultdict(list)
    for face in mesh.polygons:
        result[find(face.vertices[0])].append(face.index)
    return sorted(result.values(), key=len, reverse=True)


def _capture(mesh):
    import numpy as np
    def read(items, prop, width, dtype):
        values = np.empty((len(items), width), dtype=dtype)
        items.foreach_get(prop, values.ravel())
        return values
    result = dict(co=read(mesh.vertices,'co',3,np.float32),
                  vertices=read(mesh.loops,'vertex_index',1,np.int32).ravel(),
                  starts=read(mesh.polygons,'loop_start',1,np.int32).ravel(),
                  totals=read(mesh.polygons,'loop_total',1,np.int32).ravel(),
                  smooth=read(mesh.polygons,'use_smooth',1,np.bool_).ravel(),
                  normals=read(mesh.corner_normals,'vector',3,np.float32), attributes=[])
    formats = {'FLOAT':('value',1,np.float32), 'INT':('value',1,np.int32),
               'BOOLEAN':('value',1,np.bool_), 'FLOAT_VECTOR':('vector',3,np.float32),
               'FLOAT2':('vector',2,np.float32), 'FLOAT_COLOR':('color',4,np.float32),
               'BYTE_COLOR':('color',4,np.float32)}
    for attribute in mesh.attributes:
        if attribute.name.startswith('.') or attribute.name in {'position','custom_normal'}:
            continue
        if attribute.domain not in {'POINT','FACE','CORNER','EDGE'} or attribute.data_type not in formats:
            raise ValueError('Unsupported mesh attribute: ' + attribute.name)
        prop,width,dtype = formats[attribute.data_type]
        result['attributes'].append((attribute.name,attribute.domain,attribute.data_type,prop,
                                     read(attribute.data,prop,width,dtype)))
    if any(row[1]=='EDGE' for row in result['attributes']):
        result['edge_indices']={tuple(sorted(edge.vertices)):edge.index for edge in mesh.edges}
    return result


def _piece(source, captured, faces):
    import bpy
    import numpy as np
    ids = np.asarray(faces,dtype=np.int32)
    counts = captured['totals'][ids]
    # One contiguous allocation instead of one Python/NumPy object per face.
    # Keep face order and each face's corner order (UVs and normals use it).
    offsets = np.cumsum(counts, dtype=np.int64) - counts
    loops = np.arange(int(counts.sum()), dtype=np.int64)
    loops += np.repeat(captured['starts'][ids].astype(np.int64) - offsets, counts)
    vertices, mapped = np.unique(captured['vertices'][loops],return_inverse=True)
    coords = captured['co'][vertices]
    center = (coords.min(axis=0)+coords.max(axis=0))/2
    mesh = bpy.data.meshes.new('Stadtteil')
    try:
        mesh.vertices.add(len(vertices));mesh.vertices.foreach_set('co',(coords-center).ravel())
        mesh.loops.add(len(loops));mesh.loops.foreach_set('vertex_index',mapped.astype(np.int32))
        mesh.polygons.add(len(ids))
        mesh.polygons.foreach_set('loop_start',np.cumsum(np.r_[0,counts[:-1]]).astype(np.int32))
        mesh.polygons.foreach_set('loop_total',counts)
        mesh.polygons.foreach_set('use_smooth',captured['smooth'][ids])
        material_values=next((row[4].ravel()[ids] for row in captured['attributes'] if row[0]=='material_index'),np.zeros(len(ids),dtype=np.int32))
        used,local_materials=np.unique(material_values,return_inverse=True)
        if source.materials:
            for index in used:mesh.materials.append(source.materials[int(index)])
        mesh.update(calc_edges=True)
        for name,domain,kind,prop,values in captured['attributes']:
            if name=='material_index':
                mesh.polygons.foreach_set('material_index',local_materials.astype(np.int32))
                continue
            if domain=='EDGE':
                indices=np.asarray([captured['edge_indices'][tuple(sorted((int(vertices[e.vertices[0]]),int(vertices[e.vertices[1]]))))]
                                    for e in mesh.edges],dtype=np.int32)
            else:
                indices = {'POINT':vertices,'FACE':ids,'CORNER':loops}[domain]
            if kind == 'FLOAT2' and domain == 'CORNER' and source.uv_layers.get(name):
                layer = mesh.uv_layers.new(name=name)
                layer.data.foreach_set('uv',values[indices].ravel())
            else:
                target = mesh.attributes.get(name) or mesh.attributes.new(name,kind,domain)
                target.data.foreach_set(prop,values[indices].ravel())
        # Preserve native corner shading across UV and normal seams.
        mesh.normals_split_custom_set(captured['normals'][loops])
        mesh.update()
        return mesh, [float(v) for v in center]
    except Exception:
        bpy.data.meshes.remove(mesh)
        raise


def split_city_steps(ctx):
    import bpy
    from array import array
    from . import city_editing as city, overview_preview
    if ctx.mode != 'OBJECT':
        raise ValueError('Press Tab to return to Object Mode first')
    selected = ctx.active_object
    root = city.map_root(selected)
    if selected.get(city.PART):
        raise ValueError('This object is already an individual part')
    candidates = [o for o in root.children_recursive if o.type == 'MESH'
                  and o.get('wz09_preview_only') and not o.get('wz09_editor_overlay')
                  and not o.get(city.PART) and not o.modifiers]
    source = selected if selected in candidates else max(candidates,key=lambda o:len(o.data.polygons),default=None)
    if source is None or not source.data.polygons:
        raise ValueError('No unsplit map geometry selected')
    groups = face_components(source.data)
    if len(groups) <= 1:
        raise ValueError('Geometry is already a single connected component')
    binding = city.source_binding(root)
    records = city.registry(ctx.scene)
    uid = source.get(city.SOURCE)
    working = source.data
    baseline = None
    if uid:
        entry = records['meshes'][uid]
        if city.mesh_digest(working) != entry['remaining_geometry_sha256']:
            raise ValueError('Untracked mesh edits: split cancelled')
        city._partition(source,entry,records)
    else:
        uid = str(uuid.uuid4())
        working = source.data.copy()
        attribute = working.attributes.get(city.FACE) or working.attributes.new(city.FACE,'INT','FACE')
        attribute.data.foreach_set('value',array('i',range(len(working.polygons))))
        baseline = working.copy();baseline.use_fake_user=True
        baseline['wz09_city_baseline_id']=uid
        entry = dict(id=uid,owner_map=str(root['wz09_id']),source=binding,
                     preview_sha256=str(root.get('wz09_preview_sha256','')),
                     baseline_geometry_sha256=city.mesh_digest(baseline),
                     native_surface_mapping='missing',baseline_face_count=len(baseline.polygons))
    made = []
    empty = None
    parts_collection = None
    original_data=source.data
    try:
        captured = _capture(working)
        # Read Blender RNA once, rather than crossing it once per source face.
        source_face_ids = array('i', [0]) * len(working.polygons)
        working.attributes[city.FACE].data.foreach_get('value', source_face_ids)
        for number, faces in enumerate(groups,1):
            mesh, center = _piece(working,captured,faces)
            obj = bpy.data.objects.new(f'Stadtteil {number:04d}',mesh)
            made.append(obj)
            obj.parent=source;obj.location=center
            identity=str(uuid.uuid4())
            obj[city.PART]=identity;obj['wz09_preview_only']=True
            obj['wz09_city_part_source']=uid
            records['parts'][identity]=dict(id=identity,source_mesh_id=uid,
                source_face_ids=[source_face_ids[i] for i in faces],
                source_origin_m=center,geometry_sha256=city.mesh_digest(mesh))
            yield (number,len(groups))
        # Link beneath a disabled collection. Per-object hide_set calls force
        # expensive ViewLayer resyncs as the city grows into thousands of parts.
        parts_collection = bpy.data.collections.new('Connected parts - ' + str(root['wz09_source_map']))
        parts_collection.hide_viewport = True
        for collection in source.users_collection:
            collection.children.link(parts_collection)
        for number,obj in enumerate(made,1):
            parts_collection.objects.link(obj)
            if number%32==0:yield (len(groups),len(groups))
        empty=bpy.data.meshes.new('Map source - original retained')
        empty.attributes.new(city.FACE,'INT','FACE')
        entry['remaining_geometry_sha256']=city.mesh_digest(empty)
        records['meshes'][uid]=entry
        # No source geometry changes occur until all components have succeeded.
        source.data=empty;source[city.SOURCE]=uid
        city._write(ctx.scene,records)
    except BaseException:
        source.data=original_data
        if uid not in city.registry(ctx.scene)['meshes'] and city.SOURCE in source:
            del source[city.SOURCE]
        for obj in made:
            mesh=obj.data;bpy.data.objects.remove(obj,do_unlink=True);bpy.data.meshes.remove(mesh)
        if parts_collection is not None:bpy.data.collections.remove(parts_collection)
        if empty is not None and not empty.users:bpy.data.meshes.remove(empty)
        if baseline is not None:bpy.data.meshes.remove(baseline)
        raise
    finally:
        if baseline is not None and working.users==0:bpy.data.meshes.remove(working)
    ctx.scene['wz09_city_overview_stale']=True
    states=json.loads(ctx.scene.get('wz09_city_view_states_json','{}'))
    states[str(root['wz09_id'])]='loaded';ctx.scene['wz09_city_view_states_json']=json.dumps(states)
    overview_preview.set_focus(ctx.scene,str(root['wz09_id']))
    parts_collection.hide_viewport = False
    bpy.ops.object.select_all(action='DESELECT')
    # Select only the largest piece, avoiding thousands of orange outlines.
    made[0].select_set(True);ctx.view_layer.objects.active=made[0]
    return len(made)


def split_city(ctx):
    steps=split_city_steps(ctx)
    while True:
        try:next(steps)
        except StopIteration as finished:return finished.value
