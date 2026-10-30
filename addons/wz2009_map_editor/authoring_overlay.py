"""Focused GPU highlight of the selected audio/action boundary.

The ordinary persistent wire objects remain the complete saved overview. The
overlay touches only the active authoring root's children, so a 16-map scene
does not rebuild thousands of contour batches on every redraw.
"""
import json

_handler=None
_shader=None
_error=''
_CLASSES=[]
_FOCUS='wz09_contour_focus_snapshot_json'
_browser_cache={}
_browser_request=None


def _object_key(obj):return obj.as_pointer() if hasattr(obj,'as_pointer') else id(obj)


def _scene_key(scene):return getattr(scene,'session_uid',None) or _object_key(scene)


def _contour(obj):
    return bool((obj.get('wz09_audio_contour') or obj.get('wz09_action_contour'))
        and obj.get('wz09_preview_only') and getattr(obj,'hide_render',False)
        and not obj.get('wz09_asset_part'))


def _stable_key(obj):
    if _contour(obj):
        fields=('wz09_preview_id',)
    else:fields=('wz09_action_id','wz09_audio_id','wz09_preview_id','wz09_id')
    for field in fields:
        value=obj.get(field)
        if isinstance(value,str) and value.strip():return field+':'+value
    raise ValueError('Audio/action helper has no stable ID; inspect source/editor ID')


def _hide_operator_context(context):
    """Use a real editor region; selection-list overrides do not scope hiding."""
    window=getattr(context,'window',None)
    if window is None:raise ValueError('Outline focus requires a Blender window with a 3D viewport')
    areas=list(window.screen.areas)
    area=next((a for a in areas if a.type=='VIEW_3D' and getattr(a.spaces.active,'local_view',None) is None),None)
    if area is None:
        area=next((a for a in areas if a.type=='OUTLINER' and getattr(a.spaces.active,'display_mode','') in {'VIEW_LAYER','SCENES'}),None)
    if area is None:raise ValueError('Open a 3D viewport outside local view for outline focus')
    region=next((r for r in area.regions if r.type=='WINDOW'),None)
    if region is None:raise ValueError('Outline focus: editor region is not ready yet')
    return dict(window=window,area=area,region=region,scene=context.scene,view_layer=context.view_layer)


def _native_hide_transaction(context,objects,desired,*,bpy_module=None):
    """Apply exact Base hides in batches, restoring locks/selection atomically.

    hide_set marks the complete ViewLayer for resync on every call. Native hide
    operators synchronize once per batch, but reveal is whole-layer only. Keep
    a transient complete snapshot and reapply all unrelated hides before return.
    No global hide_viewport or collection membership is changed.
    """
    if bpy_module is None:import bpy as bpy_module
    bpy=bpy_module;layer=context.view_layer;objects=tuple(objects)
    override=_hide_operator_context(context)
    before={_object_key(obj):bool(obj.hide_get(view_layer=layer)) for obj in objects}
    wanted=dict(before);wanted.update(desired)
    selected={_object_key(obj):bool(obj.select_get(view_layer=layer)) for obj in objects}
    if any(selected[key] and wanted[key] for key in before):
        # Blender necessarily deselects hidden bases. Do not silently change a
        # user's multi-selection just to isolate one of its members.
        raise ValueError('Outline focus would hide other selected elements; select only the desired element first')
    scene_objects=tuple(context.scene.objects)
    locks=[bool(obj.hide_select) for obj in scene_objects]
    active=layer.objects.active
    sync=next((obj for obj in objects if getattr(obj,'library',None) is None
               and getattr(obj,'override_library',None) is None),None)
    if sync is None:raise ValueError('Outline focus requires local editor objects')
    unlock={key for key in before if wanted[key] or before[key] or selected[key]}
    temporary=[False if _object_key(obj) in unlock else value for obj,value in zip(scene_objects,locks)]

    def set_locks(values):
        changed=[obj for obj,value in zip(scene_objects,values) if bool(obj.hide_select)!=value]
        context.scene.objects.foreach_set('hide_select',values)
        # The raw bulk setter does not tag each Object's evaluated copy. Without
        # these linear ID tags, layer.update can reapply stale lock values from
        # every changed Object except the one receiving the scalar callback.
        for obj in changed:obj.update_tag(refresh={'OBJECT'})
        # foreach_set writes the raw RNA flags once. A single ordinary property
        # callback synchronizes BASE_SELECTABLE; layer.update alone does not.
        sync.hide_select=bool(sync.hide_select)
        layer.update()

    def apply(flags,strict=True):
        current={_object_key(obj):bool(obj.hide_get(view_layer=layer)) for obj in objects}
        reveal=any(current[key] and not flags[key] for key in current)
        if reveal:
            bpy.ops.object.hide_view_clear('EXEC_DEFAULT',False,select=False)
        for obj in objects:
            if obj.select_get(view_layer=layer):obj.select_set(False,view_layer=layer)
        targets=[obj for obj in objects if flags[_object_key(obj)] and not obj.hide_get(view_layer=layer)]
        for obj in targets:obj.select_set(True,view_layer=layer)
        if targets:bpy.ops.object.hide_view_set('EXEC_DEFAULT',False,unselected=False)
        layer.update()
        residual=[obj for obj in objects if bool(obj.hide_get(view_layer=layer))!=flags[_object_key(obj)]]
        # Globally disabled or collection-restricted bases cannot be selected by
        # native operators. Small exceptional sets use the exact public API.
        if strict and len(residual)>32:
            raise ValueError(f'Too many unselectable outlines ({len(residual)}); focus restored unchanged')
        for obj in residual:obj.hide_set(flags[_object_key(obj)],view_layer=layer)
        if residual:layer.update()

    def restore_interaction():
        set_locks(locks)
        for obj in objects:
            value=selected[_object_key(obj)]
            if bool(obj.select_get(view_layer=layer))!=value:obj.select_set(value,view_layer=layer)
        layer.objects.active=active
        if any(bool(obj.select_get(view_layer=layer))!=selected[_object_key(obj)] for obj in objects):
            raise ValueError('Outline focus could not preserve the exact selection')
        if any(bool(obj.hide_select)!=value for obj,value in zip(scene_objects,locks)):
            raise ValueError('Outline focus could not preserve selection locks exactly')

    with context.temp_override(**override):
        try:
            set_locks(temporary);apply(wanted);restore_interaction()
            if any(bool(obj.hide_get(view_layer=layer))!=wanted[_object_key(obj)] for obj in objects):
                raise ValueError('Outline focus could not set view layer visibility exactly')
        except Exception:
            # Recovery may be slower for an unsupported restricted hierarchy,
            # but it must restore every original flag rather than leave a
            # half-applied visibility transaction behind.
            set_locks(temporary);apply(before,False);restore_interaction()
            if any(bool(obj.hide_get(view_layer=layer))!=before[_object_key(obj)] for obj in objects):
                raise RuntimeError('Outline focus could not restore original visibility')
            raise


def _apply_hide_states(context,objects,candidates,flags):
    changes=[(obj,flags[key]) for key,obj in candidates.items()
             if bool(obj.hide_get(view_layer=context.view_layer))!=flags[key]]
    if not changes:return
    if any(value and callable(getattr(obj,'select_get',None)) and obj.select_get(view_layer=context.view_layer)
           for obj,value in changes):
        raise ValueError('Outline focus would hide selected elements; adjust the selection first')
    if len(changes)>32:
        _native_hide_transaction(context,objects,{_object_key(obj):value for obj,value in changes})
    else:
        for obj,value in changes:obj.hide_set(value,view_layer=context.view_layer)
        context.view_layer.update()


def toggle_contour_focus(context):
    """Hide only authoring glyphs/wires; retain exact per-view-layer hides."""
    try:from .editor_ux import _snapshot
    except ImportError:from editor_ux import _snapshot
    scene,layer=context.scene,context.view_layer;state=_snapshot(scene,_FOCUS)
    layer_key=str(layer.name);previous=state.get(layer_key);restoring=previous is not None
    if restoring and (not isinstance(previous,dict) or any(type(value) is not bool for value in previous.values())):
        raise ValueError('Saved outline visibility is invalid; nothing changed')
    selected=selected_root(context.active_object)
    if selected is None and not restoring:raise ValueError('Select a music zone, radio or action element first')
    selection={_object_key(root) for obj in getattr(context,'selected_objects',())
               if (root:=selected_root(obj)) is not None}
    if selected is not None:selection.add(_object_key(selected))
    # One explicit-operation pass. Never call Object.children per authoring root.
    objects=tuple(layer.objects);candidates={};meshes=[]
    for obj in objects:
        is_root=bool(obj.get('wz09_audio_zone') or obj.get('wz09_action_element'))
        if _contour(obj) or is_root and obj.type=='EMPTY':
            key=_stable_key(obj)
            if key in candidates:raise ValueError('Duplicate stable outline/authoring ID; filter not applied')
            candidates[key]=obj
        elif obj.type=='MESH':
            # Real mesh objects are not filter candidates, even if an action
            # profile was attached directly to one of them.
            root=selected_root(obj)
            if root is not None:meshes.append(obj)
    current={key:bool(obj.hide_get(view_layer=layer)) for key,obj in candidates.items()}
    visible_before={_object_key(obj):bool(obj.visible_get(view_layer=layer)) for obj in meshes}
    target={key:(previous.get(key,current[key]) if restoring else _object_key(selected_root(obj)) not in selection)
            for key,obj in candidates.items()}
    try:
        _apply_hide_states(context,objects,candidates,target)
        if any(bool(obj.visible_get(view_layer=layer))!=visible_before[_object_key(obj)] for obj in meshes):
            raise ValueError('Outline filter would affect a real asset mesh; restored unchanged')
    except Exception:
        _apply_hide_states(context,objects,candidates,current)
        raise
    if restoring:state.pop(layer_key,None)
    else:state[layer_key]=current
    if state:scene[_FOCUS]=json.dumps(state,sort_keys=True)
    elif _FOCUS in scene:del scene[_FOCUS]
    return {'restored':restoring,'authoring_objects':len(candidates),'render_meshes_unchanged':len(meshes)}


def refresh_browser_index(scene):
    """Transient derived UI index; validators/exporters still inspect fresh data."""
    global _browser_cache
    try:from . import audio_zones,action_elements
    except ImportError:import audio_zones,action_elements
    records={'audio':[],'action':[]};contours={};errors=[];seen={'audio':set(),'action':set()}
    for obj in scene.objects:
        if _contour(obj):
            try:
                owner=selected_root(obj)
                if owner is not None:contours.setdefault(_stable_key(owner),[]).append(obj)
            except ValueError as exc:
                if len(errors)<5:errors.append(str(exc))
        for kind,flag,field,identity,validator in (('audio','wz09_audio_zone','wz09_audio_json','wz09_audio_id',audio_zones.validate_records),
                ('action','wz09_action_element','wz09_action_json','wz09_action_id',action_elements.validate_records)):
            if not obj.get(flag):continue
            try:
                raw=json.loads(obj.get(field,''));validated=validator([raw],verify_sources=False)[0]
                uid=obj.get(identity)
                if validated['id']!=uid or uid in seen[kind]:raise ValueError('missing, conflicting or duplicate authoring ID')
                seen[kind].add(uid);records[kind].append(dict(validated,name=obj.name,_object=obj))
            except (ValueError,TypeError,KeyError) as exc:
                if len(errors)<5:errors.append(kind+' '+str(obj.get(identity,'no ID'))+': '+str(exc)[:160])
    _browser_cache={'scene_key':_scene_key(scene),'objects_count':len(scene.objects),'records':records,'contours':contours,'error':' | '.join(errors)}
    return _browser_cache


def _refresh_browser_timer():
    global _browser_request
    import bpy
    requested=_browser_request;_browser_request=None;scene=bpy.context.scene
    if scene is None or _scene_key(scene)!=requested:return None
    try:refresh_browser_index(scene)
    except (ValueError,TypeError,ReferenceError,RuntimeError) as exc:
        _browser_cache.clear();_browser_cache.update(scene_key=requested,objects_count=len(scene.objects),records={'audio':[],'action':[]},error=str(exc)[:250])
    for window in bpy.context.window_manager.windows:
        for area in window.screen.areas:
            if area.type=='VIEW_3D':area.tag_redraw()
    return None


def browser_records(scene,kind):
    """Draw accessor: no scene-object iteration, IO or source verification."""
    global _browser_request
    if kind not in {'audio','action'}:raise ValueError('Unknown authoring browser kind')
    key=_scene_key(scene)
    if _browser_cache.get('scene_key')==key:
        return _browser_cache['records'][kind],_browser_cache['error'],False
    _browser_cache.clear();_browser_request=key
    import bpy
    if not bpy.app.background and not bpy.app.timers.is_registered(_refresh_browser_timer):bpy.app.timers.register(_refresh_browser_timer,first_interval=.05)
    return [],'',True


def _clear_browser_index(*args):
    global _browser_request
    import bpy
    _browser_cache.clear();_browser_request=None
    if bpy.app.timers.is_registered(_refresh_browser_timer):bpy.app.timers.unregister(_refresh_browser_timer)


def _browser_updates(scene,depsgraph):
    if not _browser_cache:return
    # KH 2026-10-19: index is only used in Audio/Aktionen/Marker; drop it elsewhere instead of scanning
    if getattr(scene,'wz09_workspace','') not in {'AUDIO','ACTIONS','PLACE'}:_clear_browser_index();return
    if _browser_cache.get('scene_key')!=_scene_key(scene):_clear_browser_index();return
    try:
        for update in depsgraph.updates:
            obj=update.id.original
            if getattr(obj,'id_type',None)=='SCENE' or callable(getattr(obj,'get',None)) and (obj.get('wz09_audio_zone') or obj.get('wz09_action_element')):
                _clear_browser_index();return
    except ReferenceError:_clear_browser_index()


def draw_contour_focus(layout,context):
    # Static button/poll; the snapshot and 155k object scene are not read here.
    layout.operator('wz09.contour_focus',text='Selected / all outlines',icon='HIDE_OFF')


def selected_root(obj):
    seen=set()
    while obj is not None:
        key=obj.as_pointer() if hasattr(obj,'as_pointer') else id(obj)
        if key in seen:return None
        seen.add(key)
        if obj.get('wz09_audio_zone') or obj.get('wz09_action_element'):return obj
        if obj.get('wz09_type')=='map_instance':return None
        obj=getattr(obj,'parent',None)
    return None


def _draw_overlay():
    global _shader,_error
    import bpy
    scene=bpy.context.scene;space=bpy.context.space_data
    if not scene or getattr(scene,'wz09_workspace','') not in {'AUDIO','ACTIONS','PLACE'}:return
    if not getattr(getattr(space,'overlay',None),'show_overlays',False):return
    root=selected_root(bpy.context.active_object)
    if root is None or not root.visible_get(view_layer=bpy.context.view_layer):return
    try:
        import gpu
        from gpu_extras.batch import batch_for_shader
        if _shader is None:_shader=gpu.shader.from_builtin('UNIFORM_COLOR')
        state=(gpu.state.blend_get(),gpu.state.depth_test_get(),gpu.state.line_width_get())
        try:
            gpu.state.blend_set('ALPHA');gpu.state.depth_test_set('NONE');gpu.state.line_width_set(2.5)
            browser_records(scene,'audio' if root.get('wz09_audio_zone') else 'action')
            for child in _browser_cache.get('contours',{}).get(_stable_key(root),()):
                if not (child.get('wz09_audio_contour') or child.get('wz09_action_contour')):continue
                if child.type!='MESH' or not child.visible_get(view_layer=bpy.context.view_layer):continue
                points=[tuple(child.matrix_world @ child.data.vertices[i].co)
                        for edge in child.data.edges for i in edge.vertices]
                if not points:continue
                batch=batch_for_shader(_shader,'LINES',{'pos':points})
                _shader.bind();_shader.uniform_float('color',tuple(child.color));batch.draw(_shader)
        finally:
            gpu.state.blend_set(state[0]);gpu.state.depth_test_set(state[1]);gpu.state.line_width_set(state[2])
        _error=''
    except (ValueError,TypeError,RuntimeError,ReferenceError,AttributeError) as exc:
        _error=str(exc)[:180]


def register():
    global _handler
    import bpy
    class WZ09_OT_contour_focus(bpy.types.Operator):
        bl_idname='wz09.contour_focus';bl_label='Selected / all outlines';bl_options={'REGISTER','UNDO'}
        bl_description='Hide other audio/action helpers in this view layer; click again to restore individual visibility. Real meshes stay unchanged'
        @classmethod
        def poll(cls,context):return selected_root(context.active_object) is not None or bool(context.scene.get(_FOCUS))
        def execute(self,context):
            try:
                result=toggle_contour_focus(context)
                self.report({'INFO'},'Konturen: '+('vorherige Sichtbarkeit wiederhergestellt' if result['restored'] else 'only selection visible'))
                return {'FINISHED'}
            except (ValueError,TypeError,RuntimeError,ReferenceError) as exc:self.report({'ERROR'},str(exc)[:250]);return {'CANCELLED'}
    class WZ09_OT_authoring_index_refresh(bpy.types.Operator):
        bl_idname='wz09.authoring_index_refresh';bl_label='Refresh audio/action list'
        def execute(self,context):
            refresh_browser_index(context.scene);return {'FINISHED'}
    _CLASSES[:]=[WZ09_OT_contour_focus,WZ09_OT_authoring_index_refresh]
    for cls in _CLASSES:bpy.utils.register_class(cls)
    for function in (_clear_browser_index,_browser_updates):bpy.app.handlers.persistent(function)
    for name in ('load_pre','load_post','undo_pre','undo_post','redo_pre','redo_post'):
        handlers=getattr(bpy.app.handlers,name)
        if _clear_browser_index not in handlers:handlers.append(_clear_browser_index)
    if _browser_updates not in bpy.app.handlers.depsgraph_update_post:bpy.app.handlers.depsgraph_update_post.append(_browser_updates)
    if not bpy.app.background and _handler is None:
        _handler=bpy.types.SpaceView3D.draw_handler_add(_draw_overlay,(),'WINDOW','POST_VIEW')


def unregister():
    global _handler,_shader,_error
    import bpy
    _clear_browser_index()
    if _browser_updates in bpy.app.handlers.depsgraph_update_post:bpy.app.handlers.depsgraph_update_post.remove(_browser_updates)
    for name in ('load_pre','load_post','undo_pre','undo_post','redo_pre','redo_post'):
        handlers=getattr(bpy.app.handlers,name)
        if _clear_browser_index in handlers:handlers.remove(_clear_browser_index)
    for cls in reversed(_CLASSES):bpy.utils.unregister_class(cls)
    _CLASSES.clear()
    if _handler is not None:bpy.types.SpaceView3D.draw_handler_remove(_handler,'WINDOW')
    _handler=None;_shader=None;_error=''
