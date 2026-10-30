"""Navigation authoring UI and a nonpersistent GPU overlay.

Pure helpers are importable without bpy. Stored UUID edges remain authoritative;
the viewport creates no objects, route checks are topology only, and condition
strings are never executed. Blender registration is explicit and reversible.
"""
import copy
import json
import math
import textwrap
import importlib.util
from pathlib import Path

try:
    from .nav_format import reachable
except ImportError:
    _spec = importlib.util.spec_from_file_location('_wz09_workflow_nav_format', Path(__file__).with_name('nav_format.py'))
    _nav_format = importlib.util.module_from_spec(_spec)
    _spec.loader.exec_module(_nav_format)
    reachable = _nav_format.reachable

EDGE_KINDS = ('walk', 'drop', 'climb', 'jump', 'mantle', 'break_glass')
_handler = None
_classes = ()
_shader = None
_overlay_error = ''
_PROPERTIES = ('wz09_nav_overlay', 'wz09_nav_overlay_selected_only',
               'wz09_nav_edit_kind', 'wz09_nav_edit_enabled', 'wz09_nav_edit_condition',
               'wz09_nav_node_kind', 'wz09_nav_is_portal', 'wz09_show_preflight_details', 'wz09_nav_xray')


def _json(raw, expected, field):
    try:
        value = json.loads(str(raw), parse_constant=lambda token: (_ for _ in ()).throw(ValueError(token)))
    except (ValueError, TypeError) as exc:
        raise ValueError(f'{field}: invalid JSON') from exc
    if not isinstance(value, expected):
        raise ValueError(f'{field}: expected {expected.__name__}')
    return value


def node_lookup(scene):
    result = {}
    for obj in scene.objects:
        if obj.get('wz09_type') != 'nav_node':
            continue
        uid = obj.get('wz09_id')
        if not isinstance(uid, str) or not uid.strip() or uid in result:
            raise ValueError('Navigation contains missing/duplicate node IDs; repair before editing')
        result[uid] = obj
    return result


def _same_object(a, b):
    if a is b:
        return True
    return callable(getattr(a, 'as_pointer', None)) and callable(getattr(b, 'as_pointer', None)) and a.as_pointer() == b.as_pointer()


def read_edges(scene):
    edges = _json(scene.get('wz09_edges_json', '[]'), list, 'wz09_edges_json')
    ids, pairs = set(), set()
    for edge in edges:
        if not isinstance(edge, dict) or any(not isinstance(edge.get(key), str) or not edge[key].strip()
                                             for key in ('id', 'from', 'to', 'kind', 'status')):
            raise ValueError('Every stored link needs id/from/to/kind/status')
        if edge['id'] in ids or (edge['from'], edge['to']) in pairs:
            raise ValueError('Duplicate stored link ID or directed endpoint pair')
        if edge['kind'] not in EDGE_KINDS:
            raise ValueError('Unsupported stored link kind')
        if 'enabled' in edge and not isinstance(edge['enabled'], bool):
            raise ValueError('Link enabled must be boolean')
        if 'condition' in edge and not isinstance(edge['condition'], str):
            raise ValueError('Link condition must be a string, never executable code')
        ids.add(edge['id'])
        pairs.add((edge['from'], edge['to']))
    return edges


def selected_pair(context):
    nodes = [obj for obj in context.selected_objects if obj.get('wz09_type') == 'nav_node']
    if len(nodes) != 2 or context.active_object is None:
        raise ValueError('Select exactly two nav nodes; make the source node active')
    active = context.active_object.get('wz09_id')
    if context.active_object.get('wz09_type') != 'nav_node' or active not in [obj.get('wz09_id') for obj in nodes]:
        raise ValueError('The active nav node is the source of the directed link')
    lookup = node_lookup(context.scene)
    if any(obj.get('wz09_id') not in lookup or not _same_object(obj, lookup[obj['wz09_id']]) for obj in nodes):
        raise ValueError('Selected nav nodes must belong to the active scene')
    nodes.sort(key=lambda obj: (obj.get('wz09_id') != active, obj.get('wz09_id')))
    return nodes[0], nodes[1]


def inspect_selected_links(context):
    a, b = selected_pair(context)
    source, target = a['wz09_id'], b['wz09_id']
    edges = read_edges(context.scene)
    lookup = node_lookup(context.scene)
    if any(edge['from'] not in lookup or edge['to'] not in lookup for edge in edges):
        raise ValueError('Repair orphan nav links before inspecting routes')
    unconditional = [edge for edge in edges if not edge.get('condition', '').strip()]
    return {'source': source, 'target': target,
            'forward': next((copy.deepcopy(edge) for edge in edges
                             if (edge['from'], edge['to']) == (source, target)), None),
            'reverse': next((copy.deepcopy(edge) for edge in edges
                             if (edge['from'], edge['to']) == (target, source)), None),
            'forward_reachable': reachable(source, target, edges),
            'reverse_reachable': reachable(target, source, edges),
            'forward_unconditional': reachable(source, target, unconditional),
            'reverse_unconditional': reachable(target, source, unconditional),
            'topology_only': True, 'conditions_evaluated': False, 'collision_verified': False}


def update_selected_links(context, *, kind=None, enabled=None, condition=None, bidirectional=False):
    """Edit existing edges, retaining IDs and unrelated metadata.

    A meaningful content edit invalidates previous evidence and resets status to
    proposed. No-op edits preserve all fields byte-for-byte after JSON decoding.
    """
    a, b = selected_pair(context)
    if kind is not None and kind not in EDGE_KINDS:
        raise ValueError('Select a supported link kind')
    if enabled is not None and not isinstance(enabled, bool):
        raise ValueError('Link enabled must be boolean')
    if condition is not None and not isinstance(condition, str):
        raise ValueError('Link condition must be text; it is never evaluated by the editor')
    if condition is not None and len(condition) > 512:
        raise ValueError('Link condition must not exceed 512 characters')
    edges = read_edges(context.scene)
    pairs = {(a['wz09_id'], b['wz09_id'])}
    if bidirectional:
        pairs.add((b['wz09_id'], a['wz09_id']))
    selected = [edge for edge in edges if (edge['from'], edge['to']) in pairs]
    if not selected:
        raise ValueError('No link exists in the selected direction; add it first')
    changed = invalidated = 0
    for edge in selected:
        values = {'kind': kind, 'enabled': enabled, 'condition': condition}
        patch = {key: value for key, value in values.items() if value is not None and
                 value != edge.get(key, True if key == 'enabled' else '' if key == 'condition' else None)}
        if patch:
            if edge['status'] != 'proposed' or 'evidence' in edge:
                invalidated += 1
            edge.update(patch)
            edge['status'] = 'proposed'
            edge.pop('evidence', None)
            changed += 1
    if changed:
        context.scene['wz09_edges_json'] = json.dumps(edges, sort_keys=True, allow_nan=False)
    return {'matched': len(selected), 'changed': changed, 'evidence_invalidated': invalidated,
            'ids': [edge['id'] for edge in selected]}


def update_node(context, *, kind, is_portal):
    node = context.active_object
    if node is None or node.get('wz09_type') != 'nav_node':
        raise ValueError('Select an active nav node')
    lookup = node_lookup(context.scene)
    if node.get('wz09_id') not in lookup or not _same_object(node, lookup[node['wz09_id']]):
        raise ValueError('Active nav node must belong to this scene')
    if not isinstance(kind, str) or not kind.strip() or len(kind) > 128:
        raise ValueError('Node kind needs 1–128 characters')
    if not isinstance(is_portal, bool):
        raise ValueError('Portal flag must be boolean')
    properties = _json(node.get('wz09_nav_properties_json', '{}'), dict, 'wz09_nav_properties_json')
    properties['is_portal'] = is_portal
    encoded = json.dumps(properties, sort_keys=True, allow_nan=False)
    node['wz09_nav_kind'] = kind.strip()
    node['wz09_nav_properties_json'] = encoded
    return node['wz09_id']


def overlay_segments(scene, selected_ids=(), selected_only=False, hidden_ids=()):
    """Build transient line/arrow coordinates, never Blender objects or IDs."""
    nodes = node_lookup(scene)
    groups = {key: [] for key in ('proposed', 'checked', 'conditional', 'disabled', 'selected')}
    selected_ids = set(selected_ids)
    hidden_ids = set(hidden_ids)
    for edge in read_edges(scene):
        if hidden_ids.intersection((edge['from'], edge['to'])):
            continue
        if edge['from'] not in nodes or edge['to'] not in nodes:
            continue  # Preflight/explicit orphan repair remain authoritative.
        highlighted = bool(selected_ids.intersection((edge['from'], edge['to'])))
        if selected_only and not highlighted:
            continue
        a = tuple(float(v) for v in nodes[edge['from']].matrix_world.translation)
        b = tuple(float(v) for v in nodes[edge['to']].matrix_world.translation)
        if not all(math.isfinite(v) for v in a+b):
            continue
        delta = tuple(y-x for x, y in zip(a, b))
        length = math.sqrt(sum(v*v for v in delta))
        if length < 1e-8:
            continue
        direction = tuple(v/length for v in delta)
        side = (-direction[1], direction[0], 0)
        side_length = math.sqrt(sum(v*v for v in side))
        if side_length < 1e-6:
            side = (1, 0, 0)
        else:
            side = tuple(v/side_length for v in side)
        arrow_size = min(0.55, length*0.2)
        tip = tuple(b[i]-direction[i]*min(0.22, length*0.08) for i in range(3))
        left = tuple(tip[i]-direction[i]*arrow_size+side[i]*arrow_size*0.45 for i in range(3))
        right = tuple(tip[i]-direction[i]*arrow_size-side[i]*arrow_size*0.45 for i in range(3))
        category = ('disabled' if edge.get('enabled', True) is False else
                    'conditional' if edge.get('condition', '').strip() else
                    'selected' if highlighted else
                    'proposed' if edge['status'] == 'proposed' else 'checked')
        groups[category].extend((a, b, tip, left, tip, right))
    return groups


def format_preflight_report(result):
    return ('WZ2009 OFFLINE AUTHORING REPORT\n'
            'Graph topology and static evidence only; no collision or game tests.\n\n' +
            json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False) + '\n')


def _redraw(_owner=None, _context=None):
    import bpy
    manager = getattr(bpy.context, 'window_manager', None)
    for window in getattr(manager, 'windows', ()):
        for area in window.screen.areas:
            if area.type == 'VIEW_3D':
                area.tag_redraw()


def _draw_overlay():
    import bpy
    global _overlay_error
    scene = bpy.context.scene
    if not scene or not getattr(scene, 'wz09_nav_overlay', False):
        return
    try:
        # An empty link graph draws nothing. Avoid two full object scans on
        # large authoring scenes merely to discover there are no lines.
        if not read_edges(scene):
            _overlay_error = ''
            return
        import gpu
        from gpu_extras.batch import batch_for_shader
        global _shader
        if _shader is None:
            _shader = gpu.shader.from_builtin('UNIFORM_COLOR')
        selected = [obj.get('wz09_id') for obj in bpy.context.selected_objects if obj.get('wz09_type') == 'nav_node']
        hidden = [obj.get('wz09_id') for obj in scene.objects
                  if obj.get('wz09_type') == 'nav_node' and not obj.visible_get(view_layer=bpy.context.view_layer)]
        groups = overlay_segments(scene, selected, scene.wz09_nav_overlay_selected_only, hidden)
        colors = {'proposed': (0.12, 0.68, 0.95, 0.82), 'checked': (0.30, 0.84, 0.50, 0.85),
                  'conditional': (1.0, 0.60, 0.10, 0.95), 'disabled': (0.45, 0.45, 0.45, 0.48),
                  'selected': (1.0, 0.94, 0.25, 1.0)}
        state = (gpu.state.blend_get(), gpu.state.depth_test_get(), gpu.state.line_width_get())
        try:
            gpu.state.blend_set('ALPHA')
            gpu.state.depth_test_set('NONE' if scene.wz09_nav_xray else 'LESS_EQUAL')
            gpu.state.line_width_set(1.5)
            for key, points in groups.items():
                if points:
                    batch = batch_for_shader(_shader, 'LINES', {'pos': points})
                    _shader.bind()
                    _shader.uniform_float('color', colors[key])
                    batch.draw(_shader)
        finally:
            gpu.state.blend_set(state[0])
            gpu.state.depth_test_set(state[1])
            gpu.state.line_width_set(state[2])
        _overlay_error = ''
    except (ValueError, TypeError, KeyError, RuntimeError, ReferenceError, AttributeError) as exc:
        # A malformed graph is reported by Preflight. Drawing must not corrupt
        # authoring state or flood the console on every viewport redraw.
        _overlay_error = str(exc)[:180]
        return


def get_classes():
    global _classes
    if _classes:
        return _classes
    import bpy

    class WZ09_OT_nav_inspect(bpy.types.Operator):
        bl_idname = 'wz09.inspect_nav_link'
        bl_label = 'Read Selected Directed Link'
        def execute(self, context):
            try:
                info = inspect_selected_links(context)
                edge = info['forward']
                if edge is None:
                    raise ValueError('No active-source → target link exists; add it first')
                context.scene.wz09_nav_edit_kind = edge['kind']
                context.scene.wz09_nav_edit_enabled = edge.get('enabled', True)
                context.scene.wz09_nav_edit_condition = edge.get('condition', '')
                return {'FINISHED'}
            except ValueError as exc:
                self.report({'ERROR'}, str(exc)[:220]); return {'CANCELLED'}

    class WZ09_OT_nav_update(bpy.types.Operator):
        bl_idname = 'wz09.update_nav_link'
        bl_label = 'Apply Existing Link Changes'
        bl_options = {'REGISTER', 'UNDO'}
        def execute(self, context):
            try:
                scene = context.scene
                result = update_selected_links(context, kind=scene.wz09_nav_edit_kind,
                    enabled=scene.wz09_nav_edit_enabled, condition=scene.wz09_nav_edit_condition,
                    bidirectional=getattr(scene, 'wz09_bidirectional', False))
                self.report({'WARNING'} if result['evidence_invalidated'] else {'INFO'},
                    f'{result["changed"]} links changed; UUIDs preserved. Changed links remain proposed.')
                _redraw(); return {'FINISHED'}
            except ValueError as exc:
                self.report({'ERROR'}, str(exc)[:220]); return {'CANCELLED'}

    class WZ09_OT_nav_route(bpy.types.Operator):
        bl_idname = 'wz09.check_nav_routes'
        bl_label = 'Check Directed Graph Routes'
        def execute(self, context):
            try:
                info = inspect_selected_links(context)
                context.scene['wz09_route_check_json'] = json.dumps(info, sort_keys=True)
                def label(key):
                    if not info[key + '_reachable']: return 'none'
                    return 'unconditional' if info[key + '_unconditional'] else 'conditional'
                self.report({'INFO'}, f'Graph forward: {label("forward")}; return: {label("reverse")}. No collision test.')
                return {'FINISHED'}
            except ValueError as exc:
                self.report({'ERROR'}, str(exc)[:220]); return {'CANCELLED'}

    class WZ09_OT_nav_read_node(bpy.types.Operator):
        bl_idname = 'wz09.read_nav_node'
        bl_label = 'Read Active Node'
        def execute(self, context):
            try:
                node = context.active_object
                if node is None or node.get('wz09_type') != 'nav_node':
                    raise ValueError('Select an active nav node')
                properties = _json(node.get('wz09_nav_properties_json', '{}'), dict, 'wz09_nav_properties_json')
                context.scene.wz09_nav_node_kind = str(node.get('wz09_nav_kind', 'walk'))
                context.scene.wz09_nav_is_portal = properties.get('is_portal') is True
                return {'FINISHED'}
            except ValueError as exc:
                self.report({'ERROR'}, str(exc)[:220]); return {'CANCELLED'}

    class WZ09_OT_nav_update_node(bpy.types.Operator):
        bl_idname = 'wz09.update_nav_node'
        bl_label = 'Apply Node / Portal Properties'
        bl_options = {'REGISTER', 'UNDO'}
        def execute(self, context):
            try:
                update_node(context, kind=context.scene.wz09_nav_node_kind,
                            is_portal=context.scene.wz09_nav_is_portal)
                self.report({'INFO'}, 'Node metadata updated; original CSV metadata preserved. Portal is declarative.')
                return {'FINISHED'}
            except ValueError as exc:
                self.report({'ERROR'}, str(exc)[:220]); return {'CANCELLED'}

    class WZ09_OT_preflight_text(bpy.types.Operator):
        bl_idname = 'wz09.show_preflight_report'
        bl_label = 'Create Complete Report Text'
        def execute(self, context):
            try:
                result = _json(context.scene.get('wz09_preflight_json', '{}'), dict, 'wz09_preflight_json')
                if not result:
                    raise ValueError('Run Validate World (Offline) first')
                text = bpy.data.texts.new('WZ2009 Offline Report')
                text.write(format_preflight_report(result))
                context.scene['wz09_preflight_text_name'] = text.name
                self.report({'INFO'}, 'Full report available in Blender Text Editor: ' + text.name)
                return {'FINISHED'}
            except ValueError as exc:
                self.report({'ERROR'}, str(exc)[:220]); return {'CANCELLED'}

    class WZ09_PT_nav_workflow(bpy.types.Panel):
        bl_label = 'Navigation Inspector / Diagnostics'
        bl_idname = 'WZ09_PT_nav_workflow'
        bl_parent_id = 'WZ09_PT_main'
        bl_space_type = 'VIEW_3D'; bl_region_type = 'UI'; bl_category = 'WZ2009'
        @classmethod
        def poll(cls, context):
            return getattr(context.scene, 'wz09_workspace', 'NAV') in {'NAV', 'EXPORT'}
        def draw(self, context):
            scene, layout = context.scene, self.layout
            layout.prop(scene, 'wz09_nav_overlay')
            if scene.wz09_nav_overlay:
                layout.prop(scene, 'wz09_nav_xray')
                layout.prop(scene, 'wz09_nav_overlay_selected_only')
                layout.label(text='Blue: proposed · amber: conditional')
                layout.label(text='Gray: disabled · yellow: selected')
                if _overlay_error:layout.label(text=_overlay_error, icon='ERROR')
            selected = context.active_object
            if selected is not None and selected.get('wz09_type') == 'nav_node':
                box = layout.box()
                box.label(text='Node ID: ' + str(selected.get('wz09_id', '')))
                box.label(text='Owner: ' + str(selected.get('wz09_owner_map', '') or '(world)'))
                box.label(text='Source: ' + str(selected.get('wz09_nav_source', 'manual')))
                box.operator('wz09.read_nav_node')
                box.prop(scene, 'wz09_nav_node_kind'); box.prop(scene, 'wz09_nav_is_portal')
                box.operator('wz09.update_nav_node')
            try:
                info = inspect_selected_links(context)
            except ValueError:
                info = None
            if info:
                box = layout.box()
                for direction in ('forward', 'reverse'):
                    edge = info[direction]
                    box.label(text=direction.title() + ': ' + (edge['kind'] + ' / ' + edge['status'] if edge else 'no direct link'))
                    if edge:
                        box.label(text='ID: ' + edge['id'])
                        if edge.get('enabled', True) is False: box.label(text='Disabled: excluded from graph routes')
                        if edge.get('condition', '').strip(): box.label(text='Condition: ' + edge['condition'])
                box.operator('wz09.inspect_nav_link')
                box.prop(scene, 'wz09_nav_edit_kind'); box.prop(scene, 'wz09_nav_edit_enabled')
                box.prop(scene, 'wz09_nav_edit_condition')
                box.operator('wz09.update_nav_link')
                box.label(text='Uses Both directions setting; edited evidence is invalidated')
                box.operator('wz09.check_nav_routes')
                for direction in ('forward', 'reverse'):
                    state = 'none' if not info[direction + '_reachable'] else (
                        'unconditional' if info[direction + '_unconditional'] else 'conditional')
                    box.label(text=direction.title() + ' graph route: ' + state)
                box.label(text='Conditions unevaluated. No walkability proof.')
            for name in ('wz09_nav_max_distance_m', 'wz09_nav_max_delta_z_m'):
                if hasattr(scene, name): layout.prop(scene, name)
            layout.prop(scene, 'wz09_show_preflight_details')
            if scene.wz09_show_preflight_details:
                try:
                    result = _json(scene.get('wz09_preflight_json', '{}'), dict, 'wz09_preflight_json')
                    if result:
                        layout.label(text='Last Preflight snapshot; rerun after editing')
                        for kind in ('errors', 'warnings'):
                            layout.label(text=kind.title() + ': ' + str(result.get('stats', {}).get(kind[:-1]+'_count', len(result.get(kind, [])))))
                            for item in result.get(kind, []):
                                for line in textwrap.wrap(str(item), width=58): layout.label(text=line)
                        diagnostics = result.get('diagnostics', {})
                        layout.label(text=f'Portals: {diagnostics.get("portal_count", 0)} / disconnected: {diagnostics.get("unconnected_portal_count", 0)}')
                        for row in diagnostics.get('per_map', []):
                            layout.label(text=f'{row["source_map"]}: {row["nodes"]} nodes, {row["landmarks"]} landmarks')
                            layout.label(text='Source commit: ' + (', '.join(row['source_commits']) or 'not recorded'))
                        layout.operator('wz09.show_preflight_report')
                    else: layout.label(text='Run Validate World (Offline) first')
                except (ValueError, TypeError, KeyError): layout.label(text='Stored report is invalid; run Preflight again')

    _classes = (WZ09_OT_nav_inspect, WZ09_OT_nav_update, WZ09_OT_nav_route,
                WZ09_OT_nav_read_node, WZ09_OT_nav_update_node, WZ09_OT_preflight_text,
                WZ09_PT_nav_workflow)
    return _classes


def register():
    import bpy
    from bpy.props import BoolProperty, EnumProperty, StringProperty
    global _handler
    for cls in get_classes():
        bpy.utils.register_class(cls)
    bpy.types.Scene.wz09_nav_overlay = BoolProperty(name='Show Directed Nav Links', default=True, update=_redraw)
    bpy.types.Scene.wz09_nav_xray = BoolProperty(name='X-ray Links (through surfaces)', default=True, update=_redraw)
    bpy.types.Scene.wz09_nav_overlay_selected_only = BoolProperty(name='Only links incident to selected nodes', default=False, update=_redraw)
    bpy.types.Scene.wz09_nav_edit_kind = EnumProperty(name='Existing link kind', items=[(kind, kind, kind) for kind in EDGE_KINDS])
    bpy.types.Scene.wz09_nav_edit_enabled = BoolProperty(name='Enabled in graph topology', default=True)
    bpy.types.Scene.wz09_nav_edit_condition = StringProperty(name='Declarative condition', maxlen=512, description='Stored text only; no expression is evaluated')
    bpy.types.Scene.wz09_nav_node_kind = StringProperty(name='Node kind', default='walk', maxlen=128)
    bpy.types.Scene.wz09_nav_is_portal = BoolProperty(name='Explicit connector portal', default=False)
    bpy.types.Scene.wz09_show_preflight_details = BoolProperty(name='Show Complete Preflight Findings', default=False)
    if not bpy.app.background and _handler is None:
        _handler = bpy.types.SpaceView3D.draw_handler_add(_draw_overlay, (), 'WINDOW', 'POST_VIEW')


def unregister():
    import bpy
    global _handler, _shader
    if _handler is not None:
        bpy.types.SpaceView3D.draw_handler_remove(_handler, 'WINDOW')
        _handler = None
    _shader = None
    for name in _PROPERTIES:
        if hasattr(bpy.types.Scene, name): delattr(bpy.types.Scene, name)
    for cls in reversed(_classes):
        if hasattr(cls, 'bl_rna'): bpy.utils.unregister_class(cls)
    _redraw()
