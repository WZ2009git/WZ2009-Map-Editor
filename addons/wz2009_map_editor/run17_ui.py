'Karvorsk RUN17 compact editor preset. Build into a new export folder; load terrain, road props and vegetation as linked instances; place lamps and barriers along a selected curve.'
import json, math, os, glob
from pathlib import Path
import bpy
from . import run17_kompakt as K

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent   # ...\WZ2009 MAP EDITOR
COLL = 'WZ09 RUN17-kompakt'


def _latest(pattern):
    c = sorted(glob.glob(str(ROOT/'exports'/pattern)), key=os.path.getmtime)
    return c[-1] if c else None


def build_run17(drops=40000):
    src = _latest('karvorsk-landscape-RUN16-KH-COLLV2-*') or _latest('karvorsk-landscape-RUN16-*')
    if not src: raise FileNotFoundError('No RUN16 export found')
    data = json.loads((HERE/'run17_data.json').read_text(encoding='utf-8'))
    return K.build(os.path.join(src, 'world.json'), data['map_bounds'], data['water_local'], str(ROOT/'exports'), drops)


def _coll(scene, name):
    c = bpy.data.collections.get(name)
    if c is None:
        c = bpy.data.collections.new(name); scene.collection.children.link(c)
    return c


def _source_object(asset, cache):
    if asset in cache: return cache[asset]
    ob = None
    for o in bpy.data.objects:
        if o.type == 'MESH' and o.name.split('.')[0].split('#')[0] == asset:
            ob = o; break
    cache[asset] = ob; return ob


def place_instances(scene, coll, rows, cache):
    """rows: (asset, x, y, z, yaw_deg, scale). Linked copies share mesh data; else empties."""
    made = 0
    for asset, x, y, z, yaw, s in rows:
        src = _source_object(asset, cache)
        if src is not None:
            ob = src.copy(); ob.parent = None
        else:
            ob = bpy.data.objects.new(asset, None); ob.empty_display_type = 'CUBE'; ob.empty_display_size = 1.
        ob.location = (x, y, z); ob.rotation_euler = (0., 0., math.radians(yaw)); ob.scale = (s, s, s)
        ob['wz09_asset'] = asset; coll.objects.link(ob); made += 1
    return made


def load_run17(context, job=None):
    job = job or _latest('karvorsk-landscape-RUN17-kompakt-*')
    if not job: raise FileNotFoundError('No RUN17 export yet; run Build RUN17 compact first')
    scene = context.scene; root = _coll(scene, COLL)
    before = set(bpy.data.objects)
    bpy.ops.import_scene.gltf(filepath=os.path.join(job, 'generated-terrain.glb'))
    terr = _coll(scene, COLL+' - Terrain')
    if terr.name not in root.children: 
        try: scene.collection.children.unlink(terr)
        except RuntimeError: pass
        root.children.link(terr)
    for ob in set(bpy.data.objects)-before:
        for c in list(ob.users_collection): c.objects.unlink(ob)
        terr.objects.link(ob)
    cache = {}
    rp = json.load(open(os.path.join(job, 'road_props.json')))
    pc = bpy.data.collections.new(COLL+' · Strassen-Props'); root.children.link(pc)
    n1 = place_instances(scene, pc, [(p['asset'], *p['position_m'], p['yaw_deg'], 1.) for p in rp['props']], cache)
    vg = json.load(open(os.path.join(job, 'vegetation_scatter.json')))
    vc = bpy.data.collections.new(COLL+' · Vegetation'); root.children.link(vc)
    n2 = place_instances(scene, vc, [r[:6] for r in vg['instances']], cache)
    scene['wz09_run17_job'] = job
    return job, n1, n2


def props_along_curve(context, ob, biome='green'):
    mw = ob.matrix_world; pts = []
    for sp in ob.data.splines:
        src = sp.bezier_points if sp.type == 'BEZIER' else sp.points
        pts += [tuple((mw @ p.co.to_3d()) if hasattr(p.co, 'to_3d') else (mw @ p.co)) for p in src]
    if len(pts) < 2: raise ValueError('Curve requires at least 2 points')
    res = [pts[0]]
    for a, b in zip(pts, pts[1:]):
        L = math.dist(a, b); k = max(1, int(L//4))
        res += [tuple(a[i]+(b[i]-a[i])*j/k for i in range(3)) for j in range(1, k+1)]
    rows = []; acc = 0.
    rail = {'snow': K.ROAD_PROPS['snow'], 'desert': 'bc_hesco_barrier_med'}.get(biome, K.ROAD_PROPS['guardrail'])
    lamp = 'roadkill_cd_powerpole' if biome == 'desert' else K.ROAD_PROPS['lamp']
    for i, (a, b) in enumerate(zip(res, res[1:])):
        L = math.dist(a[:2], b[:2]) or 1.; dx, dy = (b[0]-a[0])/L, (b[1]-a[1])/L; nx, ny = -dy, dx
        yaw = math.degrees(math.atan2(dy, dx)); acc += L; grade = abs(b[2]-a[2])/L
        if acc >= 30.:
            acc = 0.; side = 1 if (i//8) % 2 else -1
            rows.append((lamp, a[0]+nx*7*side, a[1]+ny*7*side, a[2], yaw+90*side, 1.))
        if grade > .08 or ob.get('wz09_bridge'):
            for side in (-1, 1): rows.append((rail, a[0]+nx*6*side, a[1]+ny*6*side, a[2], yaw, 1.))
    c = bpy.data.collections.new('WZ09 Props · '+ob.name); context.scene.collection.children.link(c)
    return place_instances(context.scene, c, rows, {})


class WZ09_OT_run17_build(bpy.types.Operator):
    bl_idname = 'wz09.run17_build'; bl_label = 'Build RUN17 compact'
    bl_description = 'Rebuild Karvorsk 3 by 3 km terrain, river, roads, vegetation and collision into a new export folder. RUN16 is retained. Expected duration: 1-3 minutes'
    def execute(self, context):
        try: job, plan, _ = build_run17()
        except (OSError, ValueError, KeyError) as e:
            self.report({'ERROR'}, str(e)[:200]); return {'CANCELLED'}
        self.report({'INFO'}, 'RUN17 fertig: ' + os.path.basename(job)); return {'FINISHED'}


class WZ09_OT_run17_load(bpy.types.Operator):
    bl_idname = 'wz09.run17_load'; bl_label = 'Load RUN17'
    bl_description = 'Load the latest RUN17 terrain, road props and vegetation into a separate removable collection'
    def execute(self, context):
        try: job, a, b = load_run17(context)
        except (OSError, ValueError, KeyError, RuntimeError) as e:
            self.report({'ERROR'}, str(e)[:200]); return {'CANCELLED'}
        self.report({'INFO'}, 'Loaded: %d road props, %d plants/rocks' % (a, b)); return {'FINISHED'}


class WZ09_OT_run17_road_props(bpy.types.Operator):
    bl_idname = 'wz09.run17_road_props'; bl_label = 'Lamps and barriers along road'
    bl_description = 'Selected curve: lamp every 30 m, barrier every 4 m on slopes above 8 percent, or everywhere for a bridge curve'
    biome: bpy.props.EnumProperty(items=[('green', 'Gruen/Stadt', ''), ('desert', 'Wueste', ''), ('snow', 'Schnee', '')])
    def execute(self, context):
        ob = context.active_object
        if ob is None or ob.type != 'CURVE':
            self.report({'ERROR'}, 'Select a road curve first'); return {'CANCELLED'}
        try: n = props_along_curve(context, ob, self.biome)
        except ValueError as e:
            self.report({'ERROR'}, str(e)); return {'CANCELLED'}
        self.report({'INFO'}, '%d Props gesetzt' % n); return {'FINISHED'}


class WZ09_OT_current_terrain_view(bpy.types.Operator):
    bl_idname = 'wz09.current_terrain_view'
    bl_label = 'Current terrain view'
    bl_options = {'REGISTER', 'UNDO'}
    authoring: bpy.props.BoolProperty(default=False)
    def execute(self, context):
        for obj in context.scene.objects:
            if obj.get('wz09_authoring_terrain'):
                obj.hide_set(not self.authoring)
            elif obj.get('wz09_current_runtime_tile_preview'):
                obj.hide_set(self.authoring)
        return {'FINISHED'}


class WZ09_OT_current_prop_detail(bpy.types.Operator):
    bl_idname = 'wz09.current_prop_detail'
    bl_label = 'Viewport prop detail'
    bl_options = {'REGISTER', 'UNDO'}
    lightweight: bpy.props.BoolProperty(default=True)
    def execute(self, context):
        from . import compact_preview
        compact_preview.viewport_lod(context.scene,self.lightweight)
        return {'FINISHED'}


class WZ09_OT_current_edit_focus(bpy.types.Operator):
    bl_idname = 'wz09.current_edit_focus'
    bl_label = 'City editing focus'
    overview: bpy.props.BoolProperty(default=False)
    def execute(self, context):
        from . import overview_preview
        uid=None if self.overview else overview_preview.selected_owner(context.active_object)
        if uid is None and not self.overview:
            self.report({'WARNING'},'Select a city preview or map root first')
            return {'CANCELLED'}
        overview_preview.set_focus(context.scene,uid)
        window=context.window
        if window:
            proxy=next((o for o in context.scene.objects if o.get(overview_preview.PROXY)==uid),None) if uid else None
            for area in window.screen.areas:
                if area.type=='VIEW_3D':
                    view=area.spaces.active.region_3d
                    if proxy:
                        from mathutils import Vector
                        center=sum((Vector(p) for p in proxy.bound_box),Vector())/8
                        view.view_location=proxy.matrix_world@center
                        view.view_distance=max(700,max(proxy.dimensions.x,proxy.dimensions.y)*1.8)
                    else:
                        view.view_location=(0,0,50);view.view_distance=5200
                    area.spaces.active.clip_end=max(area.spaces.active.clip_end,view.view_distance*4)
        return {'FINISHED'}


def draw(layout, context):
    if context.object is not None and context.object.get('wz09_compact_records_json'):
        layout.operator('wz09.restore_compact_objects', text='Edit individual objects', icon='OUTLINER_OB_MESH')
    if context.scene.get('wz09_current_map'):
        current = layout.box()
        current.label(text='Current game layout · RUN18', icon='CHECKMARK')
        if context.scene.get('wz09_overview_statistics_json'):
            row=current.row(align=True)
            row.operator('wz09.current_edit_focus',text='World overview').overview=True
            row.operator('wz09.current_edit_focus',text='Edit selected city').overview=False
        row = current.row(align=True)
        row.operator('wz09.current_terrain_view', text='Game terrain').authoring = False
        row.operator('wz09.current_terrain_view', text='Detailed terrain').authoring = True
        row = current.row(align=True)
        row.operator('wz09.current_prop_detail', text='Light props').lightweight = True
        row.operator('wz09.current_prop_detail', text='Full props').lightweight = False
        if not context.scene.wz09_show_advanced_import:
            return
    box = layout.box(); box.label(text='Karvorsk RUN17 compact (3 by 3 km)', icon='WORLD')
    box.operator('wz09.run17_build', icon='MOD_OCEAN')
    box.operator('wz09.run17_load', icon='IMPORT')
    row = box.row(align=True); row.label(text='Road:')
    for k, l in (('green', 'Gruen'), ('desert', 'Wueste'), ('snow', 'Schnee')):
        row.operator('wz09.run17_road_props', text=l).biome = k
    job = context.scene.get('wz09_run17_job')
    if job: box.label(text=os.path.basename(job)[-24:], icon='CHECKMARK')


classes = (WZ09_OT_run17_build, WZ09_OT_run17_load, WZ09_OT_run17_road_props, WZ09_OT_current_terrain_view, WZ09_OT_current_prop_detail, WZ09_OT_current_edit_focus)
def register():
    for c in classes: bpy.utils.register_class(c)
    from . import compact_preview
    compact_preview.register()
def unregister():
    from . import compact_preview
    compact_preview.unregister()
    for c in reversed(classes): bpy.utils.unregister_class(c)
