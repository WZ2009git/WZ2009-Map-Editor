"""Lossless static preview instancing; original placements remain editable.

Only leaf preview meshes with identical mesh/material/visibility state are
batched. Authored assets, animations, modifiers, constraints and sheared
transforms stay as ordinary objects. No decimation or geometry regeneration.
"""
import collections
import json
import uuid

GROUP = 'WZ09 shared static preview instances v1'
RECORDS = 'wz09_compact_records_json'
LOD = 'WZ09 viewport-only prop detail'


def viewport_lod(scene, enabled=True, ratio=.2):
    """Reversible prototype modifiers; original meshes and renders stay full detail."""
    import json
    import bpy
    count=0;faces=0
    for obj in scene.objects:
        if not obj.get('wz09_compact_count'):
            continue
        template=bpy.data.objects.get(obj.get('wz09_compact_template'))
        if template is None:
            continue
        modifier=template.modifiers.get(LOD)
        polygons=len(template.data.polygons)
        weight=polygons*obj['wz09_compact_count']
        if modifier is None and enabled and polygons>=128 and weight>=5000:
            modifier=template.modifiers.new(LOD,'DECIMATE')
            _enum(modifier,'decimate_type','COLLAPSE')
            modifier.ratio=ratio
            modifier.use_collapse_triangulate=True
        if modifier is not None:
            modifier.show_viewport=enabled
            modifier.show_render=False
            count+=1;faces+=weight
    result=dict(enabled=enabled,prototype_modifiers=count,original_instanced_faces_affected=faces,
                requested_ratio=ratio,original_meshes_modified=False,render_decimation=False)
    scene['wz09_viewport_lod_enabled']=enabled
    scene['wz09_viewport_lod_statistics_json']=json.dumps(result)
    return result


def expanded_core(points, margin=34.0):
    """Offset a convex native core by the runtime's render/collision band."""
    import math
    area = sum(a[0]*b[1]-b[0]*a[1] for a, b in zip(points, points[1:]+points[:1]))
    if not math.isfinite(area) or abs(area) < 1e-8 or margin < 0:
        raise ValueError('Invalid convex core/margin')
    sign = 1 if area > 0 else -1
    lines = []
    for a, b in zip(points, points[1:]+points[:1]):
        dx, dy = b[0]-a[0], b[1]-a[1]
        length = math.hypot(dx, dy)
        if length < 1e-8:
            raise ValueError('Degenerate core edge')
        nx, ny = sign*dy/length, -sign*dx/length
        lines.append((nx, ny, nx*a[0]+ny*a[1]+margin))
    result = []
    for a, b in zip(lines[-1:]+lines[:-1], lines):
        det = a[0]*b[1]-b[0]*a[1]
        if abs(det) < 1e-8:
            raise ValueError('Parallel adjacent core edges')
        result.append(((a[2]*b[1]-b[2]*a[1])/det, (a[0]*b[2]-b[0]*a[2])/det))
    if any(nx*x+ny*y > d+1e-5 for x, y in result for nx, ny, d in lines):
        raise ValueError('Nonconvex core')
    return result


def _enum(owner, field, value):
    allowed = {x.identifier for x in owner.bl_rna.properties[field].enum_items}
    if value not in allowed:
        raise ValueError(f'Unsupported {field}: {value}')
    setattr(owner, field, value)


def _plain(value):
    if hasattr(value, 'to_dict'):
        return {k: _plain(v) for k, v in value.to_dict().items()}
    if hasattr(value, 'to_list'):
        return value.to_list()
    if isinstance(value, dict):
        return {k: _plain(v) for k, v in value.items()}
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return [_plain(v) for v in value]


def _node_group(bpy):
    group = bpy.data.node_groups.get(GROUP)
    if group:
        return group
    group = bpy.data.node_groups.new(GROUP, 'GeometryNodeTree')
    group.interface.new_socket(name='Geometry', in_out='OUTPUT', socket_type='NodeSocketGeometry')
    group.interface.new_socket(name='Geometry', in_out='INPUT', socket_type='NodeSocketGeometry')
    group.interface.new_socket(name='Source', in_out='INPUT', socket_type='NodeSocketObject')
    nodes, links = group.nodes, group.links
    source = nodes.new('NodeGroupInput')
    output = nodes.new('NodeGroupOutput')
    info = nodes.new('GeometryNodeObjectInfo')
    _enum(info, 'transform_space', 'ORIGINAL')
    info.inputs['As Instance'].default_value = True
    inst = nodes.new('GeometryNodeInstanceOnPoints')
    links.new(source.outputs['Geometry'], inst.inputs['Points'])
    links.new(source.outputs['Source'], info.inputs['Object'])
    links.new(info.outputs['Geometry'], inst.inputs['Instance'])
    for name, socket in [('wz_rotation', 'Rotation'), ('wz_scale', 'Scale')]:
        attr = nodes.new('GeometryNodeInputNamedAttribute')
        _enum(attr, 'data_type', 'FLOAT_VECTOR')
        attr.inputs['Name'].default_value = name
        links.new(attr.outputs['Attribute'], inst.inputs[socket])
    links.new(inst.outputs['Instances'], output.inputs['Geometry'])
    return group


def compact(scene, minimum=4, progress=None):
    import bpy
    from mathutils import Matrix
    children = collections.Counter(o.parent.as_pointer() for o in scene.objects if o.parent)
    memberships = collections.defaultdict(list)
    for coll in [scene.collection] + list(scene.collection.children_recursive):
        for item in coll.objects:
            memberships[item.as_pointer()].append(coll)
    owners, cache, groups = {}, {}, collections.defaultdict(list)

    def owner(obj):
        chain = []
        while obj:
            key = obj.as_pointer()
            if key in cache:
                result = cache[key]
                break
            chain.append(key)
            if obj.get('wz09_type') == 'map_instance':
                result = obj
                break
            obj = obj.parent
        else:
            result = None
        for key in chain:
            cache[key] = result
        return result

    for obj in scene.objects:
        if (obj.type != 'MESH' or not obj.get('wz09_preview_only')
                or obj.get('wz09_asset_placement_id') or obj.get(RECORDS)
                or obj.modifiers or obj.constraints or obj.animation_data
                or children[obj.as_pointer()] or obj.display_type != 'TEXTURED'
                or getattr(obj, 'wz09_core_original_mesh', None)):
            continue
        root = owner(obj)
        if root is None:
            continue
        owners[root.as_pointer()] = root
        materials = tuple((s.material.as_pointer() if s.material else 0, s.link) for s in obj.material_slots)
        key = (root.as_pointer(), obj.data.as_pointer(), materials, tuple(obj.color),
               obj.hide_viewport, obj.hide_select,
               tuple(c.name for c in memberships[obj.as_pointer()]), obj.hide_get(), obj.hide_render)
        groups[key].append(obj)
    source_coll = bpy.data.collections.new('WZ09 compact source meshes')
    scene.collection.children.link(source_coll)
    source_coll.hide_render = True
    group = _node_group(bpy)
    source_socket = next(s.identifier for s in group.interface.items_tree
                         if s.item_type == 'SOCKET' and s.in_out == 'INPUT' and s.name == 'Source')
    removed, made, refused = [], 0, 0
    max_error = 0.0
    for key, objects in groups.items():
        if len(objects) < minimum:
            continue
        root = owners[key[0]]
        inverse = root.matrix_world.inverted()
        records, coords, rotations, scales = [], [], [], []
        for obj in objects:
            matrix = inverse @ obj.matrix_world
            loc, rot, scale = matrix.decompose()
            rebuilt = Matrix.LocRotScale(loc, rot, scale)
            error = max(abs(matrix[i][j] - rebuilt[i][j]) for i in range(4) for j in range(4))
            max_error = max(max_error, error)
            if error > 0.0001:
                break
            records.append(dict(name=obj.name, parent=obj.parent.name if obj.parent else None,
                                matrix=[list(row) for row in matrix],
                                properties={k: _plain(v) for k, v in obj.items()},
                                collections=[c.name for c in memberships[obj.as_pointer()]],
                                global_hidden=obj.hide_viewport,
                                hide_viewport=obj.hide_get(), hide_render=obj.hide_render,
                                hide_select=obj.hide_select, color=list(obj.color)))
            coords.append(tuple(loc)); rotations.extend(rot.to_euler('XYZ')); scales.extend(scale)
        if len(records) != len(objects):
            refused += len(objects)
            continue
        template = objects[0].copy()
        template.name = 'SOURCE · ' + objects[0].data.name
        template.parent = None
        template.matrix_world = Matrix.Identity(4)
        for k in list(template.keys()):
            del template[k]
        template['wz09_editor_overlay'] = True
        source_coll.objects.link(template)
        template.hide_set(True)
        template.hide_render = True
        mesh = bpy.data.meshes.new('Placements · ' + template.data.name)
        mesh.from_pydata(coords, [], [])
        for name, values in [('wz_rotation', rotations), ('wz_scale', scales)]:
            attribute = mesh.attributes.new(name, 'FLOAT_VECTOR', 'POINT')
            attribute.data.foreach_set('vector', values)
        obj = bpy.data.objects.new('Instances · ' + objects[0].name.split('#')[0], mesh)
        for coll in memberships[objects[0].as_pointer()]:
            coll.objects.link(obj)
        obj.parent = root
        obj.color = objects[0].color
        obj.hide_viewport = objects[0].hide_viewport
        obj.hide_select = objects[0].hide_select
        modifier = obj.modifiers.new('Shared original geometry', 'NODES')
        modifier.node_group = group
        # Blender 5.2 moved modifier inputs into NodesModifier.properties.
        if hasattr(modifier, 'properties'):
            getattr(modifier.properties.inputs, source_socket).value = template
        else:
            modifier[source_socket] = template
        obj[RECORDS] = json.dumps(records, ensure_ascii=False, separators=(',', ':'))
        obj['wz09_compact_template'] = template.name
        obj['wz09_preview_only'] = True
        obj['wz09_preview_id'] = str(uuid.uuid4())
        obj['wz09_compact_count'] = len(records)
        obj.hide_set(key[-2]); obj.hide_render = key[-1]
        for original in objects:
            ancestor=original.parent
            while ancestor is not None and ancestor!=root:
                metadata=ancestor.get('iw4l')
                if hasattr(metadata,'get') and metadata.get('model'):
                    ancestor['wz09_compacted_source_parts']=True
                ancestor=ancestor.parent
        removed.extend(objects); made += 1
        if progress and made % 100 == 0:
            progress(made, len(removed))
    bpy.data.batch_remove(ids=removed)
    return dict(source_objects=len(removed), instance_groups=made,
                reduction=len(removed)-2*made, refused_objects=refused,
                maximum_trs_error=max_error)


def restore(obj):
    import bpy
    from mathutils import Matrix
    records = json.loads(obj[RECORDS])
    # Library append may rename prototypes. Resolve the real GN ID reference,
    # never an old display name stored by an earlier file.
    template = None
    for modifier in obj.modifiers:
        if modifier.type != 'NODES' or not modifier.node_group: continue
        for socket in modifier.node_group.interface.items_tree:
            if socket.item_type != 'SOCKET' or socket.in_out != 'INPUT' or socket.name != 'Source': continue
            template = (getattr(modifier.properties.inputs, socket.identifier).value
                        if hasattr(modifier, 'properties') else modifier.get(socket.identifier))
    if template is None: raise ValueError('Original asset prototype missing')
    collections = list(obj.users_collection)
    objects = []
    from .city_editing import map_root
    owner = map_root(obj)
    parents_by_id={o.get('wz09_library_object_id'):o for o in owner.children_recursive if o.get('wz09_library_object_id')}
    for row in records:
        made = bpy.data.objects.new(row['name'], template.data)
        targets=([bpy.data.collections.get(name) or next((c for c in collections if c.name==name),None)
                  for name in row['collections']] if row.get('collections') else collections)
        if any(c.get('wz09_overview_source_map_id') for c in collections):targets=collections
        for coll in [c for c in targets if c is not None] or collections:
            coll.objects.link(made)
        # Object material overrides are retained by copying the template.
        for index, slot in enumerate(template.material_slots):
            made.material_slots[index].link = slot.link
            made.material_slots[index].material = slot.material
        # After library reload old names need not exist. Map-local matrices
        # retain the correct assembly regardless of display names.
        parent_id = row.get('parent_id')
        parent = parents_by_id.get(parent_id)
        if parent is None and not parent_id:
            legacy = bpy.data.objects.get(row.get('parent', ''))
            if legacy is not None:
                try:
                    if map_root(legacy) == owner: parent = legacy
                except ValueError: pass
        made.parent = parent or owner
        made.matrix_world = obj.matrix_world @ Matrix(row['matrix'])
        for k, v in row['properties'].items():
            made[k] = v
        made.hide_render = row['hide_render']; made.hide_select = row['hide_select']
        made.color = row['color']; made.hide_set(row['hide_viewport'])
        made.hide_viewport = row.get('global_hidden', False)
        objects.append(made)
    bpy.data.objects.remove(obj, do_unlink=True)
    return objects


def restore_model(root):
    """Restore only one original model's remaining parts across shared batches."""
    import bpy
    from array import array
    parents = {o.name for o in [root] + list(root.children_recursive)}
    restored = []
    for group in list(bpy.context.scene.objects):
        if not group.get(RECORDS):
            continue
        rows = json.loads(group[RECORDS])
        selected = [i for i, row in enumerate(rows) if row.get('parent') in parents]
        if not selected:
            continue
        chosen = set(selected)
        kept = [i for i in range(len(rows)) if i not in chosen]
        # restore() reads recorded local matrices, not point coordinates.
        temporary = group.copy()
        temporary[RECORDS] = json.dumps([rows[i] for i in selected])
        for collection in group.users_collection:
            collection.objects.link(temporary)
        restored.extend(restore(temporary))
        if not kept:
            bpy.data.objects.remove(group, do_unlink=True)
            continue
        mesh = bpy.data.meshes.new('Remaining compact placements')
        mesh.from_pydata([group.data.vertices[i].co[:] for i in kept], [], [])
        for name in ('wz_rotation', 'wz_scale'):
            source = group.data.attributes[name]
            target = mesh.attributes.new(name, 'FLOAT_VECTOR', 'POINT')
            target.data.foreach_set('vector', array('f', (v for i in kept for v in source.data[i].vector)))
        group.data = mesh
        group[RECORDS] = json.dumps([rows[i] for i in kept], separators=(',', ':'))
        group['wz09_compact_count'] = len(kept)
    # All records belonging to this assembly have now been restored. Preserve
    # compact warnings on unrelated native model roots and source libraries.
    for obj in [root] + list(root.children_recursive):
        if obj.get('wz09_compacted_source_parts'):
            del obj['wz09_compacted_source_parts']
    return restored


def register():
    import bpy
    class WZ09_OT_restore_compact(bpy.types.Operator):
        bl_idname = 'wz09.restore_compact_objects'
        bl_label = 'Edit individual objects'
        bl_description = 'Restore the selected instance group as individual original objects'
        bl_options = {'REGISTER', 'UNDO'}
        @classmethod
        def poll(cls, context):
            return context.object is not None and RECORDS in context.object
        def execute(self, context):
            made = restore(context.object)
            context.view_layer.objects.active = made[0] if made else None
            self.report({'INFO'}, f'Restored {len(made)} original objects')
            return {'FINISHED'}
    global _class
    _class = WZ09_OT_restore_compact
    bpy.utils.register_class(_class)


def unregister():
    import bpy
    bpy.utils.unregister_class(_class)
