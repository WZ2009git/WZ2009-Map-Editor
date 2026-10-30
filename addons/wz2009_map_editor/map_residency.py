"""Versioned per-map libraries and actual Blender datablock eviction.

Composition owns placement; libraries own local geometry. All writes create a
new editor-local file. No game exports, original files or source maps are edited.
"""
import hashlib
import json
from pathlib import Path
import uuid

LIBRARY = 'wz09_detail_library_json'
OUTER = 'wz09_outskirts_library_json'
OWNER = 'wz09_resident_map_id'
LAYER = 'wz09_resident_layer'
TOKEN = 'wz09_residency_token'
FORMAT = 'wz09-map-library-v1'
_CLASSES = []
_HANDLE = None
_PANEL_POLLS = []


def project_root():
    return Path(__file__).resolve().parents[2]


def sha(path):
    result = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(1048576), b''):
            result.update(block)
    return result.hexdigest()


def safe_path(path):
    path = Path(path).resolve()
    if not path.is_relative_to(project_root().resolve()) or path.suffix != '.blend':
        raise ValueError('Detail file must remain inside the editor project')
    return path


def all_ids():
    import bpy
    # Include dependency IDs such as image pixels and GN prototypes, not only
    # visible objects. Never call a global orphan purge in the user's session.
    for name in ('objects', 'collections', 'meshes', 'curves', 'materials', 'images',
                 'node_groups', 'actions', 'textures', 'armatures', 'cameras', 'lights'):
        yield from getattr(bpy.data, name)


def _local_registry(scene, identity):
    from . import city_editing as city
    records = city.registry(scene)
    meshes = {k: v for k, v in records['meshes'].items() if v['owner_map'] == identity}
    return dict(meshes=meshes, parts={k: v for k, v in records['parts'].items()
                if v['source_mesh_id'] in meshes},
                models={k: v for k, v in records['models'].items() if v['owner_map'] == identity})


def geometry_objects(root):
    """Native meshes, edited parts and assembly parents, excluding helpers."""
    chosen = set()
    for obj in root.children_recursive:
        if obj.type != 'MESH' or obj.get('wz09_editor_overlay') or obj.get('wz09_type'):
            continue
        if not (obj.get('wz09_preview_only') or obj.get('wz09_city_edit_part_id')):
            continue
        node = obj
        while node is not None and node != root:
            chosen.add(node)
            node = node.parent
    return chosen


def write_library(scene, root, objects, directory, *, layer='core'):
    """Snapshot an assembly in map-local coordinates, retaining source IDs."""
    import bpy
    from mathutils import Matrix
    directory = Path(directory).resolve()
    directory.relative_to(project_root().resolve())
    directory.mkdir(parents=True, exist_ok=True)
    identity = str(root['wz09_id'])
    collection = bpy.data.collections.new('WZ09 library ' + identity + ' ' + layer)
    collection_name = collection.name
    collection[OWNER] = identity; collection[LAYER] = layer
    records = _local_registry(scene, identity) if layer == 'core' else dict(meshes={}, parts={}, models={})
    collection['wz09_library_registry_json'] = json.dumps(records)
    baselines = [m for m in bpy.data.meshes if m.get('wz09_city_baseline_id') in records['meshes']]
    collection['wz09_library_baselines_json'] = json.dumps([m.name for m in baselines])
    path = safe_path(directory / (identity + '-' + layer + '-' + uuid.uuid4().hex[:12] + '.blend'))
    old_matrix = root.matrix_basis.copy()
    old_inverse = root.matrix_parent_inverse.copy()
    old_parent = root.parent
    old_kind = root.get('wz09_type')
    linked = list(objects) + [root]
    for obj in linked:
        if not obj.get('wz09_library_object_id'): obj['wz09_library_object_id'] = str(uuid.uuid4())
    identities = {obj.name: obj['wz09_library_object_id'] for obj in linked}
    for obj in objects:
        if obj.get('wz09_compact_records_json'):
            rows = json.loads(obj['wz09_compact_records_json'])
            for row in rows:
                if not row.get('parent_id'): row['parent_id'] = identities.get(row.get('parent'))
            obj['wz09_compact_records_json'] = json.dumps(rows, separators=(',', ':'))
    previous_roots = {obj: obj['wz09_library_root_id'] for obj in objects if obj.get('wz09_library_root_id')}
    try:
        for obj in linked: collection.objects.link(obj)
        for obj in previous_roots: del obj['wz09_library_root_id']
        root.parent = None; root.matrix_parent_inverse = Matrix.Identity(4)
        root.matrix_basis = Matrix.Identity(4)
        # The appended local assembly root is not a second MapInstanceId.
        if 'wz09_type' in root: del root['wz09_type']
        root['wz09_library_root_id'] = identity
        bpy.data.libraries.write(str(path), {collection, *baselines}, path_remap='ABSOLUTE', fake_user=True, compress=False)
    finally:
        root.parent = old_parent; root.matrix_parent_inverse = old_inverse
        root.matrix_basis = old_matrix
        if old_kind is not None: root['wz09_type'] = old_kind
        if 'wz09_library_root_id' in root: del root['wz09_library_root_id']
        for obj, value in previous_roots.items(): obj['wz09_library_root_id'] = value
        bpy.data.collections.remove(collection)
        bpy.context.view_layer.update()
    return dict(format=FORMAT, map_id=identity, source_map=str(root['wz09_source_map']),
                layer=layer, path=str(path), sha256=sha(path), collection=collection_name,
                objects=len(linked), native_source_sha256=json.loads(root.get('wz09_calibration_json', '{}')).get('registration', {}).get('native_source_sha256'))


def resident(scene, root, layer='core'):
    identity = str(root['wz09_id'])
    return next((c for c in scene.collection.children_recursive
                 if c.get(OWNER) == identity and c.get(LAYER) == layer), None)


def load(ctx, root=None, *, layer='core'):
    import bpy
    from mathutils import Matrix
    from . import city_editing as city, overview_preview
    root = root or city.map_root(ctx.active_object)
    if ctx.mode != 'OBJECT': raise ValueError('Press Tab to finish mesh editing before loading details')
    if resident(ctx.scene, root, layer): return root
    key = LIBRARY if layer == 'core' else OUTER
    record = json.loads(root.get(key, '{}'))
    if record.get('format') != FORMAT or record.get('map_id') != str(root['wz09_id']) or record.get('source_map') != root['wz09_source_map'] or record.get('layer') != layer:
        raise ValueError('No matching verified detail/outskirts library available')
    path = safe_path(record['path'])
    if sha(path) != record['sha256']: raise ValueError('Detail file changed outside this workflow')
    # A second map is saved and evicted before loading another full assembly.
    for other in tuple(ctx.scene.objects):
        if other.get('wz09_type') == 'map_instance' and other != root and resident(ctx.scene, other):
            unload(ctx, other)
    before = {v.as_pointer() for v in all_ids()}
    token = uuid.uuid4().hex
    try:
        with bpy.data.libraries.load(str(path), link=False) as (source, target):
            if record['collection'] not in source.collections: raise ValueError('Detail-Sammlung fehlt')
            target.collections = [record['collection']]
            # Baselines may have fake users but no object dependencies.
        collection = target.collections[0]
        additions = [v for v in all_ids() if v.as_pointer() not in before]
        for value in additions: value[TOKEN] = token
        collection[TOKEN] = token
        with bpy.data.libraries.load(str(path), link=False) as (source, baseline_target):
            names = json.loads(collection.get('wz09_library_baselines_json', '[]'))
            baseline_target.meshes = names
        for value in all_ids():
            if value.as_pointer() not in before: value[TOKEN] = token
        roots = [o for o in collection.objects if o.get('wz09_library_root_id') == str(root['wz09_id'])]
        if len(roots) != 1: raise ValueError('Detail root missing or ambiguous')
        assembly = roots[0]
        for field in (LIBRARY, OUTER):
            if field in assembly: del assembly[field]
        assembly.parent = root; assembly.matrix_parent_inverse = Matrix.Identity(4)
        assembly.matrix_basis = Matrix.Identity(4)
        ctx.scene.collection.children.link(collection)
        collection.hide_viewport = False
        for obj in collection.all_objects:
            if obj.get('wz09_native_instance_prototype'): obj.hide_set(True)
        if layer == 'core':
            collection[overview_preview.SOURCE] = str(root['wz09_id'])
            records = city.registry(ctx.scene)
            incoming = json.loads(collection['wz09_library_registry_json'])
            if root.get('wz09_original_map_instance_id') and not incoming['meshes']:
                # Blender copies can carry the source map edit bookkeeping.
                # The copied visible geometry is this new instance baseline;
                # preserve the old provenance without claiming old part IDs.
                for obj in collection.all_objects:
                    fields={key:obj[key] for key in (city.SOURCE,city.PART,'wz09_city_part_source') if key in obj}
                    if fields:
                        obj['wz09_copied_edit_origin_json']=json.dumps(fields)
                        for key in fields: del obj[key]
            for kind in records:
                if set(incoming[kind]) & set(records[kind]): raise ValueError('Doppelte Bearbeitungs-IDs beim Laden')
                records[kind].update(incoming[kind])
            city._write(ctx.scene, records)
            states = json.loads(ctx.scene.get('wz09_city_view_states_json', '{}'))
            states[str(root['wz09_id'])] = 'loaded'
            ctx.scene['wz09_city_view_states_json'] = json.dumps(states)
            for proxy in ctx.scene.objects:
                if proxy.get(overview_preview.PROXY) == str(root['wz09_id']): proxy.hide_set(True)
        bpy.context.view_layer.update()
        bpy.ops.object.select_all(action='DESELECT')
        root.hide_set(False); root.select_set(True)
        ctx.view_layer.objects.active = root
        return root
    except BaseException:
        additions = [v for v in all_ids() if v.as_pointer() not in before]
        if additions: bpy.data.batch_remove(ids=additions)
        raise


def save(ctx, root=None, *, layer='core'):
    import bpy
    from . import city_editing as city
    root = root or city.map_root(ctx.active_object)
    if ctx.mode != 'OBJECT': raise ValueError('Press Tab to finish mesh editing before saving details')
    collection = resident(ctx.scene, root, layer)
    if collection is None: raise ValueError('This detail geometry is not loaded')
    assembly = next(o for o in collection.objects if o.get('wz09_library_root_id') == str(root['wz09_id']))
    key = LIBRARY if layer == 'core' else OUTER
    previous = json.loads(root[key])
    # write_library uses the real map for registry/provenance. The loaded local
    # assembly root already has identity coordinates and can be retained below it.
    objects = set(collection.all_objects)
    if layer == 'core':
        additional=geometry_objects(root)-objects
        outer=resident(ctx.scene,root,'outer')
        if outer: additional-=set(outer.all_objects)
        for obj in additional: collection.objects.link(obj)
        objects.update(additional)
    token = collection[TOKEN]
    for obj in objects:
        if obj.type == 'MESH' and not obj.data.get(TOKEN): obj.data[TOKEN] = token
    if layer == 'core':
        mesh_ids = _local_registry(ctx.scene, str(root['wz09_id']))['meshes']
        for mesh in bpy.data.meshes:
            if mesh.get('wz09_city_baseline_id') in mesh_ids: mesh[TOKEN] = token
    record = write_library(ctx.scene, root, objects, Path(previous['path']).parent, layer=layer)
    # The export includes master root plus prior assembly. Only the outermost
    # root carries the library root ID after this write.
    root[key] = json.dumps(record)
    return record


def _evict(ctx, root, layer):
    import bpy
    from . import city_editing as city, overview_preview
    collection = resident(ctx.scene, root, layer)
    if collection is None: return
    token = collection[TOKEN]
    objects = set(collection.all_objects)
    for child in collection.children_recursive: child[TOKEN] = token
    # Geometry created after append (split/extract/restore) belongs to this
    # collection too. Snapshot it before this operation.
    bpy.data.batch_remove(ids=list(objects) + [collection])
    # Remove only IDs imported by this residency, and never referenced by another
    # map/scene. Clearing fake users is necessary for meshes/images to leave RAM.
    for _ in range(8):
        removable = []
        for value in list(all_ids()):
            if value.get(TOKEN) != token: continue
            value.use_fake_user = False
            if value.users == 0: removable.append(value)
        if not removable: break
        bpy.data.batch_remove(ids=removable)
    if layer == 'core':
        outgoing = _local_registry(ctx.scene, str(root['wz09_id']))
        records = city.registry(ctx.scene)
        for kind in records:
            for identity in outgoing[kind]: records[kind].pop(identity, None)
        city._write(ctx.scene, records)
        states = json.loads(ctx.scene.get('wz09_city_view_states_json', '{}'))
        states[str(root['wz09_id'])] = 'unloaded'
        ctx.scene['wz09_city_view_states_json'] = json.dumps(states)
        for proxy in ctx.scene.objects:
            if proxy.get(overview_preview.PROXY) == str(root['wz09_id']): proxy.hide_set(False)


def lighten_public_proxy(proxy):
    """Keep overview meshes from retaining native textures after eviction."""
    import bpy
    import numpy as np
    material=bpy.data.materials.get('WZ2009 lightweight overview')
    if material is None:
        material=bpy.data.materials.new('WZ2009 lightweight overview')
        material.diffuse_color=(.58,.6,.57,1)
    proxy.data.materials.clear(); proxy.data.materials.append(material)
    proxy.data.polygons.foreach_set('material_index',np.zeros(len(proxy.data.polygons),np.int32))


def unload(ctx, root=None):
    from . import city_editing as city, city_view_cache
    root = root or city.map_root(ctx.active_object)
    # Save both layers first; any failure retains all loaded objects.
    layers = [layer for layer in ('outer', 'core') if resident(ctx.scene, root, layer)]
    for layer in layers: save(ctx, root, layer=layer)
    if 'core' in layers:
        # Preserve appearance of edits in the master preview before eviction.
        active = ctx.view_layer.objects.active
        try:
            ctx.view_layer.objects.active = root
            city_view_cache.bake(ctx)
            simplify_proxy(ctx.active_object)
            if ctx.scene.get('wz09_public_project'): lighten_public_proxy(ctx.active_object)
        finally:
            ctx.view_layer.objects.active = active if active and active.name in ctx.scene.objects else root
    for layer in layers: _evict(ctx, root, layer)
    ctx.view_layer.objects.active = root
    return root


def simplify_proxy(obj, ratio=.12):
    """Reduce only a derived overview copy; detail libraries stay lossless."""
    import bpy
    from . import overview_preview
    if not obj.get(overview_preview.PROXY) or len(obj.data.polygons)<10000: return
    modifier=obj.modifiers.new('Light overview only','DECIMATE')
    modifier.ratio=ratio; modifier.use_collapse_triangulate=True
    replacement=None
    try:
        bpy.context.view_layer.update()
        replacement=bpy.data.meshes.new_from_object(obj.evaluated_get(bpy.context.evaluated_depsgraph_get()))
    finally:
        obj.modifiers.remove(modifier)
    previous=obj.data; obj.data=replacement
    if previous.users==0: bpy.data.meshes.remove(previous)


def toggle_outer(ctx):
    from . import city_editing as city
    root = city.map_root(ctx.active_object)
    if resident(ctx.scene, root, 'outer'):
        save(ctx, root, layer='outer'); _evict(ctx, root, 'outer')
    else:
        load(ctx, root)
        load(ctx, root, layer='outer')


def _draw_labels():
    import bpy
    import blf
    from bpy_extras.view3d_utils import location_3d_to_region_2d
    from mathutils import Vector
    from . import overview_preview
    ctx = bpy.context
    if not ctx.region or not ctx.region_data or not getattr(ctx.scene, 'wz09_map_labels', False): return
    blf.size(0, round(16*ctx.preferences.system.ui_scale)); blf.color(0, .95, .88, .58, 1)
    blf.enable(0,blf.SHADOW); blf.shadow(0,3,0,0,0,.9); blf.shadow_offset(0,1,-1)
    roots=[]; proxies={}
    for obj in ctx.scene.objects:
        if obj.get('wz09_type')=='map_instance': roots.append(obj)
        if obj.get(overview_preview.PROXY): proxies[str(obj[overview_preview.PROXY])]=obj
    for obj in roots:
        proxy=proxies.get(str(obj['wz09_id']))
        anchor=obj.matrix_world.translation
        if proxy is not None and proxy.type=='MESH' and proxy.data.vertices:
            center=sum((Vector(p) for p in proxy.bound_box),Vector())/8
            anchor=proxy.matrix_world@center
        point = location_3d_to_region_2d(ctx.region, ctx.region_data, anchor)
        if point:
            label = str(obj.get('wz09_display_name', obj.get('wz09_source_map', obj.name)))
            blf.position(0, point.x-blf.dimensions(0, label)[0]/2, point.y+18, 0); blf.draw(0, label)
    blf.disable(0,blf.SHADOW)


def initialize_workshop_view(ctx):
    """Frame the composition on first user launch, preserving later saved views."""
    if not ctx.scene.get('wz09_library_workspace') or ctx.scene.get('wz09_workshop_view_initialized'): return
    from mathutils import Vector, Euler
    from . import overview_preview
    points=[]
    for obj in ctx.scene.objects:
        if obj.get(overview_preview.PROXY): points.extend(obj.matrix_world@Vector(p) for p in obj.bound_box)
    if not points: return
    lower=Vector(tuple(min(p[axis] for p in points) for axis in range(3)))
    upper=Vector(tuple(max(p[axis] for p in points) for axis in range(3)))
    center=(lower+upper)/2; distance=max((upper-lower).length,1000)
    for area in ctx.window.screen.areas:
        if area.type!='VIEW_3D': continue
        space=area.spaces.active
        space.show_region_ui=True; space.show_region_toolbar=True
        space.overlay.show_overlays=True; space.overlay.show_relationship_lines=False
        space.overlay.show_extras=False; space.shading.type='SOLID'; space.shading.color_type='MATERIAL'
        space.clip_end=max(20000,distance*3)
        space.region_3d.view_location=center; space.region_3d.view_distance=distance
        space.region_3d.view_rotation=Euler((.35,0,0)).to_quaternion()
        space.region_3d.view_perspective='ORTHO'
    ctx.scene['wz09_workshop_view_initialized']=True


def draw(layout, ctx):
    import textwrap
    from . import city_editing as city
    width=max(22,int((getattr(ctx.region,'width',320)-50)/7))
    def help_text(parent,text):
        for line in textwrap.wrap(text,width=width): parent.label(text=line)
    box = layout.box(); box.label(text='Map workshop', icon='OUTLINER_COLLECTION')
    root=None
    try:
        root = city.map_root(ctx.active_object)
        box.prop(root,'["wz09_display_name"]',text='Name')
        box.label(text=('Full details loaded' if resident(ctx.scene, root) else 'Overview only'),icon='INFO')
        box.label(text=('Outskirts loaded' if resident(ctx.scene, root,'outer') else 'Outskirts not loaded'))
    except ValueError:
        help_text(box,'Select a map in the viewport or Outliner to begin.')
    column=box.column(); column.enabled=bool(root and root.get(LIBRARY))
    load_button=column.column(); load_button.enabled=bool(root and not resident(ctx.scene,root))
    load_button.operator('wz09.city_assets_load', text='1. Load map details', icon='IMPORT')
    help_text(column,'Load one map at full detail. Other maps stay as light previews.')
    outer_button=column.column(); outer_button.enabled=bool(root and root.get(OUTER))
    outer_button.operator('wz09.map_outskirts', text=('Save + unload outskirts' if root and resident(ctx.scene,root,'outer') else 'Load original outskirts'), icon='WORLD')
    help_text(column,'Optional original scenery outside the preserved map core.')
    save_buttons=column.column(); save_buttons.enabled=bool(root and resident(ctx.scene,root))
    save_buttons.operator('wz09.map_details_save', text='Save detail version', icon='FILE_TICK')
    save_buttons.operator('wz09.city_assets_unload', text='3. Save + unload map', icon='EXPORT')
    help_text(column,'Writes new detail files and frees unused detail data. Ctrl-S saves the composition and its references.')
    box.prop(ctx.scene, 'wz09_map_labels', text='Show map names')
    box.operator('wz09.browse_local_assets',text='Browse local assets',icon='ASSET_MANAGER')
    box.operator('wz09.map_helper_visibility', text='Show / hide legacy guides', icon='HIDE_OFF')
    box.prop(ctx.scene, 'wz09_map_advanced', text='Advanced tools')


def register():
    import bpy
    from bpy.props import BoolProperty
    class WZ09_OT_map_details_save(bpy.types.Operator):
        bl_idname='wz09.map_details_save'; bl_label='Save detail version'
        bl_description='Save all loaded layers of the selected map to new versioned files. Press Ctrl-S afterwards to save composition references'
        @classmethod
        def poll(cls,ctx): return ctx.mode=='OBJECT' and ctx.active_object is not None
        def execute(self, ctx):
            try:
                from . import city_editing as city
                root = city.map_root(ctx.active_object)
                for layer in ('core','outer'):
                    if resident(ctx.scene,root,layer): save(ctx,root,layer=layer)
                self.report({'INFO'}, 'New detail file saved. Press Ctrl-S to save the composition references.'); return {'FINISHED'}
            except (ValueError, RuntimeError, OSError, KeyError) as exc:
                self.report({'ERROR'}, str(exc)[:220]); return {'CANCELLED'}
    class WZ09_OT_map_outskirts(bpy.types.Operator):
        bl_idname='wz09.map_outskirts'; bl_label='Load / unload outskirts'
        bl_description='Load editable original scenery outside this map core. Click again to save a new version and unload it'
        @classmethod
        def poll(cls,ctx): return ctx.mode=='OBJECT' and ctx.active_object is not None
        def execute(self, ctx):
            try: toggle_outer(ctx); return {'FINISHED'}
            except (ValueError, RuntimeError, OSError, KeyError) as exc:
                self.report({'ERROR'}, str(exc)[:220]); return {'CANCELLED'}
    class WZ09_OT_map_helper_visibility(bpy.types.Operator):
        bl_idname='wz09.map_helper_visibility'; bl_label='Toggle legacy guides'; bl_options={'UNDO'}
        bl_description='Toggle preserved old labels, authoring guides and Blender relationship lines. Current map names have a separate switch'
        def execute(self, ctx):
            ctx.scene['wz09_helpers_visible'] = not ctx.scene.get('wz09_helpers_visible', False)
            visible = ctx.scene['wz09_helpers_visible']
            for obj in ctx.scene.objects:
                if obj.get('wz09_legacy_helper'):
                    obj.hide_set(not visible)
            for area in ctx.screen.areas if ctx.screen else ():
                if area.type == 'VIEW_3D': area.spaces.active.overlay.show_relationship_lines = visible
            return {'FINISHED'}
    for cls in (WZ09_OT_map_details_save, WZ09_OT_map_outskirts, WZ09_OT_map_helper_visibility):
        bpy.utils.register_class(cls); _CLASSES.append(cls)
    bpy.types.Scene.wz09_map_labels = BoolProperty(name='Map names', default=True)
    bpy.types.Scene.wz09_map_advanced = BoolProperty(name='Advanced tools', default=False)
    global _HANDLE
    if not bpy.app.background:
        _HANDLE = bpy.types.SpaceView3D.draw_handler_add(_draw_labels, (), 'WINDOW', 'POST_PIXEL')
    # Keep specialist panels available behind one explicit switch in library
    # workspaces. Their original workspace/category conditions still apply.
    for panel in bpy.types.Panel.__subclasses__():
        if not panel.__module__.startswith('wz2009_map_editor') or panel.__name__=='WZ09_PT_main': continue
        if getattr(panel,'bl_category','')!='WZ2009': continue
        descriptor=panel.__dict__.get('poll')
        previous=getattr(panel,'poll',None)
        def poll(cls,ctx,prior=previous):
            if ctx.scene.get('wz09_library_workspace') and not ctx.scene.wz09_map_advanced: return False
            return bool(prior(ctx)) if prior else True
        _PANEL_POLLS.append((panel,descriptor))
        panel.poll=classmethod(poll)


def unregister():
    import bpy
    global _HANDLE
    for panel,descriptor in _PANEL_POLLS:
        if descriptor is None: delattr(panel,'poll')
        else: panel.poll=descriptor
    _PANEL_POLLS.clear()
    if _HANDLE is not None: bpy.types.SpaceView3D.draw_handler_remove(_HANDLE, 'WINDOW'); _HANDLE = None
    for cls in reversed(_CLASSES): bpy.utils.unregister_class(cls)
    _CLASSES.clear()
    for field in ('wz09_map_labels', 'wz09_map_advanced'):
        if hasattr(bpy.types.Scene, field): delattr(bpy.types.Scene, field)
