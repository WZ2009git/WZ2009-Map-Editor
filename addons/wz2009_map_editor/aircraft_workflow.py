"""Aircraft route authoring UI and disposable viewport-only route polylines.

The overlay is an editor aid. It is neither flight simulation nor clearance,
navigation or collision evidence, and is excluded from all authored geometry.
"""
import hashlib
import json
import math
import uuid

try:
    import bpy
except ImportError:
    bpy = None

try:
    from .authoring_helpers import aircraft_routes
except ImportError:
    from authoring_helpers import aircraft_routes


def _fingerprint(markers):
    records = [item for item in markers if item.get('kind') == 'aircraft_keyframe']
    payload = json.dumps(sorted(records, key=lambda item: item['id']), sort_keys=True,
                         separators=(',', ':'), allow_nan=False).encode()
    return hashlib.sha256(payload).hexdigest()


def _report(markers):
    report = aircraft_routes(markers)
    by_id = {marker['id']: marker for marker in markers}
    for route in report['routes']:
        route['sequences'] = [by_id[uid]['properties']['sequence'] for uid in route['keyframe_ids']]
    report.update(ok=True, source_fingerprint=_fingerprint(markers),
                  scope='editor_authoring_only', clearance_verified=False)
    return report


def collect_route_report(scene):
    try:
        from .scene_io import export_blender_scene
    except ImportError:
        from scene_io import export_blender_scene
    return _report(export_blender_scene(scene)['markers'])


def route_preview_plan(markers, route_id):
    """Return a validated ordered polyline in world coordinates, without edits."""
    if not isinstance(route_id, str) or not route_id.strip():
        raise ValueError('Set an aircraft route ID before building its editor polyline')
    report = _report(markers)
    route = next((item for item in report['routes'] if item['route_id'] == route_id), None)
    if route is None:
        raise ValueError(f'Aircraft route {route_id}: no timed keyframes found')
    if not route['complete_polyline']:
        raise ValueError(f'Aircraft route {route_id}: at least two timed keyframes required')
    points = route['positions_m']
    if any(not isinstance(p, (tuple,list)) or len(p) != 3 or any(
            type(v) not in (int,float) or not math.isfinite(v) for v in p) for p in points):
        raise ValueError('Aircraft route preview needs finite world positions')
    return dict(route, source_fingerprint=report['source_fingerprint'],
                runtime_verified=False, clearance_verified=False)


def _same(a, b):
    return a is b or (hasattr(a, 'as_pointer') and hasattr(b, 'as_pointer')
                      and a.as_pointer() == b.as_pointer())


def _previous_overlay(scene, route_id):
    previous = [obj for obj in scene.objects if obj.get('wz09_editor_overlay') is True
        and obj.get('wz09_overlay_kind') == 'aircraft_route' and obj.get('wz09_route_id') == route_id]
    if len(previous) > 1:
        raise ValueError('Multiple route overlays share this route ID; remove the duplicate editor overlays first')
    for obj in previous:
        if (obj.type != 'CURVE' or not obj.get('wz09_overlay_id') or obj.get('wz09_type')
                or obj.get('wz09_custom_geometry') or obj.get('wz09_preview_only') or obj.get('wz09_preview_id')):
            raise ValueError('Existing route object is not an exclusively tagged editor overlay')
        owners = list(getattr(obj, 'users_scene', ()))
        if (len(owners) != 1 or not _same(owners[0], scene) or obj.library or obj.override_library
                or obj.data.library or obj.data.override_library or obj.data.users != 1):
            raise ValueError('Linked, shared or foreign-scene route overlays cannot be replaced')
    return previous[0] if previous else None


def _overlay_collection(scene, api):
    if not scene.get('wz09_scene_id'):
        scene['wz09_scene_id'] = str(uuid.uuid4())
    for collection in scene.collection.children:
        if (collection.get('wz09_scene_id') == scene['wz09_scene_id']
                and collection.get('wz09_collection_kind') == 'WZ09_EDITOR_OVERLAYS'):
            return collection
    collection = api.data.collections.new('WZ09_EDITOR_OVERLAYS')
    collection['wz09_scene_id'] = scene['wz09_scene_id']
    collection['wz09_collection_kind'] = 'WZ09_EDITOR_OVERLAYS'
    scene.collection.children.link(collection)
    return collection


def build_route_preview(ctx, route_id, *, bpy_module=None, markers=None):
    """Publish a new overlay, replacing only the same owned route's old curve."""
    if markers is None:
        try:
            from .scene_io import export_blender_scene
        except ImportError:
            from scene_io import export_blender_scene
        markers = export_blender_scene(ctx.scene)['markers']
    plan = route_preview_plan(markers, route_id)
    previous = _previous_overlay(ctx.scene, route_id)
    api = bpy_module if bpy_module is not None else bpy
    if api is None:
        raise RuntimeError('Route preview creation requires Blender')
    curve = api.data.curves.new('WZ09 Aircraft Route', 'CURVE')
    obj = None
    try:
        curve.dimensions = '3D'
        curve.resolution_u = 1
        curve.bevel_depth = 0
        spline = curve.splines.new('POLY')
        spline.points.add(len(plan['positions_m'])-1)
        for point, position in zip(spline.points, plan['positions_m']):
            point.co = (*position, 1.0)
        obj = api.data.objects.new('WZ09 Aircraft Route', curve)
        obj.show_in_front = True
        obj.hide_render = True
        obj.display_type = 'WIRE'
        obj.color = (1.0, .55, .1, 1.0)
        obj['wz09_editor_overlay'] = True
        obj['wz09_overlay_kind'] = 'aircraft_route'
        obj['wz09_route_id'] = route_id
        obj['wz09_overlay_id'] = str(uuid.uuid4())
        obj['wz09_route_fingerprint'] = plan['source_fingerprint']
        obj['wz09_clearance_verified'] = False
        _overlay_collection(ctx.scene, api).objects.link(obj)
        if previous is not None:
            old_curve = previous.data
            api.data.objects.remove(previous, do_unlink=True)
            if old_curve.users == 0:
                api.data.curves.remove(old_curve)
        ctx.scene['wz09_aircraft_report_json'] = json.dumps(_report(markers), sort_keys=True, allow_nan=False)
        return obj
    except Exception:
        if obj is not None:
            api.data.objects.remove(obj, do_unlink=True)
        if curve.users == 0:
            api.data.curves.remove(curve)
        raise


classes = ()
if bpy is not None:
    class WZ09_OT_validate_aircraft_route(bpy.types.Operator):
        bl_idname = 'wz09.validate_aircraft_route'
        bl_label = 'Validate Aircraft Routes'

        def execute(self, ctx):
            try:
                report = collect_route_report(ctx.scene)
                ctx.scene['wz09_aircraft_report_json'] = json.dumps(report, sort_keys=True, allow_nan=False)
                self.report({'WARNING'} if report['untimed_keyframes'] else {'INFO'},
                    f'{len(report["routes"])} timed editor routes; {len(report["untimed_keyframes"])} untimed frames')
                return {'FINISHED'}
            except (OSError, ValueError, TypeError, KeyError) as exc:
                ctx.scene['wz09_aircraft_report_json'] = json.dumps({'ok':False, 'error':str(exc)[:300]})
                self.report({'ERROR'}, str(exc)[:220])
                return {'CANCELLED'}


    class WZ09_OT_build_aircraft_route_preview(bpy.types.Operator):
        bl_idname = 'wz09.build_aircraft_route_preview'
        bl_label = 'Build Aircraft Route Editor Line'
        bl_options = {'REGISTER', 'UNDO'}

        def execute(self, ctx):
            try:
                build_route_preview(ctx, ctx.scene.wz09_aircraft_route_id)
                self.report({'INFO'}, 'Editor route line rebuilt; terrain clearance and flight behavior unverified')
                return {'FINISHED'}
            except (OSError, ValueError, TypeError, KeyError, RuntimeError) as exc:
                self.report({'ERROR'}, str(exc)[:220])
                return {'CANCELLED'}


    class WZ09_PT_aircraft_routes(bpy.types.Panel):
        bl_label = 'Aircraft Route Authoring'
        bl_idname = 'WZ09_PT_aircraft_routes'
        bl_parent_id = 'WZ09_PT_main'
        bl_space_type = 'VIEW_3D'
        bl_region_type = 'UI'
        bl_category = 'WZ2009'
        bl_options = {'DEFAULT_CLOSED'}

        @classmethod
        def poll(cls,ctx):return getattr(ctx.scene,'wz09_workspace','PLACE')=='PLACE'

        def draw(self, ctx):
            layout = self.layout
            layout.prop(ctx.scene, 'wz09_aircraft_route_id', text='Route ID')
            layout.prop(ctx.scene, 'wz09_aircraft_interval_s', text='New frame interval (s)')
            layout.label(text='Add aircraft keyframes with Gameplay Marker')
            active = ctx.active_object
            if active is not None and active.get('wz09_type') == 'aircraft_keyframe':
                for prop, label in (('wz09_route_id','Frame route ID'),('wz09_route_sequence','Sequence'),
                                    ('wz09_route_time_s','Relative time (s)')):
                    if prop in active:
                        layout.prop(active, '["'+prop+'"]', text=label)
                if not active.get('wz09_route_id'):
                    layout.label(text='Legacy untimed frame; add a timed frame for a route')
            layout.operator('wz09.validate_aircraft_route')
            layout.operator('wz09.build_aircraft_route_preview')
            try:
                report = json.loads(ctx.scene.get('wz09_aircraft_report_json', 'null'))
            except (ValueError, TypeError):
                report = None
            if not report:
                layout.label(text='Validate after editing positions or route fields')
            elif not report.get('ok'):
                layout.label(text=str(report.get('error', 'Route validation failed'))[:90], icon='ERROR')
            else:
                layout.label(text='Last validation snapshot; validate again after edits')
                for route in report['routes'][:20]:
                    box = layout.box()
                    box.label(text=f'{route["route_id"]}: {len(route["keyframe_ids"])} frames / {route["duration_s"]:g} s')
                    for index, time, uid in zip(route['sequences'][:32], route['times_s'][:32], route['keyframe_ids'][:32]):
                        box.label(text=f'{index}: {time:g} s / {uid[:12]}')
                if report['untimed_keyframes']:
                    layout.label(text=f'{len(report["untimed_keyframes"])} untimed legacy frames')
            layout.label(text='Viewport line only; no clearance or flight validation')

    classes = (WZ09_OT_validate_aircraft_route, WZ09_OT_build_aircraft_route_preview, WZ09_PT_aircraft_routes)
