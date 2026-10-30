"""Disposable cinematic presentation over existing cameras and POI identities.

Native camera planes make screen fades and air labels visible in real Blender
renders. The matching GPU fade is restricted to camera view. Procedural clouds
are an explicit editor preview, not an extracted IW shader, collider or game FX.
Nothing is created, registered, rendered or played merely by importing this file.
"""
import hashlib
import json
import math
import uuid
from array import array

try:
    from . import cinematic_core as core, poi_labels
except ImportError:
    import cinematic_core as core
    import poi_labels

_HANDLE=None
_HANDLERS=[]
_BUSY=set()
_INDEX={}


def helper_id(sequence_id,role,source_id=''):
    return str(uuid.uuid5(uuid.NAMESPACE_URL,'wz09/cinematic-presentation/'+sequence_id+'/'+role+'/'+source_id))


def presentation_signature(document):
    payload={'id':document['id'],'cameras':sorted(c['id'] for c in document['cameras']),
             'fades':sorted((e for e in document['events'] if e['kind']=='screen_fade'),key=lambda e:e['id'])}
    return hashlib.sha256(json.dumps(payload,sort_keys=True,separators=(',',':'),allow_nan=False).encode()).hexdigest()


def validate_cloud_bounds(bounds):
    if not isinstance(bounds,(list,tuple)) or len(bounds)!=6:raise ValueError('Cloud bounds require minXYZ,maxXYZ')
    values=[core.number(v,'cloud bound',-1e8,1e8) for v in bounds]
    if any(values[i+3]<=values[i] for i in range(3)):raise ValueError('Cloud bounds need positive extents')
    return values


def active_camera_id(document,time_s):
    time=core.number(time_s,'camera sample time_s',0,document['duration_s'])
    cuts=sorted((c for c in document['cuts'] if c['time_s']<=time),key=lambda c:c['time_s'])
    if cuts:return cuts[-1]['camera_id']
    return document['cameras'][0]['id'] if document['cameras'] else None


def validate_poi_camera_ids(value,camera_ids):
    """None preserves legacy all-camera previews; an explicit empty list hides labels."""
    if value is None:return None
    if not isinstance(value,list) or any(not isinstance(uid,str) for uid in value):
        raise ValueError('POI camera allowlist must be a list of existing camera IDs')
    if len(value)!=len(set(value)) or any(uid not in camera_ids for uid in value):
        raise ValueError('POI camera allowlist has duplicate/missing camera IDs')
    return sorted(value)


def poi_viewport_allowed(config,document,camera_id,camera_sequence_id,view_perspective,*,enabled=True):
    """Apply the shot allowlist only inside this cinematic's camera preview."""
    if view_perspective!='CAMERA' or not enabled or config is None:return True
    cameras={c['id'] for c in document['cameras']}
    if (config.get('sequence_id')!=document['id'] or config.get('signature')!=presentation_signature(document)
            or camera_sequence_id!=document['id'] or camera_id not in cameras):return False
    allowed=validate_poi_camera_ids(config.get('poi_camera_ids'),cameras)
    return allowed is None or camera_id in allowed


def label_layout(profile,position,viewer,projected,frame_width,frame_height,*,phase='insertion'):
    """Pure camera-space layout for a rendered counterpart of an existing POI.

    projected is Blender's normalized camera XYZ; negative Z is behind camera.
    Time-based editor wall-clock fades intentionally do not enter a movie.
    """
    p=poi_labels.validate_profile(profile)
    core.number(frame_width,'frame width',1,100000);core.number(frame_height,'frame height',1,100000)
    xyz=core.vector(projected,'camera projection')
    alpha=poi_labels.label_weight(p,position,viewer,phase=phase)
    visible=xyz[2]>0 and 0<=xyz[0]<=1 and 0<=xyz[1]<=1 and alpha>1e-6
    return {'visible':visible,'alpha':alpha if visible else 0.,
            'x_px':xyz[0]*frame_width,'y_px':xyz[1]*frame_height,
            'text_size_px':p['text_size_px'],'alignment':p['alignment'],'color':list(p['color'])}


def _tag(obj,sequence_id,role,source_id=''):
    obj['wz09_cinematic_presentation']=True
    obj['wz09_cinematic_sequence_id']=sequence_id
    obj['wz09_cinematic_presentation_role']=role
    obj['wz09_cinematic_presentation_source_id']=source_id
    obj['wz09_cinematic_helper_id']=helper_id(sequence_id,role,source_id)
    if obj.data is not None:
        obj.data['wz09_cinematic_helper_id']=obj['wz09_cinematic_helper_id']
    obj['wz09_editor_overlay']=True
    obj['wz09_overlay_kind']='cinematic_presentation'
    obj['wz09_runtime_integrated']=False
    obj['wz09_collision_verified']=False
    for key in ('visible_shadow','visible_diffuse','visible_glossy','visible_transmission','visible_volume_scatter'):
        if hasattr(obj,key):setattr(obj,key,False)


def _material(api,name,color=(0.,0.,0.),alpha=0.,owner_id=''):
    material=api.data.materials.new(name);material.use_nodes=True
    material['wz09_cinematic_helper_id']=owner_id
    nodes=material.node_tree.nodes;nodes.clear()
    output=nodes.new('ShaderNodeOutputMaterial');output.name='WZ09 Output'
    mix=nodes.new('ShaderNodeMixShader');mix.name='WZ09 Opacity'
    transparent=nodes.new('ShaderNodeBsdfTransparent');transparent.name='WZ09 Transparent'
    emission=nodes.new('ShaderNodeEmission');emission.name='WZ09 Color'
    emission.inputs['Color'].default_value=(*color,1.)
    emission.inputs['Strength'].default_value=1.
    mix.inputs[0].default_value=alpha
    links=material.node_tree.links
    links.new(transparent.outputs[0],mix.inputs[1]);links.new(emission.outputs[0],mix.inputs[2]);links.new(mix.outputs[0],output.inputs['Surface'])
    # Camera overlays need continuous opacity: Eevee's stochastic dither
    # produces visible grain/bands during a fade, especially in fast previews.
    if hasattr(material,'surface_render_method'):material.surface_render_method='BLENDED'
    if hasattr(material,'use_transparency_overlap'):material.use_transparency_overlap=False
    material.diffuse_color=(*color,alpha)
    return material


def _check_update_target(obj,sequence_id):
    """Fail before writing if a preview was rebound to foreign data/materials."""
    role=obj.get('wz09_cinematic_presentation_role');source=str(obj.get('wz09_cinematic_presentation_source_id',''))
    expected=helper_id(sequence_id,role,source)
    if (obj.get('wz09_cinematic_helper_id')!=expected or obj.get('wz09_cinematic_sequence_id')!=sequence_id
            or obj.library or obj.override_library):
        raise ValueError('Cinematic helper ownership changed: rebuild presentation')
    data=obj.data
    if (data is None or data.get('wz09_cinematic_helper_id')!=expected or data.library
            or getattr(data,'override_library',None) or data.users!=1):
        raise ValueError('Cinematic helper data is foreign/shared: rebuild presentation')
    if len(data.materials)!=1 or data.materials[0] is None:
        raise ValueError('Cinematic helper material missing: rebuild presentation')
    material=data.materials[0]
    if (material.get('wz09_cinematic_helper_id')!=expected or material.library
            or getattr(material,'override_library',None) or material.users!=1):
        raise ValueError('Cinematic helper material is foreign/shared: rebuild presentation')
    if role!='cloud':
        camera=obj.parent
        if camera is None or camera.type!='CAMERA' or camera.get('wz09_cinematic_sequence_id')!=sequence_id:
            raise ValueError('Cinematic screen helper parent is foreign: rebuild presentation')
        camera_id=str(camera.get('wz09_cinematic_camera_id',''))
        if source!=(camera_id if role=='fade' else camera_id+'/'+str(obj.get('wz09_poi_source_id',''))):
            raise ValueError('Cinematic screen helper camera identity changed: rebuild presentation')
        if not material.use_nodes or len(material.node_tree.nodes)!=4:
            raise ValueError('Cinematic helper shader changed: rebuild presentation')
        nodes=material.node_tree.nodes
        required={'WZ09 Opacity':'ShaderNodeMixShader','WZ09 Color':'ShaderNodeEmission',
                  'WZ09 Transparent':'ShaderNodeBsdfTransparent','WZ09 Output':'ShaderNodeOutputMaterial'}
        for name,kind in required.items():
            if nodes.get(name) is None or nodes[name].bl_idname!=kind:
                raise ValueError('Cinematic helper shader changed: rebuild presentation')
        links=list(material.node_tree.links);mix=nodes['WZ09 Opacity']
        expected_links=[(nodes['WZ09 Transparent'].outputs[0],mix.inputs[1]),
                        (nodes['WZ09 Color'].outputs[0],mix.inputs[2]),
                        (mix.outputs[0],nodes['WZ09 Output'].inputs['Surface'])]
        if len(links)!=3 or any(not any(l.from_socket==a and l.to_socket==b for l in links) for a,b in expected_links):
            raise ValueError('Cinematic helper shader links changed: rebuild presentation')
    return material


def _owned(scene,document):
    return [o for o in scene.objects if o.get('wz09_cinematic_presentation') is True
            and o.get('wz09_cinematic_sequence_id')==document['id']]


def _check_owned(scene,document):
    owned=_owned(scene,document);seen=set()
    for obj in owned:
        role=obj.get('wz09_cinematic_presentation_role');source=str(obj.get('wz09_cinematic_presentation_source_id',''))
        uid=obj.get('wz09_cinematic_helper_id')
        if role not in {'fade','poi','cloud'} or uid!=helper_id(document['id'],role,source) or uid in seen:
            raise ValueError('Cinematic helper identity changed or duplicated')
        seen.add(uid)
        if obj.library or obj.override_library or len(obj.users_scene)!=1 or obj.users_scene[0]!=scene:
            raise ValueError('Linked/shared cinematic helpers cannot be changed')
        if obj.get('wz09_type') or obj.get('wz09_asset_placement') or obj.get('wz09_custom_geometry'):
            raise ValueError('Cinematic helper must remain excluded from world/collision records')
    return owned


def _index(scene,document,config):
    key=scene.as_pointer();cached=_INDEX.get(key)
    if cached is not None and cached['count']==len(scene.objects) and cached['signature']==config['signature']:
        return cached
    sources={str(o['wz09_id']):o for o in scene.objects if o.get('wz09_type')=='poi'} if config['render_poi'] else {}
    if sorted(sources)!=config['poi_ids']:raise ValueError('POI set changed: rebuild cinematic presentation')
    cached={'count':len(scene.objects),'signature':config['signature'],'sources':sources,
            'helpers':_check_owned(scene,document)}
    _INDEX[key]=cached
    return cached


def remove_presentation(scene,*,bpy_module=None):
    """Remove only exclusively owned helpers; cameras, POIs and source meshes stay."""
    if bpy_module is None:
        import bpy as bpy_module
    document=json.loads(scene.get('wz09_cinematics_json','null'))
    if document is None:return
    _INDEX.pop(scene.as_pointer(),None)
    owned=_check_owned(scene,document);keys={o.as_pointer() for o in owned}
    if any(child.as_pointer() not in keys for obj in owned for child in obj.children):
        raise ValueError('Foreign child on cinematic helper; detach it explicitly first')
    data=[];materials={}
    for obj in owned:
        data.append((obj.type,obj.data))
        materials.update({m.as_pointer():m for m in getattr(obj.data,'materials',()) if m is not None})
        bpy_module.data.objects.remove(obj,do_unlink=True)
    for kind,item in data:
        if item is not None and item.users==0:
            (bpy_module.data.meshes if kind=='MESH' else bpy_module.data.curves).remove(item)
    for material in materials.values():
        if material.users==0:bpy_module.data.materials.remove(material)
    for key in ('wz09_cinematic_presentation_json','wz09_cinematic_presentation_error'):
        if key in scene:del scene[key]
    for collection in list(scene.collection.children):
        if (collection.get('wz09_cinematic_presentation') is True
                and collection.get('wz09_cinematic_sequence_id')==document['id']
                and not collection.objects and not collection.children and collection.users==1):
            bpy_module.data.collections.remove(collection)


def build_presentation(ctx,document=None,world=None,*,cloud_bounds=None,render_poi=True,
                       cloud_density=.015,cloud_noise_scale=.004,poi_camera_ids=None):
    """Explicitly build/rebuild owned native helpers, without rendering/playback.

    cloud_bounds=[minX,minY,minZ,maxX,maxY,maxZ], in editor design metres.
    The .blend retains clouds; the existing FX event may describe their profile.
    The portable timeline exports exact fades, not procedural shader internals.
    """
    import bpy
    from . import cinematic_studio
    scene=ctx.scene
    document=document or json.loads(scene.get('wz09_cinematics_json','null'))
    if document is None:raise ValueError('Create a cinematic sequence first')
    core.validate_document(document,world or cinematic_studio.collect_world(scene))
    if not document['cameras']:raise ValueError('Cinematic presentation needs an existing camera')
    if type(render_poi) is not bool:raise ValueError('render_poi must be boolean')
    bounds=validate_cloud_bounds(cloud_bounds) if cloud_bounds is not None else None
    core.number(cloud_density,'cloud_density',.000001,10)
    core.number(cloud_noise_scale,'cloud_noise_scale',.000001,1000)
    cameras={str(o['wz09_cinematic_camera_id']):o for o in scene.objects
             if o.type=='CAMERA' and o.get('wz09_cinematic_sequence_id')==document['id']}
    if set(cameras)!={c['id'] for c in document['cameras']}:raise ValueError('Missing/duplicate native sequence camera')
    allowed_poi_cameras=validate_poi_camera_ids(poi_camera_ids,set(cameras))
    poi_sources={str(o['wz09_id']):o for o in scene.objects if o.get('wz09_type')=='poi'} if render_poi else {}
    for obj in poi_sources.values():poi_labels.marker_profile(obj)
    remove_presentation(scene,bpy_module=bpy)
    collection=bpy.data.collections.new('WZ09 Cinematic Presentation '+document['id'][:8])
    collection['wz09_cinematic_sequence_id']=document['id'];collection['wz09_cinematic_presentation']=True
    collection['wz09_editor_overlay']=True;collection['wz09_collision_verified']=False
    scene.collection.children.link(collection)
    for uid,camera in cameras.items():
        mesh=bpy.data.meshes.new('Cinematic fade plane');mesh.from_pydata([(-1,-1,0),(1,-1,0),(1,1,0),(-1,1,0)],[],[(0,1,2,3)]);mesh.update()
        obj=bpy.data.objects.new('Intro · Bildschirmblende',mesh);collection.objects.link(obj)
        _tag(obj,document['id'],'fade',uid);obj.parent=camera;obj.hide_viewport=True
        mesh.materials.append(_material(bpy,'Intro fade · '+uid[:8],owner_id=obj['wz09_cinematic_helper_id']))
        for poi_id,source in poi_sources.items():
            data=bpy.data.curves.new('Air label '+poi_id,'FONT');data.body=str(source.get('wz09_poi_name',source.name))
            data.align_x='CENTER';data.align_y='BOTTOM_BASELINE';data.resolution_u=2
            label=bpy.data.objects.new('Intro · '+data.body,data);collection.objects.link(label)
            _tag(label,document['id'],'poi',uid+'/'+poi_id);label['wz09_poi_source_id']=poi_id
            label['wz09_cinematic_camera_id_ref']=uid;label.parent=camera;label.hide_viewport=True
            data.materials.append(_material(bpy,'Intro label '+poi_id[:8],(1,1,1),owner_id=label['wz09_cinematic_helper_id']))
    if bounds is not None:
        lo=bounds[:3];hi=bounds[3:]
        vertices=[(x,y,z) for z in (lo[2],hi[2]) for y in (lo[1],hi[1]) for x in (lo[0],hi[0])]
        faces=[(0,2,3,1),(4,5,7,6),(0,1,5,4),(2,6,7,3),(0,4,6,2),(1,3,7,5)]
        mesh=bpy.data.meshes.new('Procedural cloud volume');mesh.from_pydata(vertices,[],faces);mesh.update()
        obj=bpy.data.objects.new('Intro · Prozedurale Wolkenvorschau',mesh);collection.objects.link(obj)
        _tag(obj,document['id'],'cloud');obj.display_type='WIRE';obj['wz09_procedural_cloud_bounds_json']=json.dumps(bounds)
        material=bpy.data.materials.new('Intro · Wolkenvolumen');material.use_nodes=True
        material['wz09_cinematic_helper_id']=obj['wz09_cinematic_helper_id']
        nodes=material.node_tree.nodes;nodes.clear();output=nodes.new('ShaderNodeOutputMaterial')
        volume=nodes.new('ShaderNodeVolumePrincipled');volume.inputs['Color'].default_value=(.78,.84,.94,1)
        volume.inputs['Anisotropy'].default_value=.25
        noise=nodes.new('ShaderNodeTexNoise');noise.inputs['Scale'].default_value=cloud_noise_scale
        noise.inputs['Detail'].default_value=3.
        geometry=nodes.new('ShaderNodeNewGeometry');ramp=nodes.new('ShaderNodeValToRGB')
        ramp.color_ramp.elements[0].position=.38;ramp.color_ramp.elements[1].position=.7
        density=nodes.new('ShaderNodeMath');density.operation='MULTIPLY';density.inputs[1].default_value=cloud_density
        links=material.node_tree.links;links.new(geometry.outputs['Position'],noise.inputs['Vector'])
        links.new(noise.outputs['Fac'],ramp.inputs[0]);links.new(ramp.outputs[0],density.inputs[0])
        links.new(density.outputs[0],volume.inputs['Density']);links.new(volume.outputs['Volume'],output.inputs['Volume'])
        mesh.materials.append(material)
        # Unlike screen helpers, the cloud scatters illumination deliberately.
        if hasattr(obj,'visible_volume_scatter'):obj.visible_volume_scatter=True
    config={'version':1,'sequence_id':document['id'],'signature':presentation_signature(document),
            'render_poi':render_poi,'poi_ids':sorted(poi_sources),'cloud_bounds':bounds,
            'poi_camera_ids':allowed_poi_cameras,
            'cloud_density':cloud_density,'cloud_noise_scale':cloud_noise_scale,
            'runtime_integrated':False,'collision_verified':False,'render_preview_only':True}
    scene['wz09_cinematic_presentation_json']=json.dumps(config,sort_keys=True,allow_nan=False)
    scene['wz09_cinematic_presentation_enabled']=True
    update_scene(scene)
    return dict(config,helper_count=len(_owned(scene,document)))


def validate_scene(scene,document=None):
    """Check the owned preview graph before export; never silently repair it."""
    raw=scene.get('wz09_cinematic_presentation_json')
    if not raw:return None
    document=document or json.loads(scene['wz09_cinematics_json']);config=json.loads(raw)
    if (config.get('version')!=1 or config.get('sequence_id')!=document['id']
            or config.get('signature')!=presentation_signature(document)
            or config.get('runtime_integrated') is not False or config.get('collision_verified') is not False):
        raise ValueError('Cinematic presentation stale/unsupported: rebuild presentation')
    cameras={c['id'] for c in document['cameras']}
    validate_poi_camera_ids(config.get('poi_camera_ids'),cameras)
    expected={helper_id(document['id'],'fade',uid) for uid in cameras}
    for uid in cameras:
        expected.update(helper_id(document['id'],'poi',uid+'/'+pid) for pid in config['poi_ids'])
    if config['cloud_bounds'] is not None:expected.add(helper_id(document['id'],'cloud'))
    owned=_check_owned(scene,document)
    if {o['wz09_cinematic_helper_id'] for o in owned}!=expected:raise ValueError('Cinematic helpers missing/changed: rebuild presentation')
    for obj in owned:
        role=obj['wz09_cinematic_presentation_role']
        _check_update_target(obj,document['id'])
        if obj.constraints or obj.modifiers or obj.animation_data:
            raise ValueError('Cinematic helper gained animation/constraint/modifier: rebuild presentation')
        if any(abs(v-1)>1e-6 for v in obj.scale) or any(abs(v)>1e-6 for v in obj.rotation_euler):
            raise ValueError('Cinematic helper scale/rotation changed: rebuild presentation')
        if role=='cloud':
            if obj.type!='MESH' or obj.parent is not None or any(abs(v)>1e-6 for v in obj.location):
                raise ValueError('Cloud helper transform changed: rebuild presentation')
            bounds=validate_cloud_bounds(config['cloud_bounds'])
            actual=[min(v.co[i] for v in obj.data.vertices) for i in range(3)]+[max(v.co[i] for v in obj.data.vertices) for i in range(3)]
            if any(abs(a-b)>max(1e-4,abs(b)*1e-6) for a,b in zip(actual,bounds)):
                raise ValueError('Cloud helper bounds changed: rebuild presentation')
        else:
            camera=obj.parent
            if camera is None or camera.get('wz09_cinematic_camera_id') not in cameras:
                raise ValueError('Cinematic screen helper camera changed: rebuild presentation')
            uid=str(camera['wz09_cinematic_camera_id'])
            source=obj['wz09_cinematic_presentation_source_id']
            if role=='fade' and (source!=uid or obj.type!='MESH' or len(obj.data.vertices)!=4):
                raise ValueError('Fade helper changed: rebuild presentation')
            if role=='poi' and (source!=uid+'/'+str(obj.get('wz09_poi_source_id')) or obj.type!='FONT'):
                raise ValueError('POI helper identity changed: rebuild presentation')
            if len(obj.data.materials)!=1 or obj.data.materials[0] is None:
                raise ValueError('Cinematic helper material missing: rebuild presentation')
            material=obj.data.materials[0]
            if not material.use_nodes or len(material.node_tree.nodes)!=4:
                raise ValueError('Cinematic helper shader changed: rebuild presentation')
            if material.node_tree.nodes.get('WZ09 Opacity') is None or material.node_tree.nodes.get('WZ09 Color') is None:
                raise ValueError('Cinematic helper opacity shader changed: rebuild presentation')
    return {'helper_count':len(owned),'render_preview_only':True,'runtime_integrated':False,'collision_verified':False}


def _set_rna_if_changed(owner,attribute,value):
    """Avoid native dependency tags for unchanged RNA float32/display values.

    Compare the actual stored value on every call. This deliberately has no
    frame cache: same-frame source edits and ownership validation still apply.
    """
    if isinstance(value,(tuple,list)):
        value=tuple(array('f',value));current=tuple(getattr(owner,attribute))
    else:
        if type(value) is float:value=array('f',[value])[0]
        current=getattr(owner,attribute)
    if current==value:return False
    setattr(owner,attribute,value);return True


def _set_material(material,color,alpha):
    changed=_set_rna_if_changed(material.node_tree.nodes['WZ09 Opacity'].inputs[0],'default_value',alpha)
    changed=_set_rna_if_changed(material.node_tree.nodes['WZ09 Color'].inputs['Color'],'default_value',(*color[:3],1.)) or changed
    return _set_rna_if_changed(material,'diffuse_color',(*color[:3],alpha)) or changed


def update_scene(scene):
    """Evaluate presentation at absolute scene frame, also callable headlessly."""
    raw=scene.get('wz09_cinematic_presentation_json')
    if not raw:return None
    key=scene.as_pointer()
    if key in _BUSY:return None
    _BUSY.add(key)
    try:
        from mathutils import Vector
        from bpy_extras.object_utils import world_to_camera_view
        config=json.loads(raw);document=json.loads(scene['wz09_cinematics_json'])
        if config['signature']!=presentation_signature(document):raise ValueError('Cinematic presentation changed: rebuild presentation')
        time=min(document['duration_s'],max(0.,(scene.frame_current+scene.frame_subframe-document['frame_start'])/document['fps']))
        current=active_camera_id(document,time);fade=core.screen_fade_sample(document,time)
        poi_cameras=validate_poi_camera_ids(config.get('poi_camera_ids'),{c['id'] for c in document['cameras']})
        enabled=bool(scene.get('wz09_cinematic_presentation_enabled',True))
        cached=_index(scene,document,config);sources=cached['sources']
        width=scene.render.resolution_x*scene.render.resolution_percentage/100
        height=scene.render.resolution_y*scene.render.resolution_percentage/100
        count=0
        for obj in cached['helpers']:
            role=obj['wz09_cinematic_presentation_role']
            _check_update_target(obj,document['id'])
            if role=='cloud':_set_rna_if_changed(obj,'hide_render',not enabled);continue
            camera=obj.parent
            if camera is None or camera.type!='CAMERA':raise ValueError('Cinematic screen helper camera detached')
            uid=str(camera['wz09_cinematic_camera_id']);is_current=uid==current
            depth=max(.02,float(camera.data.clip_start)*2)
            frame=list(camera.data.view_frame(scene=scene))
            if camera.data.type=='ORTHO':xs=[v.x for v in frame];ys=[v.y for v in frame]
            elif camera.data.type=='PERSP':
                xs=[v.x*depth/-v.z for v in frame];ys=[v.y*depth/-v.z for v in frame]
            else:raise ValueError('Cinematic presentation supports perspective/orthographic cameras only')
            xmin,xmax=min(xs),max(xs);ymin,ymax=min(ys),max(ys)
            if role=='fade':
                if len(obj.data.vertices)!=4:raise ValueError('Cinematic fade helper geometry changed')
                cx=(xmin+xmax)/2;cy=(ymin+ymax)/2
                geometry_changed=False
                for vertex,xy in zip(obj.data.vertices,[(xmin,ymin),(xmax,ymin),(xmax,ymax),(xmin,ymax)]):
                    geometry_changed=_set_rna_if_changed(vertex,'co',(cx+(xy[0]-cx)*1.02,cy+(xy[1]-cy)*1.02,-depth)) or geometry_changed
                if geometry_changed:obj.data.update()
                _set_material(obj.data.materials[0],fade['color'],fade['alpha'])
                _set_rna_if_changed(obj,'hide_render',not enabled or not is_current or fade['alpha']<=1e-6)
            else:
                source=sources[obj['wz09_poi_source_id']];profile=poi_labels.marker_profile(source)
                position=list(source.matrix_world.translation);eye=list(camera.matrix_world.translation)
                projected=list(world_to_camera_view(scene,camera,source.matrix_world.translation+Vector(profile['offset_m'])))
                layout=label_layout(profile,position,eye,projected,width,height)
                _set_rna_if_changed(obj,'hide_render',(not enabled or not is_current or not layout['visible']
                                 or poi_cameras is not None and uid not in poi_cameras))
                _set_rna_if_changed(obj.data,'body',str(source.get('wz09_poi_name',source.name)))
                _set_rna_if_changed(obj.data,'align_x',profile['alignment'])
                # Labels sit behind the fade plane at twice its camera depth.
                scale=1. if camera.data.type=='ORTHO' else 2.
                _set_rna_if_changed(obj,'location',((xmin+(xmax-xmin)*projected[0])*scale,
                              (ymin+(ymax-ymin)*projected[1])*scale,-depth*2))
                _set_rna_if_changed(obj.data,'size',(ymax-ymin)*scale*profile['text_size_px']/height)
                _set_material(obj.data.materials[0],profile['color'],layout['alpha'])
                if not obj.hide_render:count+=1
        if 'wz09_cinematic_presentation_error' in scene:del scene['wz09_cinematic_presentation_error']
        return {'time_s':time,'camera_id':current,'fade':fade,'rendered_poi_count':count,
                'runtime_integrated':False,'player_control_transferred':False}
    except (ValueError,TypeError,KeyError,ReferenceError) as exc:
        _INDEX.pop(key,None)
        scene['wz09_cinematic_presentation_error']=str(exc)[:300]
        raise
    finally:_BUSY.discard(key)


def _frame(scene,*args):
    try:update_scene(scene)
    except (ValueError,TypeError,KeyError,ReferenceError):pass


def _loaded(*args):
    import bpy
    _INDEX.clear()
    for scene in bpy.data.scenes:_frame(scene)


def _draw():
    import bpy,gpu
    from gpu_extras.batch import batch_for_shader
    from bpy_extras.view3d_utils import location_3d_to_region_2d
    ctx=bpy.context;scene=ctx.scene
    if (not ctx.region_data or ctx.region_data.view_perspective!='CAMERA'
            or not scene.get('wz09_cinematic_presentation_enabled',False)
            or not scene.get('wz09_cinematic_presentation_json') or scene.camera is None):return
    try:
        config=json.loads(scene['wz09_cinematic_presentation_json']);doc=json.loads(scene['wz09_cinematics_json'])
        if config['signature']!=presentation_signature(doc):return
        time=min(doc['duration_s'],max(0.,(scene.frame_current+scene.frame_subframe-doc['frame_start'])/doc['fps']))
        fade=core.screen_fade_sample(doc,time)
        if fade['alpha']<=1e-6:return
        points=[location_3d_to_region_2d(ctx.region,ctx.region_data,scene.camera.matrix_world@p)
                for p in scene.camera.data.view_frame(scene=scene)]
        if any(p is None for p in points):return
        xmin=min(p.x for p in points);xmax=max(p.x for p in points);ymin=min(p.y for p in points);ymax=max(p.y for p in points)
        shader=gpu.shader.from_builtin('UNIFORM_COLOR')
        batch=batch_for_shader(shader,'TRIS',{'pos':[(xmin,ymin),(xmax,ymin),(xmax,ymax),(xmin,ymin),(xmax,ymax),(xmin,ymax)]})
        previous=gpu.state.blend_get();gpu.state.blend_set('ALPHA')
        try:shader.bind();shader.uniform_float('color',(*fade['color'],fade['alpha']));batch.draw(shader)
        finally:gpu.state.blend_set(previous)
    except (ValueError,TypeError,KeyError,ReferenceError):pass


def register():
    global _HANDLE
    import bpy
    if _HANDLE is None:_HANDLE=bpy.types.SpaceView3D.draw_handler_add(_draw,(),'WINDOW','POST_PIXEL')
    for group,function in ((bpy.app.handlers.frame_change_post,_frame),(bpy.app.handlers.load_post,_loaded),
                           (bpy.app.handlers.undo_post,_loaded),(bpy.app.handlers.redo_post,_loaded)):
        function=bpy.app.handlers.persistent(function)
        if function not in group:group.append(function);_HANDLERS.append((group,function))


def unregister():
    global _HANDLE
    import bpy
    for group,function in _HANDLERS:
        if function in group:group.remove(function)
    _HANDLERS.clear();_BUSY.clear();_INDEX.clear()
    if _HANDLE is not None:bpy.types.SpaceView3D.draw_handler_remove(_HANDLE,'WINDOW');_HANDLE=None
