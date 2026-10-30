"""POI air-label authoring and actual viewport text over existing marker IDs.

Profiles live in the existing POI marker properties. Labels are drawn at runtime
in the editor viewport, never baked into terrain textures, meshes or a movie.
The standalone sidecar retains exactly the same POI IDs as the tactical map.
"""
import copy
import json
import math
from pathlib import Path
import time
try:from .cinematic_core import number,text,vector
except ImportError:from cinematic_core import number,text,vector
try:from .authoring_helpers import resolve_owner_map
except ImportError:from authoring_helpers import resolve_owner_map

PHASES=('editor','insertion','active','extraction')
DEFAULT={'enabled':True,'region_id':None,'priority':0,'max_distance_m':4000.,
         'distance_fade_m':200.,'min_height_m':0.,'max_height_m':2000.,'height_fade_m':20.,
         'phases':list(PHASES),'fade_in_s':.5,'fade_out_s':.5,'text_size_px':24,
         'alignment':'CENTER','offset_m':[0.,0.,10.],'color':[.95,.91,.68,1.]}
_CLASSES=[]
_HANDLE=None
_CACHE={}
_FADE={}


def validate_profile(profile):
    if not isinstance(profile,dict) or set(profile)!=set(DEFAULT):raise ValueError('POI label profile has missing or unknown fields')
    p=copy.deepcopy(profile)
    if type(p['enabled']) is not bool:raise ValueError('POI enabled must be boolean')
    if p['region_id'] is not None:text(p['region_id'],'region_id')
    if type(p['priority']) is not int or not -10000<=p['priority']<=10000:raise ValueError('POI priority must be bounded integer')
    number(p['max_distance_m'],'max_distance_m',.001,1e7)
    number(p['distance_fade_m'],'distance_fade_m',0,p['max_distance_m'])
    number(p['min_height_m'],'min_height_m',-1e7,1e7)
    number(p['max_height_m'],'max_height_m',p['min_height_m']+.001,1e7)
    number(p['height_fade_m'],'height_fade_m',0,(p['max_height_m']-p['min_height_m'])/2)
    for key in ('fade_in_s','fade_out_s'):number(p[key],key,0,60)
    if type(p['text_size_px']) is not int or not 8<=p['text_size_px']<=160:raise ValueError('POI text size must be 8..160 pixels')
    if p['alignment'] not in {'LEFT','CENTER','RIGHT'}:raise ValueError('Unknown POI text alignment')
    vector(p['offset_m'],'POI offset')
    if (not isinstance(p['phases'],list) or not p['phases'] or any(type(v) is not str or v not in PHASES for v in p['phases'])
            or len(set(p['phases']))!=len(p['phases'])):
        raise ValueError('POI phases must be a unique nonempty supported phase list')
    if not isinstance(p['color'],(list,tuple)) or len(p['color'])!=4:raise ValueError('POI label needs RGBA color')
    for value in p['color']:number(value,'POI color',0,1)
    return p


def _smooth(value):
    value=max(0.,min(1.,value));return value*value*(3-2*value)


def label_weight(profile,position,viewer,*,phase='editor',region_id=None):
    """Spatial opacity; altitude is viewer Z minus the POI base Z, in design m."""
    p=validate_profile(profile);point=vector(position,'POI position');eye=vector(viewer,'viewer')
    if phase not in PHASES:raise ValueError('Unknown preview phase')
    if not p['enabled'] or phase not in p['phases'] or region_id is not None and p['region_id'] not in (None,region_id):return 0.
    distance=math.dist(point,eye);height=eye[2]-point[2]
    if distance>=p['max_distance_m'] or height<p['min_height_m'] or height>p['max_height_m']:return 0.
    distance_weight=1. if p['distance_fade_m']==0 else _smooth((p['max_distance_m']-distance)/p['distance_fade_m'])
    height_weight=1.
    if p['height_fade_m']:
        height_weight=_smooth((height-p['min_height_m'])/p['height_fade_m'])*_smooth((p['max_height_m']-height)/p['height_fade_m'])
    return distance_weight*height_weight*p['color'][3]


def fade_alpha(current,target,delta_s,fade_in_s,fade_out_s):
    current=number(current,'current alpha',0,1);target=number(target,'target alpha',0,1)
    delta=number(delta_s,'delta_s',0,10);fade=number(fade_in_s if target>current else fade_out_s,'fade_s',0,60)
    if fade==0:return target
    step=delta/fade
    return min(target,current+step) if target>current else max(target,current-step)


def marker_profile(obj):
    properties=json.loads(obj.get('wz09_properties_json','{}'))
    if not isinstance(properties,dict):raise ValueError('POI marker properties must be an object')
    return validate_profile(properties.get('poi_label',DEFAULT))


def _markers(scene):
    result=[]
    for obj in scene.objects:
        if obj.get('wz09_type')!='poi':continue
        properties=json.loads(obj.get('wz09_properties_json','{}'))
        properties['name']=str(obj.get('wz09_poi_name',obj.name))
        properties['poi_label']=marker_profile(obj)
        owner=resolve_owner_map(obj)
        if owner is not None:properties['editor_owner_map']=str(owner['wz09_id'])
        else:properties.pop('editor_owner_map',None)
        result.append({'id':str(obj['wz09_id']),'kind':'poi','position_m':list(obj.matrix_world.translation),'properties':properties})
    return result


def validate_document(document,world):
    if (not isinstance(document,dict) or set(document)!={'kind','version','units','labels','runtime_integrated','physical_scale_verified'}
            or document['kind']!='wz09_poi_labels' or type(document['version']) is not int or document['version']!=1
            or document['units']!='editor_design_meters' or document['runtime_integrated'] is not False
            or document['physical_scale_verified'] is not False):raise ValueError('Unsupported POI-label sidecar or verification claim')
    source=[m for m in world.get('markers',[]) if m.get('kind')=='poi']
    markers={m['id']:m for m in source}
    if len(markers)!=len(source):raise ValueError('Duplicate POI source ID')
    maps={m['id'] for m in world.get('maps',[])}
    regions={r['id']:r for r in world.get('composition',{}).get('regions',[])}
    if not isinstance(document['labels'],list) or len(document['labels'])>10000:raise ValueError('POI label budget exceeded')
    seen=set()
    for label in document['labels']:
        if not isinstance(label,dict) or set(label)!={'poi_id','name','position_m','map_instance_id','settings'}:raise ValueError('Unknown POI-label fields')
        uid=text(label['poi_id'],'poi_id')
        if uid in seen or uid not in markers:raise ValueError('Missing or duplicate existing POI ID')
        seen.add(uid);text(label['name'],'POI name');vector(label['position_m'],'POI position')
        if any(abs(a-b)>1e-6 for a,b in zip(label['position_m'],markers[uid]['position_m'])):raise ValueError('POI position differs from world marker')
        if label['name']!=markers[uid].get('properties',{}).get('name'):raise ValueError('POI display name differs from tactical marker')
        owner=label['map_instance_id']
        if owner!=markers[uid].get('properties',{}).get('editor_owner_map') or owner is not None and owner not in maps:
            raise ValueError('POI label owner differs from world marker')
        profile=validate_profile(label['settings'])
        if profile['region_id'] is not None and profile['region_id'] not in regions:raise ValueError('POI label references missing region')
        if owner is not None and profile['region_id'] is not None and regions[profile['region_id']].get('map_instance_id')!=owner:
            raise ValueError('POI label region belongs to another map')
    return document


def serialize_scene(scene,world=None):
    if world is None:
        world={'markers':_markers(scene),'maps':[{'id':str(o['wz09_id'])} for o in scene.objects if o.get('wz09_type')=='map_instance']}
        try:
            from .composite_world import serialize_regions
            world['composition']={'regions':serialize_regions(scene)}
        except ImportError:pass
    labels=[]
    for marker in world.get('markers',[]):
        if marker.get('kind')!='poi':continue
        properties=marker.get('properties',{})
        labels.append({'poi_id':marker['id'],'name':properties.get('name',''),
                       'position_m':list(marker['position_m']),'map_instance_id':properties.get('editor_owner_map'),
                       'settings':validate_profile(properties.get('poi_label',DEFAULT))})
    document={'kind':'wz09_poi_labels','version':1,'units':'editor_design_meters',
              'labels':sorted(labels,key=lambda x:x['poi_id']),'runtime_integrated':False,'physical_scale_verified':False}
    validate_document(document,world);return document


def refresh(scene=None):
    if scene is None:_CACHE.clear()
    else:_CACHE.pop(scene.as_pointer(),None)


def viewport_eye(view):
    """Use the camera eye, or an explicit design eye for orthographic views.

    Blender's orthographic view matrix places its origin at the focus plane.
    Use the view distance along the viewing axis for altitude/distance preview.
    A real CAMERA view always retains its actual view-matrix eye.
    """
    if view.view_perspective=='ORTHO':
        from mathutils import Vector
        return list(view.view_location + view.view_rotation @ Vector((0.,0.,view.view_distance)))
    return list(view.view_matrix.inverted().translation)


def _draw():
    import bpy,blf
    from bpy_extras.view3d_utils import location_3d_to_region_2d
    from mathutils import Vector
    ctx=bpy.context;scene=ctx.scene
    if not getattr(scene,'wz09_poi_preview',False) or not ctx.region_data or ctx.region.type!='WINDOW':return
    presentation_get=getattr(scene,'get',lambda *_args:None)
    if (ctx.region_data.view_perspective=='CAMERA' and presentation_get('wz09_cinematic_presentation_enabled',False)
            and presentation_get('wz09_cinematic_presentation_json')):
        try:
            from .cinematic_presentation import poi_viewport_allowed
            camera=scene.camera
            if not poi_viewport_allowed(json.loads(scene['wz09_cinematic_presentation_json']),
                    json.loads(scene['wz09_cinematics_json']),
                    camera.get('wz09_cinematic_camera_id') if camera else None,
                    camera.get('wz09_cinematic_sequence_id') if camera else None,'CAMERA'):return
        except (ValueError,TypeError,KeyError,ReferenceError):return
    pointer=scene.as_pointer()
    if pointer not in _CACHE:_CACHE[pointer]=[o for o in scene.objects if o.get('wz09_type')=='poi']
    eye=viewport_eye(ctx.region_data);now=time.monotonic();candidates=[]
    try:
        for obj in _CACHE[pointer]:
            if not obj.visible_get(view_layer=ctx.view_layer):continue
            p=marker_profile(obj);position=obj.matrix_world.translation
            weight=label_weight(p,list(position),list(eye),phase=scene.wz09_poi_phase,region_id=scene.wz09_poi_region or None)
            key=(pointer,str(obj['wz09_id']),ctx.region.as_pointer())
            previous,last=_FADE.get(key,(0.,now-.1))
            alpha=fade_alpha(previous,weight,min(.25,max(0.,now-last)),p['fade_in_s'],p['fade_out_s'])
            _FADE[key]=(alpha,now)
            if alpha<=.001:continue
            point=location_3d_to_region_2d(ctx.region,ctx.region_data,position+Vector(p['offset_m']))
            if point is None:continue
            candidates.append((p['priority'],str(obj.get('wz09_poi_name',obj.name)),point,p,alpha))
        occupied=[]
        for priority,name,point,p,alpha in sorted(candidates,key=lambda x:(-x[0],x[1])):
            blf.size(0,p['text_size_px']);width,height=blf.dimensions(0,name)
            x=point.x-(width/2 if p['alignment']=='CENTER' else width if p['alignment']=='RIGHT' else 0);y=point.y
            box=(x-4,y-4,x+width+4,y+height+4)
            if any(not (box[2]<b[0] or b[2]<box[0] or box[3]<b[1] or b[3]<box[1]) for b in occupied):continue
            if box[2]<0 or box[0]>ctx.region.width or box[3]<0 or box[1]>ctx.region.height:continue
            occupied.append(box);blf.position(0,x,y,0);blf.color(0,*p['color'][:3],alpha);blf.draw(0,name)
    except (ReferenceError,ValueError,TypeError,KeyError):refresh(scene)


def _changed(scene,depsgraph):
    for update in depsgraph.updates:
        if getattr(update.id,'get',None) and update.id.get('wz09_type')=='poi':refresh(scene);return


def _loaded(*args):
    refresh();_FADE.clear()


def _redraw():
    import bpy
    enabled=False
    for window in bpy.context.window_manager.windows:
        if not getattr(window.scene,'wz09_poi_preview',False):continue
        enabled=True
        for area in window.screen.areas:
            if area.type=='VIEW_3D':area.tag_redraw()
    return .1 if enabled else 1.


def _selected(ctx):
    obj=ctx.active_object
    if obj is None or obj.get('wz09_type')!='poi':raise ValueError('Select an existing POI marker')
    return obj


def register():
    global _HANDLE
    import bpy
    from bpy.props import StringProperty,FloatProperty,IntProperty,BoolProperty,EnumProperty,PointerProperty,FloatVectorProperty
    class WZ09_POISettings(bpy.types.PropertyGroup):
        name:StringProperty(name='Display name',default='Location')
        region_id:StringProperty(name='Region-ID (optional)')
        priority:IntProperty(name='Priority',default=0,min=-10000,max=10000)
        max_distance_m:FloatProperty(name='View distance',default=4000,min=1,subtype='DISTANCE')
        distance_fade_m:FloatProperty(name='Distance fade',default=200,min=0,subtype='DISTANCE')
        min_height_m:FloatProperty(name='Minimum height above POI',default=0,subtype='DISTANCE')
        max_height_m:FloatProperty(name='Maximum height above POI',default=2000,subtype='DISTANCE')
        height_fade_m:FloatProperty(name='Height fade',default=20,min=0,subtype='DISTANCE')
        phases:StringProperty(name='Phases (separated by semicolons)',default='editor;insertion;active;extraction')
        fade_in_s:FloatProperty(name='Fade in (s)',default=.5,min=0,max=60)
        fade_out_s:FloatProperty(name='Fade out (s)',default=.5,min=0,max=60)
        text_size_px:IntProperty(name='Font size (px)',default=24,min=8,max=160)
        alignment:EnumProperty(name='Alignment',items=[(v,v,'') for v in ('LEFT','CENTER','RIGHT')],default='CENTER')
        offset_m:FloatVectorProperty(name='Label offset',size=3,default=(0,0,10),subtype='TRANSLATION')
        color:FloatVectorProperty(name='Text color',size=4,subtype='COLOR',default=(.95,.91,.68,1),min=0,max=1)
        enabled:BoolProperty(name='Label enabled',default=True)
        export_path:StringProperty(name='POI JSON',default='exports/poi-labels.json',subtype='FILE_PATH')
    class WZ09_OT_poi_label(bpy.types.Operator):
        bl_idname='wz09.poi_label';bl_label='Edit POI label';bl_options={'REGISTER','UNDO'}
        action:EnumProperty(items=[(x,x,'') for x in ('LOAD','APPLY','REGION','EXPORT')])
        def execute(self,ctx):
            try:
                s=ctx.scene.wz09_poi_settings
                if self.action=='EXPORT':
                    from .scene_io import safe_output_path,atomic_write
                    root=Path(ctx.scene.wz09_project_root).resolve()
                    if not (root/'AGENTS.md').is_file():raise ValueError('Set the standalone project path')
                    path=safe_output_path(s.export_path,root=root);atomic_write(path,serialize_scene(ctx.scene),root=root)
                elif self.action=='REGION':
                    from .authoring_helpers import resolve_owner_map
                    from .composite_world import serialize_regions
                    owner=resolve_owner_map(_selected(ctx));records=[r for r in serialize_regions(ctx.scene) if owner and r['map_instance_id']==owner.get('wz09_id')]
                    if len(records)!=1:raise ValueError('Create exactly one map core for the POI owner map')
                    s.region_id=records[0]['id']
                else:
                    obj=_selected(ctx)
                    if self.action=='LOAD':
                        p=marker_profile(obj);s.name=str(obj.get('wz09_poi_name',obj.name))
                        for key in DEFAULT:
                            value=p[key]
                            if key=='region_id':value=value or ''
                            if key=='phases':value=';'.join(value)
                            setattr(s,key,value)
                    else:
                        p={key:getattr(s,key) for key in DEFAULT}
                        p['offset_m']=list(s.offset_m);p['color']=list(s.color);p['region_id']=s.region_id.strip() or None
                        p['phases']=[x.strip() for x in s.phases.split(';') if x.strip()]
                        p=validate_profile(p);text(s.name,'POI name')
                        props=json.loads(obj.get('wz09_properties_json','{}'));props['poi_label']=p
                        obj['wz09_properties_json']=json.dumps(props,sort_keys=True,allow_nan=False);obj['wz09_poi_name']=s.name
                        refresh(ctx.scene)
                return {'FINISHED'}
            except (ValueError,TypeError,KeyError,OSError,RuntimeError) as exc:
                self.report({'ERROR'},str(exc)[:240]);return {'CANCELLED'}
    class WZ09_PT_poi_labels(bpy.types.Panel):
        bl_idname='WZ09_PT_poi_labels';bl_label='Karvorsk - location names';bl_parent_id='WZ09_PT_main'
        bl_space_type='VIEW_3D';bl_region_type='UI';bl_category='WZ2009'
        @classmethod
        def poll(cls,ctx):return getattr(ctx.scene,'wz09_workspace','') in {'POI','CINEMATICS','STUDIO'}
        def draw(self,ctx):
            a=self.layout;s=ctx.scene.wz09_poi_settings
            a.label(text='Use an existing POI marker with a stable ID',icon='EMPTY_AXIS')
            a.prop(ctx.scene,'wz09_poi_preview');a.prop(ctx.scene,'wz09_poi_phase');a.prop(ctx.scene,'wz09_poi_region')
            a.operator('wz09.poi_label',text='Read selection',icon='EYEDROPPER').action='LOAD'
            for key in ('name','enabled','region_id','priority','max_distance_m','distance_fade_m','min_height_m',
                        'max_height_m','height_fade_m','phases','fade_in_s','fade_out_s','text_size_px','alignment','offset_m','color'):a.prop(s,key)
            a.operator('wz09.poi_label',text='Region from map core',icon='LINKED').action='REGION'
            a.operator('wz09.poi_label',text='Apply label',icon='CHECKMARK').action='APPLY'
            a.prop(s,'export_path');a.operator('wz09.poi_label',text='Export POI labels separately',icon='EXPORT').action='EXPORT'
            a.label(text='Dynamic text - no video or texture bake',icon='INFO')
    _CLASSES[:]=[WZ09_POISettings,WZ09_OT_poi_label,WZ09_PT_poi_labels]
    for cls in _CLASSES:bpy.utils.register_class(cls)
    bpy.types.Scene.wz09_poi_settings=PointerProperty(type=WZ09_POISettings)
    bpy.types.Scene.wz09_poi_preview=BoolProperty(name='Location names in viewport',default=False)
    bpy.types.Scene.wz09_poi_phase=EnumProperty(name='Preview phase',items=[(v,v,'') for v in PHASES],default='editor')
    bpy.types.Scene.wz09_poi_region=StringProperty(name='Preview region (empty = all)')
    _HANDLE=bpy.types.SpaceView3D.draw_handler_add(_draw,(),'WINDOW','POST_PIXEL')
    bpy.app.handlers.persistent(_changed);bpy.app.handlers.persistent(_loaded)
    bpy.app.handlers.depsgraph_update_post.append(_changed)
    bpy.app.handlers.load_post.append(_loaded)
    bpy.app.timers.register(_redraw,first_interval=1.,persistent=True)


def unregister():
    global _HANDLE
    import bpy
    if _HANDLE is not None:bpy.types.SpaceView3D.draw_handler_remove(_HANDLE,'WINDOW');_HANDLE=None
    if _changed in bpy.app.handlers.depsgraph_update_post:bpy.app.handlers.depsgraph_update_post.remove(_changed)
    if _loaded in bpy.app.handlers.load_post:bpy.app.handlers.load_post.remove(_loaded)
    if bpy.app.timers.is_registered(_redraw):bpy.app.timers.unregister(_redraw)
    for key in ('wz09_poi_settings','wz09_poi_preview','wz09_poi_phase','wz09_poi_region'):
        if hasattr(bpy.types.Scene,key):delattr(bpy.types.Scene,key)
    for cls in reversed(_CLASSES):bpy.utils.unregister_class(cls)
    _CLASSES.clear();_CACHE.clear();_FADE.clear()
