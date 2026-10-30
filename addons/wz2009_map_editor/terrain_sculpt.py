"""Bounded editable heightmap controls over owned generated terrain only.

Native/source meshes are never modified. All grid edits use heightfield's
protected-core, water and border locks; rebuilding uses the existing workflow.
"""
import copy
import datetime
import hashlib
import json
import math
from pathlib import Path
import struct
import uuid

try:
    from . import heightfield
except ImportError:
    import heightfield

ROOT=Path(__file__).resolve().parents[2]
KEY='wz09_heightfield_authoring_json'
SUMMARY='wz09_heightfield_summary_json'
_CLASSES=[]


def _workflow():
    try:from . import terrain_workflow
    except ImportError:import terrain_workflow
    return terrain_workflow


def settings(scene,regions=None,waters=None,source_settings=None):
    """Return the saved layer; supplied current inputs also enforce freshness."""
    raw=scene.get(KEY)
    if not raw:return None
    try:
        grid=heightfield.validate_grid(json.loads(raw))
    except (ValueError,TypeError) as exc:
        raise ValueError('Saved heightmap is invalid') from exc
    if source_settings is not None:
        expected=heightfield.fingerprint(regions,waters,source_settings)
        if grid['source_sha256']!=expected:
            raise ValueError('Heightmap outdated: profile, cores, masks or water changed; recapture it')
    return grid


def _inputs(context,workflow=None):
    workflow=workflow or _workflow()
    regions,waters,source=workflow._inputs(context.scene)
    # Recapture must always sample the current base, never its own old layer.
    source={key:value for key,value in source.items() if key!='heightfield_authoring'}
    return workflow,regions,waters,source


def _summary(grid):
    return {'id':grid['id'],'width':grid['width'],'height':grid['height'],
            'locked_samples':sum(grid['locked']),'operations':len(grid['operations']),
            'source_sha256':grid['source_sha256']}


def _store(context,grid,*,workflow=None,regenerate=None):
    scene=context.scene
    old={key:scene.get(key) for key in (KEY,SUMMARY)}
    if grid is None:
        for key in (KEY,SUMMARY):
            if key in scene:del scene[key]
    else:
        heightfield.validate_grid(grid)
        scene[KEY]=json.dumps(grid,sort_keys=True,separators=(',',':'),allow_nan=False)
        scene[SUMMARY]=json.dumps(_summary(grid),sort_keys=True)
    if regenerate is None:
        regenerate=bool(scene.wz09_terrain_sculpt.auto_regenerate)
    try:
        if regenerate:(workflow or _workflow()).generate_terrain(context)
    except Exception:
        # Existing generate_terrain removes its failed owned revision itself.
        for key,value in old.items():
            if value is None:
                if key in scene:del scene[key]
            else:scene[key]=value
        raise
    return grid


def capture_current(context,*,workflow=None):
    workflow,regions,waters,source=_inputs(context,workflow)
    controls=context.scene.wz09_terrain_sculpt
    field=workflow._field(regions,waters,source)
    grid=heightfield.capture(field,list(source['bounds_m']),controls.width,controls.height,
                            heightfield.fingerprint(regions,waters,source))
    return _store(context,grid,workflow=workflow,regenerate=False)


def _current(context,workflow=None):
    workflow,regions,waters,source=_inputs(context,workflow)
    grid=settings(context.scene,regions,waters,source)
    if grid is None:raise ValueError('Capture a heightmap from the current terrain first')
    return workflow,grid


def brush_at_cursor(context,*,workflow=None):
    workflow,grid=_current(context,workflow)
    s=context.scene.wz09_terrain_sculpt
    result=heightfield.brush(grid,list(context.scene.cursor.location)[:2],s.radius_m,s.strength,
                             s.mode,target_height=s.target_height_m if s.mode=='flatten' else None)
    return _store(context,result,workflow=workflow)


def thermal_erosion(context,*,workflow=None):
    workflow,grid=_current(context,workflow)
    s=context.scene.wz09_terrain_sculpt
    result=heightfield.erode(grid,s.iterations,talus_deg=s.talus_deg,rate=s.erosion_rate)
    return _store(context,result,workflow=workflow)


def _image_path(path,bpy_module=None):
    if str(path).startswith('//'):
        if bpy_module is None:import bpy as bpy_module
        path=bpy_module.path.abspath(str(path))
    return Path(path).expanduser().resolve()


def image_samples(path,width,height,*,flip_y=False,bpy_module=None):
    """Read an independently loaded temporary image, scaled within 513² cells."""
    if bpy_module is None:import bpy as bpy_module
    path=_image_path(path,bpy_module)
    if path.suffix.lower() not in {'.png','.tif','.tiff','.exr'}:
        raise ValueError('Heightmap source must be PNG, TIFF or EXR')
    if not path.is_file():raise ValueError('Heightmapbild fehlt')
    width=heightfield.core._integer(width,'width',2,513)
    height=heightfield.core._integer(height,'height',2,513)
    temporary=bpy_module.data.images.load(str(path),check_existing=False)
    try:
        if temporary.size[0]<2 or temporary.size[1]<2 or max(temporary.size)>8192:
            raise ValueError('Heightmap image requires 2-8192 pixels per axis')
        temporary.colorspace_settings.name='Non-Color'
        temporary.scale(width,height)
        pixels=temporary.pixels[:]
        count=width*height
        channels=len(pixels)//count
        if channels not in (1,2,3,4) or len(pixels)!=count*channels:
            raise ValueError('Heightmap image has no complete pixel grid')
        values=[]
        for y in range(height):
            source_y=height-1-y if flip_y else y
            for x in range(width):
                value=float(pixels[(source_y*width+x)*channels])
                if not math.isfinite(value):raise ValueError('Heightmap image contains non-finite heights')
                values.append(value)
        return values
    finally:
        bpy_module.data.images.remove(temporary)


def apply_image(context,*,workflow=None,bpy_module=None):
    workflow,grid=_current(context,workflow)
    s=context.scene.wz09_terrain_sculpt
    scale=heightfield.core._number(s.image_scale_m,'heightmap image scale')
    offset=heightfield.core._number(s.image_offset_m,'heightmap image offset')
    values=image_samples(s.image_path,grid['width'],grid['height'],flip_y=s.image_flip_y,bpy_module=bpy_module)
    result=copy.deepcopy(grid)
    changed=0
    for index,value in enumerate(values):
        if grid['locked'][index]:continue
        value=heightfield.core._number(offset+value*scale,'imported height')
        changed+=value!=grid['samples'][index]
        result['samples'][index]=value
    source_path=_image_path(s.image_path,bpy_module)
    source_sha=hashlib.sha256()
    with source_path.open('rb') as stream:
        for chunk in iter(lambda:stream.read(1024*1024),b''):source_sha.update(chunk)
    result['operations'].append({'kind':'height_image','source_path':str(source_path),
        'source_sha256':source_sha.hexdigest(),
        'scale_m':scale,'offset_m':offset,'flip_y':bool(s.image_flip_y),
        'channel':'red_or_grayscale','changed_samples':changed})
    return _store(context,heightfield.validate_grid(result),workflow=workflow)


def discard(context,*,workflow=None):
    return _store(context,None,workflow=workflow)


def export_heightmap(context,*,workflow=None,output_root=None):
    _,grid=_current(context,workflow)
    export_root=(ROOT/'exports').resolve()
    destination=Path(output_root).resolve() if output_root is not None else export_root
    if not destination.is_relative_to(export_root):
        raise ValueError('Heightmap export stays inside the editor export folder')
    identity=str(uuid.uuid4())
    stamp=datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%dT%H%M%S')
    folder=destination/('heightmap-'+stamp+'-'+identity)
    samples=struct.pack('<'+str(len(grid['samples']))+'f',*grid['samples'])
    locks=bytes(grid['locked'])
    manifest={key:value for key,value in grid.items() if key not in ('samples','locked')}
    manifest.update(product_kind='wz09_heightfield_authoring_export',export_id=identity,
        coordinate_space='world_editor_design_meters',physical_scale_verified=False,collision_verified=False,
        sample_encoding='float32_le',sample_order='rows_along_positive_y_columns_along_positive_x',
        heights={'path':'heightmap.f32le.bin','sha256':hashlib.sha256(samples).hexdigest(),'bytes':len(samples)},
        lock_mask={'path':'locked.u8.bin','sha256':hashlib.sha256(locks).hexdigest(),'encoding':'uint8_0_editable_1_locked','bytes':len(locks)})
    payload=(json.dumps(manifest,indent=2,ensure_ascii=False,allow_nan=False)+'\n').encode('utf-8')
    folder.mkdir(parents=True,exist_ok=False)
    for name,data in (('heightmap.f32le.bin',samples),('locked.u8.bin',locks),('heightmap.json',payload)):
        with (folder/name).open('xb') as stream:stream.write(data)
    return folder/'heightmap.json'


def draw(layout,context):
    s=context.scene.wz09_terrain_sculpt
    row=layout.row(align=True);row.prop(s,'width');row.prop(s,'height')
    layout.operator('wz09.heightfield_capture',icon='MESH_GRID')
    raw=context.scene.get(SUMMARY)
    if raw:
        try:
            info=json.loads(raw)
            layout.label(text=f"{info['width']} × {info['height']} · {info['operations']} steps - {info['locked_samples']} protected")
        except (ValueError,KeyError):layout.label(text='Check saved height layer',icon='ERROR')
    layout.prop(s,'auto_regenerate')
    box=layout.box();box.label(text='Brush at 3D cursor')
    box.prop(s,'mode');box.prop(s,'radius_m');box.prop(s,'strength')
    if s.mode=='flatten':box.prop(s,'target_height_m')
    box.operator('wz09.heightfield_brush',icon='BRUSH_DATA')
    box=layout.box();box.label(text='Thermal erosion')
    box.prop(s,'iterations');box.prop(s,'talus_deg');box.prop(s,'erosion_rate')
    box.operator('wz09.heightfield_erode',icon='MOD_SMOOTH')
    box=layout.box();box.label(text='Heightmap image - red/grayscale')
    box.prop(s,'image_path');box.prop(s,'image_scale_m');box.prop(s,'image_offset_m');box.prop(s,'image_flip_y')
    box.operator('wz09.heightfield_image',icon='IMAGE_DATA')
    row=layout.row(align=True);row.operator('wz09.heightfield_export',icon='EXPORT');row.operator('wz09.heightfield_discard',icon='TRASH')
    layout.label(text='Map cores, water and grid boundary remain protected',icon='LOCKED')


def register():
    import bpy
    from bpy.props import BoolProperty,EnumProperty,FloatProperty,IntProperty,PointerProperty,StringProperty
    class Settings(bpy.types.PropertyGroup):
        width:IntProperty(name='Grid X',default=129,min=2,max=513)
        height:IntProperty(name='Grid Y',default=129,min=2,max=513)
        mode:EnumProperty(name='Tool',items=[('raise','Anheben',''),('lower','Absenken',''),('smooth','Smooth',''),('flatten','Flatten','')])
        radius_m:FloatProperty(name='Radius',default=80,min=.001,subtype='DISTANCE')
        strength:FloatProperty(name='Strength / height amount',default=.5,min=0,description='Raise/lower in meters; smooth/flatten as a fraction from 0 to 1')
        target_height_m:FloatProperty(name='Target height',default=0,subtype='DISTANCE')
        iterations:IntProperty(name='Steps',default=12,min=1,max=100)
        talus_deg:FloatProperty(name='Angle of repose',default=32,min=0,max=88.9)
        erosion_rate:FloatProperty(name='Transport rate',default=.25,min=.001,max=.5)
        auto_regenerate:BoolProperty(name='Regenerate terrain after editing',default=True)
        image_path:StringProperty(name='PNG / TIFF / EXR',subtype='FILE_PATH')
        image_scale_m:FloatProperty(name='Height per image value',default=100,subtype='DISTANCE')
        image_offset_m:FloatProperty(name='Height offset',default=0,subtype='DISTANCE')
        image_flip_y:BoolProperty(name='Flip image vertically',default=False)

    def operator(name,label,action,undo=True):
        def execute(self,context):
            try:
                result=action(context)
                self.report({'INFO'},str(result)[:180] if isinstance(result,Path) else 'Height layer updated')
                return {'FINISHED'}
            except (ValueError,RuntimeError,KeyError,TypeError,OSError) as exc:
                self.report({'ERROR'},str(exc)[:220]);return {'CANCELLED'}
        return type('WZ09_OT_'+name,(bpy.types.Operator,),{'__module__':__name__,'bl_idname':'wz09.'+name,
            'bl_label':label,'bl_options':{'REGISTER','UNDO'} if undo else {'REGISTER'},'execute':execute})

    class WZ09_PT_terrain_sculpt(bpy.types.Panel):
        bl_label='Terrain - height and erosion';bl_idname='WZ09_PT_terrain_sculpt';bl_parent_id='WZ09_PT_main'
        bl_space_type='VIEW_3D';bl_region_type='UI';bl_category='WZ2009';bl_options={'DEFAULT_CLOSED'}
        @classmethod
        def poll(cls,context):return getattr(context.scene,'wz09_workspace','')=='WORLD'
        def draw(self,context):draw(self.layout,context)

    _CLASSES[:]=[Settings,operator('heightfield_capture','Aktuelles Terrain erfassen',capture_current),
        operator('heightfield_brush','Pinsel am Cursor anwenden',brush_at_cursor),
        operator('heightfield_erode','Erosion anwenden',thermal_erosion),
        operator('heightfield_image','Apply image heights',apply_image),
        operator('heightfield_export','Heightmap speichern',export_heightmap,False),
        operator('heightfield_discard','Discard height layer',discard),WZ09_PT_terrain_sculpt]
    for cls in _CLASSES:bpy.utils.register_class(cls)
    bpy.types.Scene.wz09_terrain_sculpt=PointerProperty(type=Settings)


def unregister():
    import bpy
    if hasattr(bpy.types.Scene,'wz09_terrain_sculpt'):del bpy.types.Scene.wz09_terrain_sculpt
    for cls in reversed(_CLASSES):bpy.utils.unregister_class(cls)
    _CLASSES.clear()
