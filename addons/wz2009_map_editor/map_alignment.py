"""Whole-map placement and a shared editor water datum; no runtime physics."""
import json
import hashlib
import math
import uuid

_CLASSES=[]
_ITEMS=[]
WATER='wz09_alignment_water_reference'

def maps(scene):
    return sorted((o for o in scene.objects if o.get('wz09_type')=='map_instance'),
                  key=lambda o:(str(o.get('wz09_display_name',o.get('wz09_source_map','Map'))),str(o['wz09_id'])))

def selected_map(ctx):
    identity=ctx.scene.wz09_alignment_map
    matches=[o for o in maps(ctx.scene) if str(o['wz09_id'])==identity]
    if len(matches)!=1:raise ValueError('Choose a map in Map height and water')
    return matches[0]

def set_world_height(root,height):
    height=float(height)
    if not math.isfinite(height):raise ValueError('Map height must be finite')
    if root.library or root.constraints or root.animation_data:
        raise ValueError('Map root must be local, unanimated and unconstrained')
    matrix=root.matrix_world.copy();matrix.translation.z=height;root.matrix_world=matrix

def select_whole(ctx,root):
    import bpy
    if ctx.mode!='OBJECT':raise ValueError('Return to Object Mode first')
    bpy.ops.object.select_all(action='DESELECT')
    root.hide_set(False);root.hide_select=False;root.select_set(True)
    ctx.view_layer.objects.active=root

def map_items(self,ctx):
    global _ITEMS
    if ctx is None:return _ITEMS
    rows=[(str(o['wz09_id']),str(o.get('wz09_display_name',o.get('wz09_source_map','Map')))+
           (' (fragment)' if o.get('wz09_original_map_instance_id') else ''),str(o.get('wz09_source_map','')),0,_enum_number(str(o['wz09_id'])))
          for o in maps(ctx.scene)]
    if rows!=_ITEMS:_ITEMS=rows
    return _ITEMS

def _enum_number(identity):
    return int.from_bytes(hashlib.sha256(identity.encode()).digest()[:4],'big')&0x7fffffff

def map_get(scene):
    roots=maps(scene);identity=scene.get('wz09_alignment_map_id')
    return _enum_number(identity) if identity else (_enum_number(str(roots[0]['wz09_id'])) if roots else 0)

def map_set(scene,value):
    matches=[o for o in maps(scene) if _enum_number(str(o['wz09_id']))==value]
    if len(matches)!=1:raise ValueError('Map selector identity missing or ambiguous')
    scene['wz09_alignment_map_id']=str(matches[0]['wz09_id'])

def map_changed(self,ctx):
    try:
        root=selected_map(ctx);self.wz09_alignment_target_z=root.matrix_world.translation.z
    except ValueError:pass

def water_reference(scene):
    found=[o for o in scene.objects if o.get(WATER)]
    if len(found)>1:raise ValueError('Duplicate shared water references; resolve before updating')
    return found[0] if found else None

def ensure_water(ctx):
    import bpy
    from . import terrain_workflow as terrain
    scene=ctx.scene
    if not math.isfinite(scene.wz09_sea_level):raise ValueError('Water level must be finite')
    helper=water_reference(scene)
    if helper is None:
        helper=terrain.create_water(ctx,'sea',activate=False)
        helper[WATER]=str(uuid.uuid4())
        helper['wz09_editor_overlay']=True
    # One existing sea record is the authoring datum; a four-face-free quad
    # preview does not require terrain generation or an ocean simulation.
    data=json.loads(helper['wz09_water_json'])
    data.update(level_m=float(scene.wz09_sea_level),extent_m=list(scene.wz09_sea_extent),carve_terrain=False)
    helper.location.z=scene.wz09_sea_level
    data['position_m']=list(helper.location)
    helper['wz09_water_json']=json.dumps(data,sort_keys=True)
    helper.hide_select=True;helper.show_in_front=False
    surfaces=[o for o in helper.children if o.get('wz09_alignment_surface')==helper[WATER]]
    if len(surfaces)>1:raise ValueError('Duplicate shared water preview surfaces')
    if surfaces:surface=surfaces[0]
    else:
        mesh=bpy.data.meshes.new('Shared water reference')
        mesh.from_pydata([(-.5,-.5,0),(.5,-.5,0),(.5,.5,0),(-.5,.5,0)],[],[(0,1,2,3)])
        surface=bpy.data.objects.new('Water level reference - visual preview',mesh)
        scene.collection.objects.link(surface);surface.parent=helper
        surface['wz09_alignment_surface']=helper[WATER]
        surface['wz09_editor_overlay']=True;surface['wz09_preview_only']=True
        material=terrain._water_material();surface.data.materials.append(material)
    surface.scale=(scene.wz09_sea_extent[0],scene.wz09_sea_extent[1],1)
    surface.hide_select=True;surface.hide_render=True
    surface.hide_set(not scene.wz09_alignment_water_visible)
    helper.hide_set(True)
    return helper

def fit_water(ctx):
    from mathutils import Vector
    points=[o.matrix_world@Vector(p) for o in ctx.scene.objects if o.get('wz09_overview_map_id') for p in o.bound_box]
    if not points:raise ValueError('No map overview bounds available')
    low=[min(p[i] for p in points) for i in (0,1)];high=[max(p[i] for p in points) for i in (0,1)]
    ctx.scene.wz09_sea_extent=[max(high[i]-low[i]+400,100) for i in (0,1)]
    helper=ensure_water(ctx);helper.location.x=(low[0]+high[0])/2;helper.location.y=(low[1]+high[1])/2
    ensure_water(ctx)
    return helper

def draw(layout,ctx):
    import textwrap
    def help_text(target,message):
        width=max(22,int((getattr(ctx.region,'width',320)-50)/7))
        for line in textwrap.wrap(message,width):target.label(text=line)
    scene=ctx.scene;box=layout.box();box.label(text='Map height and shared water',icon='ORIENTATION_GLOBAL')
    box.prop(scene,'wz09_alignment_map',text='Map')
    box.operator('wz09.alignment_use_selected',text='Use map from selected part',icon='EYEDROPPER')
    try:
        root=selected_map(ctx)
        box.label(text=f'Current root Z: {root.matrix_world.translation.z:.3f}')
        box.operator('wz09.alignment_select',text='Select whole map - G / R',icon='OBJECT_ORIGIN')
        box.prop(scene,'wz09_alignment_target_z',text='Target root Z')
        box.operator('wz09.alignment_height',text='Apply map height').action='SET'
        box.prop(scene,'wz09_alignment_step',text='Height step')
        row=box.row(align=True)
        row.operator('wz09.alignment_height',text='Lower map').action='DOWN'
        row.operator('wz09.alignment_height',text='Raise map').action='UP'
        box.prop(scene,'wz09_alignment_offset',text='Root offset above water')
        box.operator('wz09.alignment_height',text='Set root to water + offset').action='WATER'
    except ValueError:pass
    help_text(box,'Moves the persistent map root, including all parts, props, previews and loaded outskirts. Z uses Blender world units. The root is a placement pivot, not the ground height.')
    box.prop(scene,'wz09_sea_level',text='Water Z')
    box.prop(scene,'wz09_sea_extent',text='Water width / length')
    box.prop(scene,'wz09_alignment_water_visible',text='Show water preview')
    box.operator('wz09.alignment_water',text='Create / update water reference').fit=False
    box.operator('wz09.alignment_water',text='Fit water around current maps').fit=True
    help_text(box,'Apply after changing water settings. Flat, non-selectable editor reference; no Ultra-Water physics. Map heights are never changed automatically.')

def register():
    import bpy
    from bpy.props import EnumProperty,FloatProperty,BoolProperty
    class WZ09_OT_alignment_use_selected(bpy.types.Operator):
        bl_idname='wz09.alignment_use_selected';bl_label='Use selected map'
        bl_description='Choose the owning map from a selected part or overview, using its persistent map ID'
        def execute(self,ctx):
            from .city_editing import map_root
            try:
                root=map_root(ctx.active_object);ctx.scene.wz09_alignment_map=str(root['wz09_id'])
                ctx.scene.wz09_alignment_target_z=root.matrix_world.translation.z
                return {'FINISHED'}
            except ValueError as exc:self.report({'ERROR'},str(exc));return {'CANCELLED'}
    class WZ09_OT_alignment_select(bpy.types.Operator):
        bl_idname='wz09.alignment_select';bl_label='Select whole map';bl_options={'UNDO'}
        bl_description='Select only the map parent. G moves and R rotates every child, even after splitting'
        def execute(self,ctx):
            try:select_whole(ctx,selected_map(ctx));return {'FINISHED'}
            except ValueError as exc:self.report({'ERROR'},str(exc));return {'CANCELLED'}
    class WZ09_OT_alignment_height(bpy.types.Operator):
        bl_idname='wz09.alignment_height';bl_label='Adjust whole map height';bl_options={'REGISTER','UNDO'}
        bl_description='Move the map pivot in world Z without rebuilding or reloading its assets'
        action:EnumProperty(items=[(v,v,'') for v in ('SET','UP','DOWN','WATER')])
        @classmethod
        def poll(cls,ctx):return ctx.mode=='OBJECT'
        def execute(self,ctx):
            try:
                root=selected_map(ctx);s=ctx.scene
                height={'SET':s.wz09_alignment_target_z,'UP':root.matrix_world.translation.z+s.wz09_alignment_step,
                        'DOWN':root.matrix_world.translation.z-s.wz09_alignment_step,
                        'WATER':s.wz09_sea_level+s.wz09_alignment_offset}[self.action]
                set_world_height(root,height);s.wz09_alignment_target_z=height
                return {'FINISHED'}
            except ValueError as exc:self.report({'ERROR'},str(exc));return {'CANCELLED'}
    class WZ09_OT_alignment_water(bpy.types.Operator):
        bl_idname='wz09.alignment_water';bl_label='Update shared water reference';bl_options={'REGISTER','UNDO'}
        bl_description='Create or update a flat visual sea datum using existing terrain-water records; no simulation or terrain carving'
        fit:BoolProperty(default=False)
        def execute(self,ctx):
            try:
                fit_water(ctx) if self.fit else ensure_water(ctx)
                self.report({'INFO'},'Water reference updated; map heights unchanged')
                return {'FINISHED'}
            except (ValueError,RuntimeError) as exc:self.report({'ERROR'},str(exc));return {'CANCELLED'}
    for cls in (WZ09_OT_alignment_use_selected,WZ09_OT_alignment_select,WZ09_OT_alignment_height,WZ09_OT_alignment_water):
        bpy.utils.register_class(cls);_CLASSES.append(cls)
    bpy.types.Scene.wz09_alignment_map=EnumProperty(name='Map',items=map_items,get=map_get,set=map_set,update=map_changed)
    bpy.types.Scene.wz09_alignment_target_z=FloatProperty(name='Target root Z',description='Absolute world Z of the map pivot, in Blender units')
    bpy.types.Scene.wz09_alignment_step=FloatProperty(name='Height step',default=1,min=.001)
    bpy.types.Scene.wz09_alignment_offset=FloatProperty(name='Root offset above water',default=0)
    bpy.types.Scene.wz09_alignment_water_visible=BoolProperty(name='Show water preview',default=True)

def unregister():
    import bpy
    for cls in reversed(_CLASSES):bpy.utils.unregister_class(cls)
    _CLASSES.clear()
    for field in ('map','target_z','step','offset','water_visible'):
        name='wz09_alignment_'+field
        if hasattr(bpy.types.Scene,name):delattr(bpy.types.Scene,name)
