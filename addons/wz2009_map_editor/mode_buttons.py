'Return to world editing or switch to the Animation workspace. UI controls only; scene data is unchanged.'
import os
import bpy

_ANIM_TABS={'ANIMATION','CINEMATICS'}

def _find_workspace(names):
    for n in names:
        ws=bpy.data.workspaces.get(n)
        if ws:return ws
    return None

def _append_default(context,name):
    """Append a Blender default workspace (e.g. 'Animation') if the file lacks it."""
    try:
        bpy.ops.workspace.append_activate(idname=name,filepath=os.path.join(bpy.utils.resource_path('LOCAL'),'datafiles','startup.blend'))
        return bpy.data.workspaces.get(name)
    except (RuntimeError,TypeError,ValueError):
        return None

def _leave_edit_modes(context):
    try:
        if context.screen and context.screen.is_animation_playing:bpy.ops.screen.animation_cancel(restore_frame=False)
    except (RuntimeError,AttributeError):pass
    obj=context.view_layer.objects.active if context.view_layer else None
    if obj and obj.mode!='OBJECT':
        try:bpy.ops.object.mode_set(mode='OBJECT')
        except RuntimeError:pass

def _finish_world(window_ptr,attempts=[0]):
    win=next((w for w in bpy.context.window_manager.windows if w.as_pointer()==window_ptr),None)
    if win is None:return None
    done=False
    for area in win.screen.areas:
        if area.type!='VIEW_3D':continue
        space=area.spaces.active
        try:space.show_region_ui=True
        except AttributeError:pass
        region=next((r for r in area.regions if r.type=='WINDOW'),None)
        if region is None:continue
        try:
            with bpy.context.temp_override(window=win,area=area,region=region):
                bpy.ops.wm.tool_set_by_id(name='builtin.move')
            done=True
        except (RuntimeError,TypeError):pass
        for r in area.regions:
            if r.type=='UI':
                try:r.active_panel_category='WZ2009'
                except (AttributeError,TypeError,ValueError):pass
        break
    return None

class WZ09_OT_mode_world(bpy.types.Operator):
    bl_idname='wz09.mode_world';bl_label='Edit world'
    bl_description='Return to world view: Layout workspace, Object Mode, Move tool and WZ2009 sidebar'
    def execute(self,context):
        _leave_edit_modes(context)
        s=context.scene
        if getattr(s,'wz09_workspace','MAPS') in _ANIM_TABS:s.wz09_workspace='MAPS'
        ws=_find_workspace(('Layout','WZ2009 Editor','WZ2009'))
        if ws is None:ws=next((w for w in bpy.data.workspaces if w.name!='Animation'),None)
        if ws is not None and context.window.workspace!=ws:context.window.workspace=ws
        ptr=context.window.as_pointer()
        bpy.app.timers.register(lambda:_finish_world(ptr),first_interval=.15)
        self.report({'INFO'},'Edit world: Object Mode, G to move, left-click to select')
        return {'FINISHED'}

class WZ09_OT_mode_animation(bpy.types.Operator):
    bl_idname='wz09.mode_animation';bl_label='Animation'
    bl_description='Switch to the Animation workspace; use World to return'
    def execute(self,context):
        _leave_edit_modes(context)
        ws=_find_workspace(('Animation',)) or _append_default(context,'Animation')
        if hasattr(context.scene,'wz09_workspace'):context.scene.wz09_workspace='ANIMATION'
        if ws is not None and context.window.workspace!=ws:context.window.workspace=ws
        if ws is None:self.report({'WARNING'},'Animation workspace not found; WZ2009 Animation tab activated')
        return {'FINISHED'}

def draw_buttons(layout):
    row=layout.row(align=True);row.scale_y=1.4
    row.operator('wz09.mode_world',text='World',icon='OBJECT_DATA')
    row.operator('wz09.mode_animation',text='Animation',icon='ACTION')

classes=(WZ09_OT_mode_world,WZ09_OT_mode_animation)
def register():
    for c in classes:bpy.utils.register_class(c)
def unregister():
    for c in reversed(classes):bpy.utils.unregister_class(c)
