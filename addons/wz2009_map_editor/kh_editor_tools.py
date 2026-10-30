"""Leistung & Kollision fuer grosse Karvorsk-Szenen (Kleiner Huen, 2026-10-19).

Modular: register()/unregister() only; one draw hook in WZ09_PT_main is not
needed because this module adds its own sub panel. Never deletes data.
  * Szene organisieren: map-preview objects -> per-city child collections
    (inside the scene's WZ09_PREVIEWS collection, so all_objects stays valid)
  * per city: Voll / Kasten / Proxy / Aus, 'Alle aus ausser Auswahl'
  * Schnellansicht (auto on load for scenes > 20.000 objects), reversible
  * Proxy: merged per-material copies of a city (originals untouched)
  * Kollision: wz09_solid flag, box / convex hull proxies, open mesh check,
    red display toggle, native-collision flag per city for the export
"""
import json
import bpy
from bpy.app.handlers import persistent

CITY_PREFIX = 'WZ09 Stadt · '
PROXY_PREFIX = 'WZ09 Proxy · '
COLL_COLLISION = 'WZ09 Kollision'
SKIP_COLLECTION_HINTS = ('WZ09_MAPS', 'Terrain', 'MARKERS', 'NAV', 'GEOMETRY', 'OVERLAYS', 'Cinematic', 'Collision', 'ocean', PROXY_PREFIX)
BIG_SCENE = 20000
DEFAULT_NATIVE = {'mp_invasion'}
MODES = (('FULL', 'Full detail', 'HIDE_OFF'), ('BOUNDS', 'Bounds', 'CUBE'), ('PROXY', 'Proxy', 'MESH_ICOSPHERE'), ('OFF', 'Off', 'HIDE_ON'))


# ----------------------------------------------------------------- helpers
def _layer_collection(view_layer, coll):
    stack = [view_layer.layer_collection]
    while stack:
        lc = stack.pop()
        if lc.collection == coll:
            return lc
        stack.extend(lc.children)
    return None


def map_root(obj):
    o = obj
    for _ in range(64):
        if o is None:
            return None
        if o.get('wz09_source_map') and o.type == 'EMPTY':
            return o
        o = o.parent
    return None


def city_collections(scene):
    return [c for c in scene.collection.children_recursive if c.get('wz09_city')]


def preview_parent(scene):
    for c in scene.collection.children:
        if c.name.startswith('WZ09_PREVIEWS'):
            return c
    return scene.collection


def _skip(coll):
    return any(h in coll.name for h in SKIP_COLLECTION_HINTS)


def organize_scene(scene):
    """Idempotent. Returns stats dict."""
    parent = preview_parent(scene)
    by_city, moved, skipped = {}, 0, 0
    # obj.users_collection scans every collection per call (quadratic on
    # 160k objects); build the membership map once instead.
    members = {}
    for coll in [scene.collection] + list(scene.collection.children_recursive):
        for o in coll.objects:
            members.setdefault(o.as_pointer(), []).append(coll)
    roots = {}
    for obj in list(scene.objects):
        p = obj.parent
        root = None
        chain = []
        o = obj
        for _ in range(64):
            if o is None:
                break
            key0 = o.as_pointer()
            if key0 in roots:
                root = roots[key0]; break
            chain.append(key0)
            if o.type == 'EMPTY' and o.get('wz09_source_map'):
                root = o; break
            o = o.parent
        for k0 in chain:
            roots[k0] = root
        if root is None:
            continue
        cols = members.get(obj.as_pointer(), [])
        if any(c.get('wz09_overview_source_map_id') for c in cols):
            skipped += 1
            continue
        if any(c.get('wz09_city') for c in cols):
            continue
        if any(_skip(c) for c in cols):
            skipped += 1
            continue
        key = root.name
        coll = by_city.get(key)
        if coll is None:
            name = CITY_PREFIX + key
            coll = bpy.data.collections.get(name)
            if coll is None:
                coll = bpy.data.collections.new(name)
                coll['wz09_city'] = key
                coll['wz09_source_map'] = root['wz09_source_map']
                coll['wz09_city_mode'] = 'FULL'
                coll['wz09_native_collision_loaded'] = root['wz09_source_map'] in DEFAULT_NATIVE
            try:
                parent.children.link(coll)
            except RuntimeError:
                pass
            by_city[key] = coll
        obj['wz09_kh_orig_collections'] = json.dumps([c.name for c in cols])
        coll.objects.link(obj)
        for c in cols:
            c.objects.unlink(obj)
        moved += 1
    return dict(moved=moved, cities=len(by_city), skipped=skipped)


def unorganize_scene(scene):
    restored = 0
    for coll in city_collections(scene):
        for obj in list(coll.objects):
            names = json.loads(obj.get('wz09_kh_orig_collections', '[]'))
            targets = [bpy.data.collections.get(n) for n in names]
            for t in targets:
                if t is not None:
                    try:
                        t.objects.link(obj)  # 'in' checks are O(n) by name; link refuses duplicates
                    except RuntimeError:
                        pass
            if not any(targets):
                scene.collection.objects.link(obj)
            coll.objects.unlink(obj)
            if 'wz09_kh_orig_collections' in obj:
                del obj['wz09_kh_orig_collections']
            restored += 1
        lc = _layer_collection(bpy.context.view_layer, coll)
        if lc:
            lc.exclude = False
    return restored


def set_city_mode(context, coll, mode):
    vl = context.view_layer
    lc = _layer_collection(vl, coll)
    proxy = bpy.data.collections.get(PROXY_PREFIX + coll['wz09_city'])
    if mode == 'PROXY' and proxy is None:
        mode = 'BOUNDS'
    if lc is not None:
        lc.exclude = mode in {'OFF', 'PROXY'}
    if proxy is not None:
        plc = _layer_collection(vl, proxy)
        if plc is not None:
            plc.exclude = mode != 'PROXY'
    if mode in {'FULL', 'BOUNDS'}:
        for obj in coll.all_objects:
            if obj.type != 'MESH':
                continue
            if mode == 'BOUNDS':
                if obj.display_type != 'BOUNDS':
                    obj['wz09_kh_display'] = obj.display_type
                    obj.display_type = 'BOUNDS'
            elif obj.display_type == 'BOUNDS' and 'wz09_kh_display' in obj:
                obj.display_type = obj['wz09_kh_display']
                del obj['wz09_kh_display']
    coll['wz09_city_mode'] = mode
    return mode


def city_of_object(obj):
    if obj is None:
        return None
    for c in obj.users_collection:
        if c.get('wz09_city'):
            return c
    root = map_root(obj)
    return bpy.data.collections.get(CITY_PREFIX + root.name) if root else None


# ------------------------------------------------------------ quick view
def apply_quickview(context, enable):
    scene = context.scene
    prefs = context.preferences
    if enable:
        if scene.get('wz09_kh_quickview_prev'):
            return
        prev = dict(spaces=[], simplify=scene.render.use_simplify,
                    subdiv=scene.render.simplify_subdivision,
                    tex=prefs.system.gl_texture_limit, undo=prefs.edit.undo_steps)
        scene.render.use_simplify = True
        scene.render.simplify_subdivision = 0
        prefs.system.gl_texture_limit = 'CLAMP_1024'
        prefs.edit.undo_steps = min(prefs.edit.undo_steps, 8)
        for screen in bpy.data.screens:
            for area in screen.areas:
                if area.type != 'VIEW_3D':
                    continue
                sp = area.spaces.active
                prev['spaces'].append([screen.name, area.x, area.y, sp.shading.type, sp.shading.color_type, sp.clip_end])
                if sp.shading.type in {'MATERIAL', 'RENDERED'}:
                    sp.shading.type = 'SOLID'
                sp.shading.color_type = 'MATERIAL'
                sp.clip_end = min(sp.clip_end, 6000.0)
        scene['wz09_kh_quickview_prev'] = json.dumps(prev)
    else:
        raw = scene.get('wz09_kh_quickview_prev')
        if not raw:
            return
        prev = json.loads(raw)
        scene.render.use_simplify = prev['simplify']
        scene.render.simplify_subdivision = prev['subdiv']
        prefs.system.gl_texture_limit = prev['tex']
        prefs.edit.undo_steps = prev['undo']
        for name, x, y, stype, ctype, clip in prev['spaces']:
            screen = bpy.data.screens.get(name)
            if not screen:
                continue
            for area in screen.areas:
                if area.type == 'VIEW_3D' and area.x == x and area.y == y:
                    sp = area.spaces.active
                    sp.shading.type, sp.shading.color_type, sp.clip_end = stype, ctype, clip
        del scene['wz09_kh_quickview_prev']
    scene['wz09_kh_quickview'] = bool(enable)


@persistent
def _after_load(*_):
    try:
        ctx = bpy.context
        scene = ctx.scene
        if scene is None or scene.get('wz09_kh_quickview_optout'):
            return
        if len(bpy.data.objects) >= BIG_SCENE and not scene.get('wz09_kh_quickview_prev'):
            apply_quickview(ctx, True)
            print('WZ09 Schnellansicht automatisch aktiviert (%d Objekte)' % len(bpy.data.objects))
    except Exception as exc:  # never break file loading
        print('WZ09 Schnellansicht: uebersprungen', exc)


# ------------------------------------------------------------- proxies
def build_city_proxy(context, coll, max_verts=2000000):
    import numpy as np
    key = coll['wz09_city']
    pname = PROXY_PREFIX + key
    proxy = bpy.data.collections.get(pname)
    if proxy is None:
        proxy = bpy.data.collections.new(pname)
        proxy['wz09_proxy_of'] = key
        preview_parent(context.scene).children.link(proxy)
    for o in list(proxy.objects):
        return dict(objects=len(proxy.objects), reused=True)
    groups = {}
    for obj in coll.all_objects:
        if obj.type != 'MESH' or not obj.data.polygons:
            continue
        mat = obj.active_material.name if obj.active_material else ''
        groups.setdefault(mat, []).append(obj)
    made, src = 0, 0
    for mat, objs in groups.items():
        chunk, nv = [], 0
        def flush(chunk):
            nonlocal made
            if not chunk:
                return
            vs, ls, ps, off, loff = [], [], [], 0, 0
            for o in chunk:
                me = o.data
                co = np.empty(len(me.vertices) * 3, dtype=np.float32); me.vertices.foreach_get('co', co)
                co = co.reshape(-1, 3)
                m = np.array(o.matrix_world, dtype=np.float64)
                co = (co @ m[:3, :3].T + m[:3, 3]).astype(np.float32)
                li = np.empty(len(me.loops), dtype=np.int32); me.loops.foreach_get('vertex_index', li)
                st = np.empty(len(me.polygons), dtype=np.int32); me.polygons.foreach_get('loop_start', st)
                vs.append(co); ls.append(li + off); ps.append(st + loff)
                off += len(co); loff += len(li)
            mesh = bpy.data.meshes.new(pname + ' · ' + (mat or 'none'))
            V = np.concatenate(vs); L = np.concatenate(ls); P = np.concatenate(ps)
            mesh.vertices.add(len(V)); mesh.vertices.foreach_set('co', V.ravel())
            mesh.loops.add(len(L)); mesh.loops.foreach_set('vertex_index', L)
            mesh.polygons.add(len(P)); mesh.polygons.foreach_set('loop_start', P)
            mesh.update(calc_edges=True); mesh.validate(clean_customdata=False)
            if mat and bpy.data.materials.get(mat):
                mesh.materials.append(bpy.data.materials[mat])
            ob = bpy.data.objects.new(mesh.name, mesh)
            ob['wz09_proxy_of'] = key; ob['wz09_preview_only'] = True
            ob.hide_select = True
            proxy.objects.link(ob); made += 1
        for o in objs:
            src += 1
            chunk.append(o); nv += len(o.data.vertices)
            if nv >= max_verts:
                flush(chunk); chunk, nv = [], 0
        flush(chunk)
    return dict(objects=made, source_objects=src, reused=False)


# ----------------------------------------------------------- collision
def _collision_collection(scene):
    c = bpy.data.collections.get(COLL_COLLISION)
    if c is None:
        c = bpy.data.collections.new(COLL_COLLISION)
    if c.name not in scene.collection.children:
        scene.collection.children.link(c)
    return c


def make_collision(context, obj, kind):
    import bmesh
    from mathutils import Vector
    if obj.type != 'MESH':
        return None
    name = obj.name + ' · Kollision'
    old = bpy.data.objects.get(name)
    if old is not None:
        bpy.data.objects.remove(old)  # only our own generated proxy
    bm = bmesh.new()
    if kind == 'BOX':
        corners = [obj.matrix_world @ Vector(c) for c in obj.bound_box]
        for c in corners:
            bm.verts.new(c)
        bm.verts.ensure_lookup_table()
        for f in ((0, 1, 2, 3), (4, 7, 6, 5), (0, 4, 5, 1), (1, 5, 6, 2), (2, 6, 7, 3), (4, 0, 3, 7)):
            bm.faces.new([bm.verts[i] for i in f])
    else:
        mw = obj.matrix_world
        for v in obj.data.vertices:
            bm.verts.new(mw @ v.co)
        bmesh.ops.convex_hull(bm, input=list(bm.verts), use_existing_faces=False)
        loose = [v for v in bm.verts if not v.link_faces]
        if loose:
            bmesh.ops.delete(bm, geom=loose, context='VERTS')
    bmesh.ops.recalc_face_normals(bm, faces=bm.faces)
    bmesh.ops.triangulate(bm, faces=bm.faces)
    me = bpy.data.meshes.new(name); bm.to_mesh(me); bm.free()
    col = bpy.data.objects.new(name, me)
    col['wz09_collision_proxy'] = True
    col['wz09_collision_of'] = obj.name
    col['wz09_collision_kind'] = 'box' if kind == 'BOX' else 'convex_hull'
    root = map_root(obj)
    if root is not None:
        col['wz09_collision_map'] = root.name
    col.display_type = 'WIRE'; col.show_in_front = True; col.color = (1, .1, .1, 1)
    _collision_collection(context.scene).objects.link(col)
    obj['wz09_solid'] = True
    return col


def open_edges(obj):
    import bmesh
    if obj.type != 'MESH':
        return 0
    bm = bmesh.new(); bm.from_mesh(obj.data)
    n = sum(1 for e in bm.edges if not e.is_manifold)
    bm.free()
    return n


def show_collision(context, enable):
    c = _collision_collection(context.scene)
    lc = _layer_collection(context.view_layer, c)
    if lc:
        lc.exclude = False
        lc.hide_viewport = not enable
    for screen in bpy.data.screens:
        for area in screen.areas:
            if area.type == 'VIEW_3D':
                area.spaces.active.shading.wireframe_color_type = 'OBJECT' if enable else 'THEME'
    context.scene['wz09_kh_show_collision'] = bool(enable)


# ------------------------------------------------------------ operators
class WZ09_OT_kh_organize(bpy.types.Operator):
    bl_idname = 'wz09.kh_organize'; bl_label = 'Organize scene'; bl_options = {'REGISTER', 'UNDO'}
    bl_description = 'Organize map objects into city collections; nothing is deleted and the operation can be undone'
    def execute(self, context):
        r = organize_scene(context.scene)
        self.report({'INFO'}, '%(moved)d objects organized into %(cities)d cities' % r)
        return {'FINISHED'}


class WZ09_OT_kh_unorganize(bpy.types.Operator):
    bl_idname = 'wz09.kh_unorganize'; bl_label = 'Undo organization'; bl_options = {'REGISTER', 'UNDO'}
    def execute(self, context):
        n = unorganize_scene(context.scene)
        self.report({'INFO'}, '%d Objekte zurueck in ihre alten Sammlungen' % n)
        return {'FINISHED'}


class WZ09_OT_kh_city_mode(bpy.types.Operator):
    bl_idname = 'wz09.kh_city_mode'; bl_label = 'Map display'; bl_options = {'REGISTER', 'UNDO'}
    collection: bpy.props.StringProperty()
    mode: bpy.props.EnumProperty(items=[(m, l, '') for m, l, _ in MODES])
    def execute(self, context):
        targets = city_collections(context.scene) if self.collection == '*' else [bpy.data.collections.get(self.collection)]
        for c in targets:
            if c is not None:
                set_city_mode(context, c, self.mode)
        return {'FINISHED'}


class WZ09_OT_kh_solo_city(bpy.types.Operator):
    bl_idname = 'wz09.kh_solo_city'; bl_label = 'Hide all except selection'; bl_options = {'REGISTER', 'UNDO'}
    bl_description = 'Show only the selected city; hide all other cities'
    def execute(self, context):
        keep = city_of_object(context.active_object)
        if keep is None:
            self.report({'WARNING'}, 'Select a city object first and organize the scene')
            return {'CANCELLED'}
        for c in city_collections(context.scene):
            set_city_mode(context, c, 'FULL' if c == keep else 'OFF')
        return {'FINISHED'}


class WZ09_OT_kh_quickview(bpy.types.Operator):
    bl_idname = 'wz09.kh_quickview'; bl_label = 'Quick view'; bl_options = {'REGISTER'}
    enable: bpy.props.BoolProperty(default=True)
    def execute(self, context):
        apply_quickview(context, self.enable)
        context.scene['wz09_kh_quickview_optout'] = not self.enable
        return {'FINISHED'}


class WZ09_OT_kh_city_proxy(bpy.types.Operator):
    bl_idname = 'wz09.kh_city_proxy'; bl_label = 'Create proxy'; bl_options = {'REGISTER', 'UNDO'}
    bl_description = 'Create a merged display copy of the map while retaining its original geometry'
    collection: bpy.props.StringProperty()
    def execute(self, context):
        targets = city_collections(context.scene) if self.collection == '*' else [bpy.data.collections.get(self.collection)]
        total = 0
        for c in targets:
            if c is None:
                continue
            r = build_city_proxy(context, c)
            total += r['objects']
            set_city_mode(context, c, 'PROXY')
        self.report({'INFO'}, '%d Proxy-Objekte' % total)
        return {'FINISHED'}


class WZ09_OT_kh_make_collision(bpy.types.Operator):
    bl_idname = 'wz09.kh_make_collision'; bl_label = 'Generate collision'; bl_options = {'REGISTER', 'UNDO'}
    kind: bpy.props.EnumProperty(items=[('BOX', 'Box', ''), ('HULL', 'Huelle', '')])
    def execute(self, context):
        n = 0
        for obj in context.selected_objects:
            if make_collision(context, obj, self.kind):
                n += 1
        self.report({'INFO'}, '%d Kollisionskoerper erzeugt' % n)
        return {'FINISHED'}


class WZ09_OT_kh_check_open(bpy.types.Operator):
    bl_idname = 'wz09.kh_check_open'; bl_label = 'Inspect open facades'
    bl_description = 'Inspect selected objects for open or single-sided meshes; this does not test runtime collision'
    def execute(self, context):
        bad = 0
        for obj in context.selected_objects:
            n = open_edges(obj)
            obj['wz09_open_edges'] = n
            bad += n > 0
        self.report({'WARNING'} if bad else {'INFO'},
                    '%d of %d objects are open; inspect collision generation' % (bad, len(context.selected_objects)))
        return {'FINISHED'}


class WZ09_OT_kh_show_collision(bpy.types.Operator):
    bl_idname = 'wz09.kh_show_collision'; bl_label = 'Show collision in red'
    enable: bpy.props.BoolProperty(default=True)
    def execute(self, context):
        show_collision(context, self.enable)
        return {'FINISHED'}


class WZ09_OT_kh_export_collision(bpy.types.Operator):
    bl_idname = 'wz09.kh_export_collision'; bl_label = 'Export collision and boundaries'
    bl_description = 'Write hole filling, 8 m detail, boundary walls, catch volumes and object collision using schema v2 to a runtime export folder'
    directory: bpy.props.StringProperty(subtype='DIR_PATH')
    def invoke(self, context, event):
        context.window_manager.fileselect_add(self); return {'RUNNING_MODAL'}
    def execute(self, context):
        from . import collision_export_bpy
        try:
            m = collision_export_bpy.publish(context.scene, self.directory)
        except (OSError, ValueError, KeyError) as exc:
            self.report({'ERROR'}, str(exc)[:200]); return {'CANCELLED'}
        s = m['summary']
        self.report({'INFO'}, 'Export v2: %d holes filled, %d detail fields, %d walls, %d object colliders' % (
            s['filled_hole_cells'], s['detail_patches'], s['boundary_walls'], s['object_collision']))
        return {'FINISHED'}


class WZ09_PT_kh_tools(bpy.types.Panel):
    bl_label = 'Performance and collision'; bl_idname = 'WZ09_PT_kh_tools'
    bl_space_type = 'VIEW_3D'; bl_region_type = 'UI'; bl_category = 'WZ2009'; bl_parent_id = 'WZ09_PT_main'
    def draw(self, context):
        a = self.layout; s = context.scene
        box = a.box(); box.label(text='Performance', icon='MEMORY')
        row = box.row(align=True)
        on = bool(s.get('wz09_kh_quickview'))
        row.operator('wz09.kh_quickview', text='Schnellansicht ' + ('AN' if on else 'AUS'),
                     icon='CHECKBOX_HLT' if on else 'CHECKBOX_DEHLT', depress=on).enable = not on
        row = box.row(align=True)
        row.operator('wz09.kh_organize', icon='OUTLINER_COLLECTION')
        row.operator('wz09.kh_unorganize', text='', icon='LOOP_BACK')
        cities = sorted(city_collections(s), key=lambda c: c['wz09_city'])
        if cities:
            box.operator('wz09.kh_solo_city', icon='RESTRICT_SELECT_OFF')
            row = box.row(align=True); row.label(text='All:')
            for m, l, ico in MODES:
                op = row.operator('wz09.kh_city_mode', text='', icon=ico); op.collection = '*'; op.mode = m
            for c in cities:
                row = box.row(align=True)
                row.label(text=c['wz09_city'])
                cur = c.get('wz09_city_mode', 'FULL')
                for m, l, ico in MODES:
                    op = row.operator('wz09.kh_city_mode', text=l, depress=(cur == m)); op.collection = c.name; op.mode = m
                row.operator('wz09.kh_city_proxy', text='', icon='MOD_DECIM').collection = c.name
                row.prop(c, '["wz09_native_collision_loaded"]', text='', icon='LOCKED')
            box.label(text='Lock indicates an original collision source binding', icon='INFO')
        else:
            box.label(text='Tip: click Organize scene first', icon='INFO')
        try:
            from . import run17_ui; run17_ui.draw(a, context)
        except Exception as exc:  # preset UI must never break the panel
            a.label(text='RUN17: %s' % str(exc)[:60], icon='ERROR')
        box = a.box(); box.label(text='Collision', icon='MOD_PHYSICS')
        row = box.row(align=True)
        row.operator('wz09.kh_make_collision', text='Generate box collision', icon='CUBE').kind = 'BOX'
        row.operator('wz09.kh_make_collision', text='Hull collision', icon='MESH_ICOSPHERE').kind = 'HULL'
        box.operator('wz09.kh_check_open', icon='ERROR')
        box.operator('wz09.kh_export_collision', icon='EXPORT')
        shown = bool(s.get('wz09_kh_show_collision'))
        box.operator('wz09.kh_show_collision', text='Show collision in red: ' + ('AN' if shown else 'AUS'),
                     depress=shown).enable = not shown
        ob = context.active_object
        if ob is not None and ob.type == 'MESH' and not ob.get('wz09_collision_proxy'):
            r = box.row(); r.label(text=ob.name[:28])
            if 'wz09_solid' in ob:
                r.prop(ob, '["wz09_solid"]', text='Solid')
            else:
                r.label(text='not solid')
            if ob.get('wz09_open_edges'):
                box.label(text='Offenes Mesh: %d offene Kanten' % ob['wz09_open_edges'], icon='ERROR')


classes = (WZ09_OT_kh_organize, WZ09_OT_kh_unorganize, WZ09_OT_kh_city_mode, WZ09_OT_kh_solo_city,
           WZ09_OT_kh_quickview, WZ09_OT_kh_city_proxy, WZ09_OT_kh_make_collision, WZ09_OT_kh_check_open,
           WZ09_OT_kh_show_collision, WZ09_OT_kh_export_collision, WZ09_PT_kh_tools)


def register():
    for c in classes:
        bpy.utils.register_class(c)
    if _after_load not in bpy.app.handlers.load_post:
        bpy.app.handlers.load_post.append(_after_load)
    try:
        from . import run17_ui; run17_ui.register()
    except Exception as exc:
        print('WZ09 RUN17 UI not registered:', exc)


def unregister():
    try:
        from . import run17_ui; run17_ui.unregister()
    except Exception:
        pass
    if _after_load in bpy.app.handlers.load_post:
        bpy.app.handlers.load_post.remove(_after_load)
    for c in reversed(classes):
        bpy.utils.unregister_class(c)
