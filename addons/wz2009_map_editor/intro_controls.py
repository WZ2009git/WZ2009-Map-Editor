"""Small navigation controls for an existing validated round-start sequence.

No import/build on registration. All targets use persistent IDs. The controls
scrub the native timeline; the boarding event remains a runtime contract.
"""
import copy

try:
    from . import cinematic_core as core
except ImportError:
    import cinematic_core as core

_CLASSES=[]


def stages(document):
    """Return useful shot times without names, mesh guesses or runtime effects."""
    cuts=sorted(document['cuts'],key=lambda r:r['time_s'])
    events=document['events']
    result=[]
    if cuts:result.append(('APPROACH','1 · Wolkenanflug',cuts[0]['time_s']))
    black=[e for e in events if e['kind']=='screen_fade' and e['parameters']['to_alpha']==1]
    if black:
        e=black[0];result.append(('BLACK','2 · Schwarzblende',e['time_s']+e['parameters']['duration_s']))
    if len(cuts)>1:
        chase=cuts[1]['time_s']
        if core.screen_fade_sample(document,chase)['alpha']>0:
            clears=sorted((e['time_s']+e['parameters']['duration_s'] for e in events
                if e['kind']=='screen_fade' and e['parameters']['to_alpha']==0
                and e['time_s']+e['parameters']['duration_s']>=chase))
            if clears:chase=clears[0]
        result.append(('CHASE','3 - Flyover',chase))
    boarding=[e for e in events if e['kind']=='aircraft_insertion']
    if boarding:result.append(('BOARDING','4 · Im Flugzeug / Absprung',boarding[0]['time_s']))
    return result


def update_jump_window(document,world,open_s,close_s):
    """Copy/validate before publication, preserving source document on failure."""
    result=copy.deepcopy(document)
    events=[e for e in result['events'] if e['kind']=='aircraft_insertion']
    if len(events)!=1:raise ValueError('Select exactly one aircraft insertion')
    events[0]['parameters'].update(jump_open_s=open_s,jump_close_s=close_s)
    core.validate_document(result,world)
    return result


def _document(scene):
    from . import cinematic_studio
    doc=cinematic_studio._stored(scene)
    core.validate_document(doc,cinematic_studio.collect_world(scene))
    return doc


def load_settings(scene):
    doc=_document(scene)
    boarding=[e for e in doc['events'] if e['kind']=='aircraft_insertion']
    if len(boarding)!=1:raise ValueError('This sequence has no unique aircraft insertion')
    settings=scene.wz09_intro_settings;params=boarding[0]['parameters']
    settings.jump_open_s=params['jump_open_s'];settings.jump_close_s=params['jump_close_s']


def goto_stage(ctx,stage):
    doc=_document(ctx.scene)
    choices={key:time for key,_,time in stages(doc)}
    if stage not in choices:raise ValueError('This intro section does not exist')
    ctx.scene.frame_set(core.frame_at(doc,choices[stage]))
    cuts=[c for c in doc['cuts'] if c['time_s']<=choices[stage]+1e-8]
    if cuts:
        uid=max(cuts,key=lambda c:c['time_s'])['camera_id']
        cameras=[o for o in ctx.scene.objects if o.get('wz09_cinematic_camera_id')==uid]
        if len(cameras)!=1:raise ValueError('Camera missing or ID duplicated')
        ctx.scene.camera=cameras[0]
    if getattr(ctx,'area',None) and ctx.area.type=='VIEW_3D':
        ctx.space_data.region_3d.view_perspective='CAMERA'
        # Camera preview keeps the POI GPU labels, while thousands of native
        # authoring names and empty outlines stay out of the shot.
        overlay=getattr(ctx.space_data,'overlay',None)
        if overlay is not None:overlay.show_overlays=False
    return ctx.scene.frame_current


def select_aircraft(ctx):
    doc=_document(ctx.scene)
    events=[e for e in doc['events'] if e['kind']=='aircraft_insertion']
    if len(events)!=1:raise ValueError('No unique aircraft insertion')
    uid=events[0]['parameters']['aircraft_placement_id']
    targets=[o for o in ctx.scene.objects if o.get('wz09_asset_placement_id')==uid and o.get('wz09_asset_placement')]
    if len(targets)!=1:raise ValueError('Aircraft root missing or ID duplicated')
    for obj in ctx.selected_objects:obj.select_set(False)
    obj=targets[0];obj.hide_set(False);obj.select_set(True);ctx.view_layer.objects.active=obj
    return obj


def draw_controls(layout,ctx):
    """Draw one compact intro box without world scans or reloading edited UI values."""
    try:
        # Draw must not scan the 155k-object world on every play frame.
        # Button actions retain full semantic reference validation.
        from . import cinematic_studio
        doc=cinematic_studio._stored(ctx.scene)
    except (ValueError,TypeError,KeyError) as exc:
        layout.label(text=str(exc)[:90],icon='ERROR');return False
    if not any(e['kind']=='aircraft_insertion' for e in doc['events']):return False
    a=layout.box()
    a.label(text='Round start - approach and jump',icon='CAMERA_DATA')
    for stage,label,_ in stages(doc):
        a.operator('wz09.intro_control',text=label,icon='CAMERA_DATA' if stage!='BLACK' else 'SEQUENCE').action=stage
    a.operator('screen.animation_play',text='Play / pause intro',icon='PLAY')
    a.label(text='Editor guides: use Clean View above',icon='INFO')
    a.operator('wz09.intro_control',text='Select aircraft',icon='RESTRICT_SELECT_OFF').action='PLANE'
    a.label(text='Flight route: move markers',icon='CURVE_PATH')
    a.label(text='Rebuild the timeline afterwards')
    box=a.box();settings=ctx.scene.wz09_intro_settings
    box.operator('wz09.intro_control',text='Load jump settings',icon='IMPORT').action='LOAD'
    box.prop(settings,'jump_open_s');box.prop(settings,'jump_close_s')
    box.operator('wz09.intro_control',text='Apply jump window',icon='CHECKMARK').action='APPLY'
    a.label(text='In-game jump behavior unverified',icon='INFO')
    return True


def register():
    import bpy
    from bpy.props import EnumProperty,FloatProperty,PointerProperty
    class WZ09_IntroSettings(bpy.types.PropertyGroup):
        jump_open_s:FloatProperty(name='Jump start (s)',description='Seconds after aircraft insertion',min=0,default=0)
        jump_close_s:FloatProperty(name='Jump end (s)',description='Seconds after aircraft insertion; the route must continue',min=0,default=10)
    class WZ09_OT_intro_control(bpy.types.Operator):
        bl_idname='wz09.intro_control';bl_label='Edit round start';bl_options={'REGISTER','UNDO'}
        action:EnumProperty(items=[(v,v,'') for v in ('APPROACH','BLACK','CHASE','BOARDING','PLANE','LOAD','APPLY')])
        def execute(self,ctx):
            try:
                if self.action=='LOAD':load_settings(ctx.scene)
                elif self.action=='APPLY':
                    from . import cinematic_studio
                    settings=ctx.scene.wz09_intro_settings
                    doc=update_jump_window(_document(ctx.scene),cinematic_studio.collect_world(ctx.scene),settings.jump_open_s,settings.jump_close_s)
                    cinematic_studio._save(ctx.scene,doc)
                    cinematic_studio.build_timeline(ctx)
                elif self.action=='PLANE':select_aircraft(ctx)
                else:goto_stage(ctx,self.action)
                return {'FINISHED'}
            except (ValueError,TypeError,KeyError,RuntimeError) as exc:
                self.report({'ERROR'},str(exc)[:240]);return {'CANCELLED'}
    _CLASSES[:]=[WZ09_IntroSettings,WZ09_OT_intro_control]
    for cls in _CLASSES:bpy.utils.register_class(cls)
    bpy.types.Scene.wz09_intro_settings=PointerProperty(type=WZ09_IntroSettings)


def unregister():
    import bpy
    if hasattr(bpy.types.Scene,'wz09_intro_settings'):del bpy.types.Scene.wz09_intro_settings
    for cls in reversed(_CLASSES):bpy.utils.unregister_class(cls)
    _CLASSES.clear()
