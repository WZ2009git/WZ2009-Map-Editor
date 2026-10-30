"""Repair visual-preview winding on scene-local copies; never edit source meshes."""
import math
import uuid


def _key(value):
    return value.as_pointer() if hasattr(value, 'as_pointer') else id(value)


def classify_triangle_winding(vertices, normals, epsilon=1e-6):
    """Compare geometric winding with averaged authored corner normals.

    Return aligned/reversed/degenerate/ambiguous.  The dot-product tolerance is
    dimensionless; tiny triangles are tested relative to their edge lengths.
    """
    if not math.isfinite(epsilon) or not 0 < epsilon < 1:
        raise ValueError('Winding epsilon must be finite and between zero and one')
    if len(vertices) != 3 or len(normals) != 3 or any(
            len(vector) != 3 or any(not math.isfinite(value) for value in vector)
            for vector in list(vertices) + list(normals)):
        raise ValueError('Winding requires three finite positions and corner normals')
    a, b, c = vertices
    u, v = [[point[i]-a[i] for i in range(3)] for point in (b, c)]
    cross = [u[1]*v[2]-u[2]*v[1], u[2]*v[0]-u[0]*v[2], u[0]*v[1]-u[1]*v[0]]
    length = lambda values: math.sqrt(sum(value*value for value in values))
    cross_length, edge_product = length(cross), length(u)*length(v)
    if edge_product == 0 or cross_length <= epsilon*edge_product:
        return 'degenerate'
    normal = [sum(value[i] for value in normals)/3 for i in range(3)]
    normal_length = length(normal)
    if normal_length <= 1e-12:
        return 'ambiguous'
    cosine = sum(cross[i]*normal[i] for i in range(3))/(cross_length*normal_length)
    return 'reversed' if cosine < -epsilon else 'aligned' if cosine > epsilon else 'ambiguous'


def _classify_mesh(mesh):
    mesh.calc_loop_triangles()
    states = {}
    totals = {name: 0 for name in ('aligned', 'reversed', 'degenerate', 'ambiguous')}
    corner_normals = mesh.corner_normals
    for triangle in mesh.loop_triangles:
        state = classify_triangle_winding(
            [tuple(mesh.vertices[index].co) for index in triangle.vertices],
            [tuple(corner_normals[index].vector) for index in triangle.loops])
        totals[state] += 1
        states.setdefault(triangle.polygon_index, set()).add(state)
    # A whole polygon can only be reversed when every nondegenerate constituent
    # triangle agrees.  Mixed/unknown authored normals are left untouched.
    faces = [index for index, values in states.items()
             if 'reversed' in values and not values & {'aligned', 'ambiguous'}]
    return faces, totals


def _corner_normal_map(mesh):
    """Snapshot object-space normals before BMesh changes their encoded basis."""
    if not all(hasattr(mesh, name) for name in ('polygons', 'loops', 'normals_split_custom_set')):
        return None  # Minimal offline fakes; real Blender Mesh exposes these.
    result = {}
    for polygon in mesh.polygons:
        for index in polygon.loop_indices:
            key = (polygon.index, mesh.loops[index].vertex_index)
            normal = tuple(mesh.corner_normals[index].vector)
            if key in result and result[key] != normal:
                raise ValueError('Preview polygon repeats a vertex with conflicting corner normals')
            result[key] = normal
    return result


def _restore_corner_normals(mesh, snapshot):
    if snapshot is None:
        return
    normals = [None]*len(mesh.loops)
    for polygon in mesh.polygons:
        for index in polygon.loop_indices:
            key = (polygon.index, mesh.loops[index].vertex_index)
            if key not in snapshot:
                raise ValueError('Preview repair changed polygon/vertex corner identity')
            normals[index] = snapshot[key]
    if any(normal is None for normal in normals):
        raise ValueError('Preview repair left an unmatched mesh corner')
    mesh.normals_split_custom_set(normals)
    mesh.update()


def copy_repaired_mesh(original, faces, *, bpy_module=None, bmesh_module=None):
    """Existing copy/BMesh repair core, without objects or scene enumeration.

    The caller assigns the returned private mesh only after its own audits.
    A failure removes only the copy made by this call.
    """
    if bpy_module is None:
        import bpy as bpy_module
    if bmesh_module is None:
        import bmesh as bmesh_module
    corner_normals = _corner_normal_map(original)
    repaired = original.copy()
    try:
        repaired['wz09_preview_geometry_id'] = str(uuid.uuid4())
        repaired['wz09_preview_repair'] = 'authored_corner_normal_winding_v1'
        repaired['wz09_preview_winding_reversed_faces'] = len(faces)
        repaired['wz09_preview_winding_source_name'] = str(getattr(original, 'name_full', ''))
        bm = bmesh_module.new()
        try:
            bm.from_mesh(repaired)
            bm.faces.ensure_lookup_table()
            bmesh_module.ops.reverse_faces(bm, faces=[bm.faces[index] for index in faces])
            bm.normal_update()
            bm.to_mesh(repaired)
            repaired.update()
            _restore_corner_normals(repaired, corner_normals)
        finally:
            bm.free()
        return repaired
    except Exception:
        bpy_module.data.meshes.remove(repaired, do_unlink=True)
        raise


def repair_preview_winding(scene, *, objects=None, bpy_module=None, bmesh_module=None):
    """Copy and repair marked preview meshes, with atomic assignment rollback.

    Shared source datablocks stay untouched.  All marked preview objects in this
    scene sharing a mesh use one repaired copy.  An object linked into another
    scene is rejected before changes because its data assignment would be global.
    Geometry classification alone imports neither bpy nor bmesh.
    """
    scene_keys = {_key(obj) for obj in scene.objects}
    candidates = list(scene.objects) if objects is None else list(objects)
    if any(_key(obj) not in scene_keys for obj in candidates):
        raise ValueError('Preview repair subset must belong to this scene')
    objects = [obj for obj in candidates
               if getattr(obj, 'type', '') == 'MESH' and obj.get('wz09_preview_only')]
    # Blender Object.users_scene scans every scene's complete object list. Doing
    # that for every imported mesh makes successive large imports quadratic.
    # Scan foreign scenes once, restricted to this operation's candidate IDs.
    api=bpy_module
    if api is None:
        try:import bpy as api
        except ImportError:api=None
    scenes=getattr(getattr(api,'data',None),'scenes',None)
    if scenes is not None:
        candidate_keys={_key(obj) for obj in objects}
        for owner in scenes:
            if _key(owner)==_key(scene):continue
            if any(_key(obj) in candidate_keys for obj in owner.objects):
                raise ValueError('Preview object belongs to another scene; create a scene-local object first')
    groups = {}
    for obj in objects:
        if getattr(obj, 'library', None) is not None or getattr(obj, 'override_library', None) is not None:
            raise ValueError('Linked or overridden preview objects cannot be repaired here')
        # The fallback supports small offline fakes without a complete bpy data
        # scene registry. Real Blender always uses the indexed path above.
        if scenes is None and any(_key(owner) != _key(scene) for owner in getattr(obj, 'users_scene', (scene,))):
            raise ValueError('Preview object belongs to another scene; create a scene-local object first')
        mesh = getattr(obj, 'data', None)
        if mesh is not None:
            group = groups.setdefault(_key(mesh), {'mesh': mesh, 'objects': []})
            group['objects'].append(obj)
    summary = {'meshes_inspected': len(groups), 'meshes_copied': 0, 'objects_reassigned': 0,
               'faces_reversed': 0, 'triangles': {name: 0 for name in ('aligned', 'reversed', 'degenerate', 'ambiguous')},
               'meshes': [], 'source_meshes_unchanged': True}
    plans = []
    for group in groups.values():
        faces, totals = _classify_mesh(group['mesh'])
        for name, count in totals.items():
            summary['triangles'][name] += count
        if faces:
            plans.append((group, faces))
    if not plans:
        return summary
    if bpy_module is None:
        import bpy as bpy_module
    if bmesh_module is None:
        import bmesh as bmesh_module
    created, assignments = [], []
    try:
        for group, faces in plans:
            original = group['mesh']
            repaired = copy_repaired_mesh(original, faces, bpy_module=bpy_module, bmesh_module=bmesh_module)
            created.append(repaired)
            identity = repaired['wz09_preview_geometry_id']
            for obj in group['objects']:
                assignments.append((obj, original))
                obj.data = repaired
            summary['meshes_copied'] += 1
            summary['objects_reassigned'] += len(group['objects'])
            summary['faces_reversed'] += len(faces)
            summary['meshes'].append({'id': identity, 'source_mesh_name': str(getattr(original, 'name_full', '')),
                                      'source_faces': len(getattr(original, 'polygons', ())),
                                      'faces_reversed': len(faces), 'objects_reassigned': len(group['objects'])})
    except Exception:
        for obj, original in reversed(assignments):
            obj.data = original
        for mesh in reversed(created):
            bpy_module.data.meshes.remove(mesh, do_unlink=True)
        raise
    return summary
