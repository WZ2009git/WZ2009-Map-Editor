"""Small, reversible viewport controls for the existing WZ2009 authoring tools.

No preference/keymap changes, source mesh edits, or collision claims. Placement
reuses the add-on's marker/node operators; visibility is local to the view layer.
The numeric/state helpers remain importable for offline tests without Blender.
"""
import importlib.util
import json
import math
from pathlib import Path

try:
    from .authoring_helpers import resolve_owner_map
    from .map_library import MAP_NAMES
except ImportError:
    _spec = importlib.util.spec_from_file_location('_wz09_ux_authoring', Path(__file__).with_name('authoring_helpers.py'))
    _module = importlib.util.module_from_spec(_spec)
    _spec.loader.exec_module(_module)
    resolve_owner_map = _module.resolve_owner_map
    from map_library import MAP_NAMES

MARKERS = frozenset(('loot_crate', 'buy_station', 'player_spawn', 'loadout_drop', 'mission', 'poi', 'aircraft_keyframe'))
CATEGORIES = ('preview', 'nav', 'marker', 'geometry')
_classes = ()
_icons = None
_map_items_cache = []
_enum_strings = {}
_active_placement = None
PROPERTIES = ('wz09_focus_map_id', 'wz09_placement_ground', 'wz09_placement_ground_z',
              'wz09_placement_offset', 'wz09_show_control_help')


def object_category(obj):
    if obj.get('wz09_editor_overlay'):
        return None
    if any(obj.get(k) for k in ('wz09_audio_zone','wz09_audio_contour','wz09_action_element','wz09_action_contour')):
        return 'marker'
    if obj.get('wz09_water_json'):
        return 'geometry'
    if obj.get('wz09_preview_only'):
        return 'preview'
    if obj.get('wz09_type') == 'nav_node':
        return 'nav'
    if obj.get('wz09_type') in MARKERS:
        return 'marker'
    if obj.get('wz09_custom_geometry'):
        return 'geometry'
    return None


def stable_object_key(obj):
    for field in ('wz09_id', 'wz09_preview_id', 'wz09_geometry_id'):
        value = obj.get(field)
        if isinstance(value, str) and value.strip():
            return field + ':' + value
    raise ValueError('Initialize the editor first: an object has no persistent editor ID')


def _same_object(a, b):
    return a is b or (a is not None and b is not None and callable(getattr(a, 'as_pointer', None))
                      and callable(getattr(b, 'as_pointer', None)) and a.as_pointer() == b.as_pointer())


def map_catalog(scene):
    """Stable identities only; duplicate display names are intentionally allowed."""
    rows, seen = [], set()
    for obj in scene.objects:
        if obj.get('wz09_type') != 'map_instance':
            continue
        uid = obj.get('wz09_id')
        if not isinstance(uid, str) or not uid.strip() or uid in seen:
            raise ValueError('Map overview needs unique persistent map IDs')
        seen.add(uid)
        rows.append({'id': uid, 'source_map': str(obj.get('wz09_source_map', 'unknown')),
                     'name': str(getattr(obj, 'name', 'Map')), 'object': obj})
    return sorted(rows, key=lambda item: (item['source_map'], item['name'], item['id']))


def find_map(scene, uid):
    obj = next((item['object'] for item in map_catalog(scene) if item['id'] == uid), None)
    if obj is None:
        raise ValueError('Choose an existing map in the overview')
    return obj


def map_overview_items(scene):
    names = dict(MAP_NAMES)
    result = []
    for row in map_catalog(scene):
        readable = names.get(row['source_map'], row['source_map'])
        label = readable if row['name'] in {row['source_map'], readable} else readable+' · '+row['name']
        tooltip = f'{readable} ({row["source_map"]}) | Object: {row["name"]} | Map ID: {row["id"]}'
        result.append((row['id'], label, tooltip))
    return result


def resolve_asset_root(obj):
    seen = set()
    placement_id = obj.get('wz09_asset_placement_id') if obj is not None else None
    if not placement_id:
        return None
    while obj is not None:
        key = obj.as_pointer() if callable(getattr(obj, 'as_pointer', None)) else id(obj)
        if key in seen:
            raise ValueError('Asset parent hierarchy contains a cycle')
        seen.add(key)
        if obj.get('wz09_asset_placement'):
            if obj.get('wz09_asset_placement_id') != placement_id:
                raise ValueError('Asset parts have conflicting placement IDs')
            return obj
        obj = getattr(obj, 'parent', None)
    return None


def selection_frame_targets(scene, selected, view_layer, viewport=None):
    """Expand selected asset empties/parts to their visible model meshes.

    Returns None for the normal Blender selection path. Selection and mesh data
    remain untouched; matching placement IDs without actual parenting are ignored.
    """
    roots = []
    ordinary = []
    for obj in selected:
        root = resolve_asset_root(obj)
        if root is None:
            ordinary.append(obj)
        elif not any(_same_object(root, other) for other in roots):
            roots.append(root)
    if not roots:
        return None
    targets = []
    found = set()
    for obj in scene.objects:
        if getattr(obj, 'type', None) != 'MESH' or getattr(obj, 'hide_viewport', False):
            continue
        if obj.hide_get(view_layer=view_layer):
            continue
        if callable(getattr(obj, 'visible_get', None)) and not obj.visible_get(view_layer=view_layer, viewport=viewport):
            continue
        root = resolve_asset_root(obj)
        for index, candidate in enumerate(roots):
            if _same_object(root, candidate):
                targets.append(obj)
                found.add(index)
                break
    if len(found) != len(roots):
        raise ValueError('Selected asset has no visible model mesh in this view layer')
    for obj in ordinary:
        if not any(_same_object(obj, other) for other in targets):
            targets.append(obj)
    return targets


def bounds_from_points(points, margin=0.0):
    values = [tuple(float(v) for v in point) for point in points]
    if not values or any(len(point) != 3 or not all(math.isfinite(v) for v in point) for point in values):
        raise ValueError('Framing needs finite 3D points')
    if not math.isfinite(margin) or margin < 0:
        raise ValueError('Frame margin must be finite and nonnegative')
    return ([min(point[i] for point in values)-margin for i in range(3)],
            [max(point[i] for point in values)+margin for i in range(3)])


def frame_parameters(bounds, aspect=1.0):
    low, high = bounds
    values = tuple(low)+tuple(high)
    if (len(values) != 6 or not all(math.isfinite(float(v)) for v in values)
            or any(a > b for a, b in zip(low, high)) or not math.isfinite(aspect) or aspect <= 0):
        raise ValueError('Invalid frame bounds or viewport aspect')
    center = tuple((a+b)*.5 for a, b in zip(low, high))
    # Conservative fit in either orientation. No camera or authored data changes.
    diameter = math.sqrt(sum((b-a)**2 for a, b in zip(low, high)))
    distance = max(3.0, diameter*1.5/max(min(aspect, 1.0), .1))
    return center, distance


def ground_intersection(origin, direction, height=0.0):
    values = tuple(origin)+tuple(direction)+(height,)
    if len(values) != 7 or not all(math.isfinite(float(v)) for v in values):
        raise ValueError('Placement ray must be finite')
    if abs(direction[2]) < 1e-9:
        raise ValueError('View ray is parallel to the chosen ground plane')
    distance = (height-origin[2])/direction[2]
    if distance < 0:
        raise ValueError('Chosen ground plane is behind the viewport')
    return tuple(origin[i]+direction[i]*distance for i in range(3))


def offset_position(position, normal, offset):
    values = tuple(position)+tuple(normal)+(offset,)
    if len(values) != 7 or not all(math.isfinite(float(v)) for v in values):
        raise ValueError('Surface position, normal and offset must be finite')
    length = math.sqrt(sum(float(v)**2 for v in normal))
    if length < 1e-10:
        raise ValueError('Placement surface has no usable normal')
    return tuple(float(position[i])+float(normal[i])*offset/length for i in range(3))


def choose_icon(*candidates, available=()):
    valid = set(available)
    return next((name for name in candidates if name in valid), 'NONE')


def clean_view_settings(space):
    """Only viewport presentation; keep extras so empties/markers remain visible."""
    changed = []
    settings = ((space.overlay, 'show_overlays', True),
                (space.overlay, 'show_relationship_lines', False),
                (space.overlay, 'show_extras', True),
                (space.shading, 'type', 'SOLID'),
                (space.shading, 'color_type', 'TEXTURE'),
                (space.shading, 'light', 'STUDIO'),
                (space.shading, 'show_shadows', True),
                (space.shading, 'show_cavity', True),
                (space.shading, 'show_xray', False))
    for target, field, value in settings:
        if hasattr(target, field):
            setattr(target, field, value)
            changed.append(field)
    return changed


def icon(*candidates):
    """Resolve against the installed Blender build; unknown icons never break UI."""
    global _icons
    if _icons is None:
        import bpy
        try:
            _icons = frozenset(bpy.types.UILayout.bl_rna.functions['operator'].parameters['icon'].enum_items.keys())
        except (AttributeError, KeyError, TypeError):
            _icons = frozenset(('NONE',))
    return choose_icon(*candidates, available=_icons)


def _keyed_objects(scene, category, *, source_only=False):
    if category not in CATEGORIES:
        raise ValueError('Unknown editor visibility category')
    result = {}
    for obj in scene.objects:
        if object_category(obj) != category:
            continue
        if source_only and (obj.get('wz09_asset_placement_id') or obj.get('wz09_asset_placement') or obj.get('wz09_asset_part')):
            continue
        key = stable_object_key(obj)
        if key in result:
            raise ValueError('Duplicate persistent IDs; repair before changing viewport filters')
        result[key] = obj
    if not result:
        raise ValueError('This scene has no objects in that category')
    return result


def _snapshot(scene, field):
    try:
        value = json.loads(str(scene.get(field, '{}')))
    except (ValueError, TypeError) as exc:
        raise ValueError('Stored viewport state is invalid; no objects changed') from exc
    if not isinstance(value, dict):
        raise ValueError('Stored viewport state must be an object')
    return value


def toggle_visibility(context, category):
    """Hide/restore this category in this view layer, preserving manual hides."""
    objects = _keyed_objects(context.scene, category)
    state = _snapshot(context.scene, 'wz09_visibility_snapshot_json')
    layer_key = str(context.view_layer.name)
    layer_state = state.get(layer_key, {})
    if not isinstance(layer_state, dict):
        raise ValueError('Stored view-layer visibility state is invalid')
    previous = layer_state.get(category)
    restoring = previous is not None
    if restoring and (not isinstance(previous, dict) or any(type(v) is not bool for v in previous.values())):
        raise ValueError('Stored category visibility state is invalid')
    current = {key: bool(obj.hide_get(view_layer=context.view_layer)) for key, obj in objects.items()}
    target = {key: previous.get(key, current[key]) if restoring else True for key in objects}
    try:
        for key, obj in objects.items():
            obj.hide_set(target[key], view_layer=context.view_layer)
    except Exception:
        for key, obj in objects.items():
            obj.hide_set(current[key], view_layer=context.view_layer)
        raise
    if restoring:
        layer_state.pop(category)
    else:
        layer_state[category] = current
    state[layer_key] = layer_state
    context.scene['wz09_visibility_snapshot_json'] = json.dumps(state, sort_keys=True)
    return {'category': category, 'restored': restoring, 'count': len(objects)}


def toggle_preview_lock(context):
    """Object selection locks are reversible and never touch external source data."""
    objects = _keyed_objects(context.scene, 'preview', source_only=True)
    for obj in objects.values():
        owners = list(getattr(obj, 'users_scene', ()))
        if getattr(obj, 'library', None) or getattr(obj, 'override_library', None) or len(owners) > 1:
            raise ValueError('Selection lock requires local preview objects belonging to one scene')
    state = _snapshot(context.scene, 'wz09_preview_lock_snapshot_json')
    restoring = bool(state)
    if any(type(v) is not bool for v in state.values()):
        raise ValueError('Stored preview selection state is invalid')
    current = {key: bool(obj.hide_select) for key, obj in objects.items()}
    for key, obj in objects.items():
        obj.hide_select = state.get(key, current[key]) if restoring else True
        if obj.hide_select:
            obj.select_set(False)
            if _same_object(context.view_layer.objects.active, obj):
                context.view_layer.objects.active = None
    context.scene['wz09_preview_lock_snapshot_json'] = json.dumps({} if restoring else current, sort_keys=True)
    return {'locked': not restoring, 'count': len(objects)}


def _view_region(context):
    area = context.area
    if area is None or area.type != 'VIEW_3D':
        raise ValueError('Use this tool in a 3D viewport')
    region = next((item for item in area.regions if item.type == 'WINDOW'), None)
    if region is None or area.spaces.active.region_3d is None:
        raise ValueError('No 3D view region is available')
    return region, area.spaces.active.region_3d


def frame_bounds(context, bounds, *, top=False):
    from mathutils import Vector, Quaternion
    region, view = _view_region(context)
    center, distance = frame_parameters(bounds, max(1, region.width)/max(1, region.height))
    view.view_location = Vector(center)
    view.view_distance = distance
    if view.view_perspective == 'CAMERA':
        view.view_perspective = 'PERSP'
    if top:
        view.view_rotation = Quaternion((1, 0, 0, 0))
        view.view_perspective = 'ORTHO'
    context.area.tag_redraw()
    return center, distance


def set_oblique_view(context):
    from mathutils import Euler
    _region, view = _view_region(context)
    view.view_rotation = Euler((math.radians(60), 0, math.radians(45)), 'XYZ').to_quaternion()
    view.view_perspective = 'PERSP'
    context.area.tag_redraw()


def _map_points(scene, owner):
    nodes, authored, previews = [], [], []
    cores=[obj for obj in scene.objects if obj.get('wz09_region_kind')=='map_core'
           and _same_object(resolve_owner_map(obj),owner)]
    if cores:
        return [tuple(obj.matrix_world @ vertex.co) for obj in cores for vertex in obj.data.vertices]
    for obj in scene.objects:
        if not _same_object(resolve_owner_map(obj), owner):
            continue
        if obj.get('wz09_type') == 'nav_node':
            nodes.append(tuple(obj.matrix_world.translation))
        elif obj.get('wz09_preview_only') and obj.type == 'MESH' and not obj.hide_get():
            previews.append(obj)
        elif object_category(obj) in {'marker', 'geometry'}:
            authored.append(tuple(obj.matrix_world.translation))
    # The real waypoint region is useful; distant skybox/backdrop is not a map bound.
    if nodes:
        return nodes
    if authored:
        return authored + [tuple(owner.matrix_world.translation)]
    if previews:
        from mathutils import Vector
        return [tuple(obj.matrix_world @ Vector(point)) for obj in previews for point in obj.bound_box]
    return [tuple(owner.matrix_world.translation)]


def _map_items(_self, context):
    global _map_items_cache
    try:
        rows = map_overview_items(context.scene) if context else []
        # Blender retains dynamic enum string pointers. Keep every returned
        # string alive for the module lifetime, including renamed display labels.
        def keep(value):
            return _enum_strings.setdefault(value, value)
        _map_items_cache = [tuple(keep(value) for value in row) for row in rows]
    except ValueError:
        _map_items_cache = []
    return _map_items_cache or [('NONE', 'No map instances', 'Add or import a map first')]


def get_classes():
    global _classes
    if _classes:
        return _classes
    import bpy
    from bpy.props import EnumProperty

    class WZ09_OT_focus_map(bpy.types.Operator):
        bl_idname = 'wz09.focus_map'
        bl_label = 'Select Map Root'
        bl_description = 'Select the map chosen in the overview, or the map owning the active object'
        source: EnumProperty(items=[('CHILD', 'Active object owner', ''), ('OVERVIEW', 'Overview map', '')], default='CHILD')
        def execute(self, context):
            try:
                owner = (find_map(context.scene, context.scene.wz09_focus_map_id) if self.source == 'OVERVIEW'
                         else resolve_owner_map(context.active_object))
                if owner is None:
                    raise ValueError('Select a map or a map child first')
                if owner.hide_get(view_layer=context.view_layer):
                    owner.hide_set(False, view_layer=context.view_layer)
                if owner.hide_select:
                    raise ValueError('The map root is selection-locked; unlock it in the Outliner')
                for obj in context.selected_objects:
                    obj.select_set(False)
                owner.select_set(True)
                context.view_layer.objects.active = owner
                context.scene.wz09_focus_map_id = owner['wz09_id']
                return {'FINISHED'}
            except (ValueError, RuntimeError) as exc:
                self.report({'ERROR'}, str(exc)[:220]); return {'CANCELLED'}

    class WZ09_OT_frame_editor(bpy.types.Operator):
        bl_idname = 'wz09.frame_editor'
        bl_label = 'Frame Editor Area'
        bl_description = 'Frame map, explicit work area or selection without changing authored objects'
        target: EnumProperty(items=[('MAP', 'Active map', ''), ('WORK', 'Work area top view', ''),
                                    ('SELECTED', 'Selection', ''), ('OBLIQUE', '3D oblique view', '')], default='MAP')
        def execute(self, context):
            try:
                region, _ = _view_region(context)
                if self.target == 'OBLIQUE':
                    set_oblique_view(context)
                    return {'FINISHED'}
                if self.target == 'SELECTED':
                    if not context.selected_objects:
                        raise ValueError('Select one or more objects first')
                    targets = selection_frame_targets(context.scene, context.selected_objects, context.view_layer, context.space_data)
                    if targets is not None:
                        from mathutils import Vector
                        points = []
                        for obj in targets:
                            if obj.type in {'MESH', 'CURVE', 'SURFACE', 'FONT', 'META'}:
                                points.extend(tuple(obj.matrix_world @ Vector(corner)) for corner in obj.bound_box)
                            else:
                                points.append(tuple(obj.matrix_world.translation))
                        frame_bounds(context, bounds_from_points(points, margin=.25))
                        return {'FINISHED'}
                    with context.temp_override(region=region):
                        result = bpy.ops.view3d.view_selected(use_all_regions=False)
                    return result
                if self.target == 'WORK':
                    if not context.scene.wz09_use_work_bounds:
                        raise ValueError('Define the work area from selected nav nodes first')
                    x0, y0, x1, y1 = context.scene.wz09_work_bounds
                    points = [tuple(obj.matrix_world.translation) for obj in context.scene.objects
                              if obj.get('wz09_type') == 'nav_node']
                    z = sum(p[2] for p in points)/len(points) if points else context.scene.cursor.location.z
                    bounds = ([x0, y0, z], [x1, y1, z])
                    frame_bounds(context, bounds, top=True)
                else:
                    owner = resolve_owner_map(context.active_object)
                    if owner is None:
                        owner = find_map(context.scene, context.scene.wz09_focus_map_id)
                    frame_bounds(context, bounds_from_points(_map_points(context.scene, owner), margin=2))
                return {'FINISHED'}
            except (ValueError, RuntimeError) as exc:
                self.report({'ERROR'}, str(exc)[:220]); return {'CANCELLED'}

    class WZ09_OT_filter_editor(bpy.types.Operator):
        bl_idname = 'wz09.filter_editor'
        bl_label = 'Toggle Editor Layer'
        bl_description = 'Hide this object category in this view layer; click again to restore its previous visibility'
        bl_options = {'REGISTER', 'UNDO'}
        category: EnumProperty(items=[(key, key.title(), '') for key in CATEGORIES])
        def execute(self, context):
            try:
                result = toggle_visibility(context, self.category)
                self.report({'INFO'}, f'{result["category"].title()}: {"visibility restored" if result["restored"] else "hidden in this view layer"}')
                return {'FINISHED'}
            except (ValueError, RuntimeError) as exc:
                self.report({'ERROR'}, str(exc)[:220]); return {'CANCELLED'}

    class WZ09_OT_preview_lock(bpy.types.Operator):
        bl_idname = 'wz09.preview_selection_lock'
        bl_label = 'Lock Source Selection'
        bl_description = 'Prevent accidental source selection; click again to restore previous selection locks'
        bl_options = {'REGISTER', 'UNDO'}
        def execute(self, context):
            try:
                result = toggle_preview_lock(context)
                self.report({'INFO'}, 'Source previews selection-locked' if result['locked'] else 'Previous source selection locks restored')
                return {'FINISHED'}
            except (ValueError, RuntimeError) as exc:
                self.report({'ERROR'}, str(exc)[:220]); return {'CANCELLED'}

    class WZ09_OT_clean_view(bpy.types.Operator):
        bl_idname = 'wz09.clean_editor_view'
        bl_label = 'Clean Editor View'
        bl_description = 'Remove parent relationship lines and show textured solid preview; markers remain visible'
        def execute(self, context):
            try:
                _view_region(context)
                clean_view_settings(context.space_data)
                context.area.tag_redraw()
                self.report({'INFO'}, 'Textured solid view; parent lines hidden, marker extras visible')
                return {'FINISHED'}
            except (ValueError, RuntimeError) as exc:
                self.report({'ERROR'}, str(exc)[:220]); return {'CANCELLED'}

    class WZ09_OT_place_surface(bpy.types.Operator):
        bl_idname = 'wz09.place_surface'
        bl_label = 'Place on Surface'
        bl_description = 'Click visible geometry to place the chosen marker or a nav node; Esc cancels; middle mouse navigates'
        bl_options = {'REGISTER', 'UNDO'}
        kind: EnumProperty(items=[('MARKER', 'Gameplay marker', ''), ('NAV', 'Nav node', '')], default='MARKER')
        _owner_id = None
        _window = None
        _area = None
        _cancelled = False

        def _finish(self, context):
            global _active_placement
            self._cancelled = True
            try:
                if self._area:
                    self._area.header_text_set(None)
                if self._window:
                    self._window.cursor_modal_restore()
                    self._window.workspace.status_text_set(None)
            except (ReferenceError, RuntimeError):
                pass
            _active_placement = None

        def cancel(self, context):
            self._finish(context)

        def invoke(self, context, _event):
            global _active_placement
            try:
                _view_region(context)
                if context.mode != 'OBJECT':
                    raise ValueError('Switch to Object Mode before placing editor markers')
                if _active_placement is not None:
                    raise ValueError('A placement tool is already active; finish or press Esc first')
                owner = resolve_owner_map(context.active_object)
                self._owner_id = str(owner['wz09_id']) if owner is not None else None
                self._area, self._window = context.area, context.window
                self._cancelled = False
                _active_placement = self
                self._window.cursor_modal_set('CROSSHAIR')
                label = context.scene.wz09_marker_choice if self.kind == 'MARKER' else 'nav node'
                help_text = f'Place {label}: left click surface · middle mouse orbit · wheel zoom · Esc/right click cancel'
                self._area.header_text_set(help_text)
                context.workspace.status_text_set(help_text)
                context.window_manager.modal_handler_add(self)
                return {'RUNNING_MODAL'}
            except (ValueError, RuntimeError) as exc:
                self.report({'ERROR'}, str(exc)[:220]); return {'CANCELLED'}

        def modal(self, context, event):
            if self._cancelled:
                return {'CANCELLED'}
            if event.type in {'ESC', 'RIGHTMOUSE'} and event.value == 'PRESS':
                self._finish(context); return {'CANCELLED'}
            if context.area != self._area or context.window != self._window:
                self._finish(context); return {'CANCELLED'}
            if event.type != 'LEFTMOUSE' or event.value != 'PRESS':
                return {'PASS_THROUGH'}
            region, view = _view_region(context)
            x, y = event.mouse_x-region.x, event.mouse_y-region.y
            if not (0 <= x < region.width and 0 <= y < region.height):
                return {'PASS_THROUGH'}
            if any(item.type in {'UI', 'TOOLS', 'HEADER', 'TOOL_HEADER', 'HUD'}
                   and item.x <= event.mouse_x < item.x+item.width
                   and item.y <= event.mouse_y < item.y+item.height for item in self._area.regions):
                return {'PASS_THROUGH'}
            old_cursor = context.scene.cursor.location.copy()
            old_active = context.view_layer.objects.active
            try:
                from mathutils import Vector
                from bpy_extras import view3d_utils
                origin = view3d_utils.region_2d_to_origin_3d(region, view, (x, y))
                direction = view3d_utils.region_2d_to_vector_3d(region, view, (x, y)).normalized()
                position, normal = None, None
                depsgraph = context.evaluated_depsgraph_get()
                ray_origin = origin.copy()
                for _ in range(64):
                    hit, location, hit_normal, _index, obj, _matrix = context.scene.ray_cast(depsgraph, ray_origin, direction)
                    if not hit:
                        break
                    original = getattr(obj, 'original', obj)
                    if not original.get('wz09_editor_overlay') and not original.hide_get(view_layer=context.view_layer):
                        position, normal = location, hit_normal; break
                    ray_origin = location+direction*.0001
                if position is None:
                    if not context.scene.wz09_placement_ground:
                        raise ValueError('No visible surface under cursor; aim at geometry or explicitly enable the ground plane')
                    position = Vector(ground_intersection(origin, direction, context.scene.wz09_placement_ground_z))
                    normal = Vector((0, 0, 1))
                context.scene.cursor.location = offset_position(position, normal, context.scene.wz09_placement_offset)
                context.view_layer.objects.active = find_map(context.scene, self._owner_id) if self._owner_id else None
                with context.temp_override(region=region):
                    result = bpy.ops.wz09.add_marker('EXEC_DEFAULT') if self.kind == 'MARKER' else bpy.ops.wz09.add_nav('EXEC_DEFAULT')
                if 'FINISHED' not in result:
                    raise ValueError('Existing authoring operator cancelled the placement')
                self._finish(context)
                self.report({'INFO'}, 'Placed on visible surface; collision remains unverified')
                return {'FINISHED'}
            except (ValueError, RuntimeError, ReferenceError) as exc:
                context.view_layer.objects.active = old_active
                self.report({'WARNING'}, str(exc)[:220])
                return {'RUNNING_MODAL'}
            finally:
                context.scene.cursor.location = old_cursor

    _classes = (WZ09_OT_focus_map, WZ09_OT_frame_editor, WZ09_OT_filter_editor,
                WZ09_OT_preview_lock, WZ09_OT_clean_view, WZ09_OT_place_surface)
    return _classes


def draw_quick_tools(context, layout):
    """Root panel entry point; order follows choose → frame → place → edit."""
    scene = context.scene
    box = layout.box()
    box.label(text='Map & View', icon=icon('WORLD', 'SCENE_DATA'))
    try:
        filter_state = _snapshot(scene, 'wz09_visibility_snapshot_json')
        locked = bool(_snapshot(scene, 'wz09_preview_lock_snapshot_json'))
    except ValueError:
        filter_state, locked = {}, False
        box.label(text='Stored viewport state needs repair', icon=icon('ERROR'))
    row = box.row(align=True)
    row.prop(scene, 'wz09_focus_map_id', text='')
    row.operator('wz09.focus_map', text='', icon=icon('RESTRICT_SELECT_OFF')).source = 'OVERVIEW'
    row = box.row(align=True)
    for target, label, names in (('MAP', 'Map', ('VIEWZOOM',)), ('WORK', 'Top / Work', ('AXIS_TOP', 'VIEW_ORTHO')),
                                  ('SELECTED', 'Selection', ('ZOOM_SELECTED', 'VIEWZOOM'))):
        row.operator('wz09.frame_editor', text=label, icon=icon(*names)).target = target
    row = box.row(align=True)
    row.operator('wz09.focus_map', text='Map Root', icon=icon('OUTLINER_OB_EMPTY')).source = 'CHILD'
    row.operator('wz09.clean_editor_view', text='Clean View', icon=icon('SHADING_TEXTURE', 'SHADING_SOLID'))
    row.operator('wz09.frame_editor', text='3D', icon=icon('VIEW_PERSPECTIVE', 'VIEW3D')).target = 'OBLIQUE'
    grid = box.grid_flow(row_major=True, columns=2, even_columns=True, align=True)
    for category, label in (('preview', 'Visuals'), ('nav', 'Nav'), ('marker', 'Markers'), ('geometry', 'Own Mesh')):
        state = filter_state.get(context.view_layer.name, {})
        hidden = category in state if isinstance(state, dict) else False
        op = grid.operator('wz09.filter_editor', text=label,
                          icon=icon('HIDE_ON' if hidden else 'HIDE_OFF'), depress=hidden)
        op.category = category
    box.operator('wz09.preview_selection_lock', text='Unlock Source Selection' if locked else 'Lock Source Selection',
                 icon=icon('LOCKED' if locked else 'UNLOCKED'), depress=locked)
    if getattr(scene, 'wz09_workspace', 'PLACE') in {'PLACE', 'NAV'}:
        box = layout.box()
        box.label(text='Click to Place', icon=icon('CURSOR'))
        if getattr(scene, 'wz09_workspace', 'PLACE') != 'NAV':
            box.prop(scene, 'wz09_marker_choice', text='Marker')
        row = box.row(align=True)
        if getattr(scene, 'wz09_workspace', 'PLACE') != 'NAV':
            row.operator('wz09.place_surface', text='Place Marker', icon=icon('ADD')).kind = 'MARKER'
        row.operator('wz09.place_surface', text='Place Nav', icon=icon('TRACKING', 'EMPTY_AXIS')).kind = 'NAV'
        box.prop(scene, 'wz09_placement_offset', text='Surface Offset')
        box.prop(scene, 'wz09_placement_ground')
        if scene.wz09_placement_ground:
            box.prop(scene, 'wz09_placement_ground_z', text='Ground Z')
    layout.prop(scene, 'wz09_show_control_help', text='Controls', icon=icon('QUESTION'))
    if scene.wz09_show_control_help:
        box = layout.box()
        for text in ('MMB orbit · Shift MMB pan · wheel zoom', 'G move · R rotate · X/Y/Z constrain',
                     'Shift RMB: 3D cursor · Numpad .: frame', 'Shift click: select multiple · Ctrl Z: undo',
                     'Place: LMB surface · Esc/RMB cancel', 'Links: active selected node is source'):
            box.label(text=text)


def register():
    import bpy
    from bpy.props import BoolProperty, EnumProperty, FloatProperty
    bpy.types.Scene.wz09_focus_map_id = EnumProperty(name='Map overview', items=_map_items)
    bpy.types.Scene.wz09_placement_ground = BoolProperty(name='Use explicit ground plane on miss', default=False)
    bpy.types.Scene.wz09_placement_ground_z = FloatProperty(name='Placement ground Z (m)', default=0)
    bpy.types.Scene.wz09_placement_offset = FloatProperty(name='Surface offset (m)', default=0, min=-10, max=10)
    bpy.types.Scene.wz09_show_control_help = BoolProperty(name='Show controls', default=False)
    for cls in get_classes():
        bpy.utils.register_class(cls)


def unregister():
    global _icons
    import bpy
    if _active_placement is not None:
        _active_placement.cancel(bpy.context)
    for cls in reversed(get_classes()):
        bpy.utils.unregister_class(cls)
    for name in PROPERTIES:
        if hasattr(bpy.types.Scene, name):
            delattr(bpy.types.Scene, name)
    _icons = None
