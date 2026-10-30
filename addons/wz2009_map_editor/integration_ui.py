"""UI adapters for the existing local authoring, source and bake workflows."""
import datetime
import json
from pathlib import Path
import uuid

import bpy
from bpy.props import BoolProperty, EnumProperty, FloatProperty, StringProperty
from bpy_extras.io_utils import ImportHelper

from .authoring_helpers import resolve_owner_map


def _addon():
    import wz2009_map_editor
    return wz2009_map_editor


def _activate(ctx,obj):
    bpy.ops.object.select_all(action='DESELECT')
    obj.select_set(True);ctx.view_layer.objects.active=obj;ctx.view_layer.update()


class WZ09_OT_design_layout(bpy.types.Operator):
    bl_idname='wz09.design_layout';bl_label='Create 16-Map Design Scene';bl_options={'REGISTER','UNDO'}
    def execute(self,ctx):
        try:
            from .layout_geometry import create_design_scene
            root=_addon().project_root(ctx.scene)
            scene=create_design_scene(ctx,ctx.scene.wz09_layout_spacing_m,ctx.scene.wz09_layout_region_gap_m)
            ctx.window.scene=scene;scene.wz09_project_root=str(root)
            stamp=uuid.uuid4().hex[:10]
            scene.wz09_export_path=f'exports/layout-{stamp}/karvorsk.world.json'
            scene.wz09_tactical_path=f'exports/layout-{stamp}/karvorsk.tactical.svg'
            scene.wz09_geometry_path=f'exports/layout-{stamp}/connectors.glb'
            self.report({'INFO'},'New design-only proxy scene; no native map import or physical placement inferred')
            return {'FINISHED'}
        except (OSError,ValueError,RuntimeError) as exc:
            self.report({'ERROR'},str(exc)[:240]);return {'CANCELLED'}


class WZ09_OT_connector_template(bpy.types.Operator):
    bl_idname='wz09.connector_template';bl_label='Create Editable Geometry';bl_options={'REGISTER','UNDO'}
    def execute(self,ctx):
        try:
            from .layout_geometry import create_connector
            scene=ctx.scene
            obj=create_connector(ctx,scene.wz09_geometry_kind,length_m=scene.wz09_road_length,
                width_m=scene.wz09_road_width,height_m=scene.wz09_geometry_height_m,
                thickness_m=scene.wz09_geometry_thickness_m,between_selected=scene.wz09_connector_between)
            _activate(ctx,obj)
            self.report({'INFO'},'Editable visual geometry created; no native collider or navigation generated')
            return {'FINISHED'}
        except (ValueError,RuntimeError) as exc:
            self.report({'ERROR'},str(exc)[:240]);return {'CANCELLED'}


class WZ09_OT_mark_geometry(bpy.types.Operator):
    bl_idname='wz09.mark_custom_geometry';bl_label='Mark Selected Own Mesh/Curve';bl_options={'REGISTER','UNDO'}
    def execute(self,ctx):
        try:
            from .layout_geometry import mark_selected_custom_geometry
            objects=mark_selected_custom_geometry(ctx,kind=ctx.scene.wz09_geometry_kind)
            self.report({'INFO'},f'{len(objects)} local objects assigned to existing custom-GLB export')
            return {'FINISHED'}
        except (ValueError,RuntimeError) as exc:
            self.report({'ERROR'},str(exc)[:240]);return {'CANCELLED'}


class WZ09_OT_bind_portals(bpy.types.Operator):
    bl_idname='wz09.bind_connector_portals';bl_label='Bind Two Selected Portals to Active Connector';bl_options={'REGISTER','UNDO'}
    def execute(self,ctx):
        try:
            obj=ctx.active_object
            if obj is None or not obj.get('wz09_custom_geometry') or obj.get('wz09_preview_only'):
                raise ValueError('Keep an own connector active and select two portal nav nodes')
            nodes=[node for node in ctx.selected_objects if node.get('wz09_type')=='nav_node']
            if len(nodes)!=2:raise ValueError('Select exactly two portal nav nodes plus the active connector')
            for node in nodes:
                if json.loads(node.get('wz09_nav_properties_json','{}')).get('is_portal') is not True:
                    raise ValueError('Explicitly mark both nav nodes as portals first')
            nodes.sort(key=lambda node:node['wz09_id'])
            owners=[str(node.get('wz09_owner_map','')) for node in nodes]
            if not all(owners) or owners[0]==owners[1]:raise ValueError('Portal endpoints must belong to different maps')
            obj['wz09_connector_from']=owners[0];obj['wz09_connector_to']=owners[1]
            obj['wz09_connector_from_portal']=str(nodes[0]['wz09_id'])
            obj['wz09_connector_to_portal']=str(nodes[1]['wz09_id'])
            self.report({'INFO'},'Explicit portal references assigned; access is still proposed')
            return {'FINISHED'}
        except (ValueError,RuntimeError) as exc:
            self.report({'ERROR'},str(exc)[:240]);return {'CANCELLED'}


class WZ09_OT_source_status(bpy.types.Operator):
    bl_idname='wz09.inspect_offline_tools';bl_label='Check Local Source and Bake Tools'
    def execute(self,ctx):
        try:
            from .pipeline_workflow import inspect_native_source,inspect_recast_source
            root=_addon().project_root(ctx.scene)
            result={}
            for key,operation in (('native_export',lambda:inspect_native_source(root,ctx.scene.wz09_asset_root)),
                                  ('recast',lambda:inspect_recast_source(root))):
                try:result[key]=operation()
                except (OSError,ValueError,RuntimeError) as exc:result[key]={'status':'unavailable','error':str(exc)[:320]}
            ctx.scene['wz09_pipeline_report_json']=json.dumps(result,sort_keys=True)
            self.report({'INFO'},'Local tools inspected; no executable was started')
            return {'FINISHED'}
        except (OSError,ValueError,RuntimeError) as exc:
            self.report({'ERROR'},str(exc)[:240]);return {'CANCELLED'}


class WZ09_OT_native_export(bpy.types.Operator):
    bl_idname='wz09.export_native_preview';bl_label='Export Native Visual Map Offline'
    def execute(self,ctx):
        try:
            from .pipeline_workflow import run_native_export
            root=_addon().project_root(ctx.scene);owner=resolve_owner_map(ctx.active_object)
            code=owner['wz09_source_map'] if owner is not None else ctx.scene.wz09_map_choice
            result=run_native_export(root,code,ctx.scene.wz09_asset_root)
            ctx.scene['wz09_pipeline_report_json']=json.dumps(result,sort_keys=True)
            ctx.scene['wz09_last_native_preview']=result['reference_path']
            self.report({'INFO'},'Offline visual export written; select a map and use Import Last Export')
            return {'FINISHED'}
        except (OSError,ValueError,RuntimeError) as exc:
            self.report({'ERROR'},str(exc)[:240]);return {'CANCELLED'}


class WZ09_OT_import_last_preview(bpy.types.Operator):
    bl_idname='wz09.import_last_preview';bl_label='Import Last Offline Export';bl_options={'REGISTER','UNDO'}
    def execute(self,ctx):
        try:
            path=ctx.scene.get('wz09_last_native_preview','')
            if not path:raise ValueError('No offline export recorded in this scene')
            return bpy.ops.wz09.import_visual_gltf(filepath=path)
        except (OSError,ValueError,RuntimeError) as exc:
            self.report({'ERROR'},str(exc)[:240]);return {'CANCELLED'}


class WZ09_OT_bake_reference(bpy.types.Operator):
    bl_idname='wz09.bake_reference';bl_label='Bake Selected Own Geometry (Recast Experiment)';bl_options={'REGISTER','UNDO'}
    def execute(self,ctx):
        try:
            from .pipeline_workflow import bake_reference
            result=bake_reference(ctx,_addon().project_root(ctx.scene),list(ctx.selected_objects))
            ctx.scene['wz09_pipeline_report_json']=json.dumps(result['manifest'],sort_keys=True)
            self.report({'INFO'},'Experimental reference overlay created; not a game collider or runtime navgraph')
            return {'FINISHED'}
        except (OSError,ValueError,RuntimeError) as exc:
            self.report({'ERROR'},str(exc)[:240]);return {'CANCELLED'}


class WZ09_OT_inspect_manifest(bpy.types.Operator,ImportHelper):
    bl_idname='wz09.inspect_manifest';bl_label='Inspect World JSON (Read-only)'
    filename_ext='.json';filter_glob:StringProperty(default='*.json',options={'HIDDEN'})
    def execute(self,ctx):
        try:
            from .validation import report
            path=Path(self.filepath).resolve()
            document=json.loads(path.read_text(encoding='utf-8-sig'),parse_constant=lambda value:(_ for _ in ()).throw(ValueError('Nonfinite JSON')))
            result=report(document,base_dir=path.parent)
            result['inspected_manifest']=str(path)
            ctx.scene['wz09_preflight_json']=json.dumps(result,sort_keys=True)
            self.report({'INFO'} if result['ok'] else {'WARNING'},'Manifest inspected; see full validation report below')
            return {'FINISHED'}
        except (OSError,ValueError,RuntimeError) as exc:
            self.report({'ERROR'},str(exc)[:240]);return {'CANCELLED'}


class WZ09_OT_save_version(bpy.types.Operator):
    bl_idname='wz09.save_version';bl_label='Save New Editor Version'
    def execute(self,ctx):
        try:
            from .scene_io import safe_output_path
            root=_addon().project_root(ctx.scene)
            stamp=datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%dT%H%M%S')+'-'+uuid.uuid4().hex[:6]
            target=safe_output_path(root/'blender_projects'/f'WZ2009_EDIT-{stamp}.blend',root=root)
            if target.exists():raise FileExistsError('Version already exists')
            target.parent.mkdir(parents=True,exist_ok=True)
            return bpy.ops.wm.save_as_mainfile(filepath=str(target),check_existing=False)
        except (OSError,ValueError,RuntimeError) as exc:
            self.report({'ERROR'},str(exc)[:240]);return {'CANCELLED'}


class WZ09_PT_integrated(bpy.types.Panel):
    bl_label='Layout, Geometry and Offline Tools';bl_idname='WZ09_PT_integrated'
    bl_space_type='VIEW_3D';bl_region_type='UI';bl_category='WZ2009';bl_parent_id='WZ09_PT_main'
    @classmethod
    def poll(cls,ctx):return getattr(ctx.scene,'wz09_workspace','GEOMETRY') in {'MAPS','GEOMETRY'}
    bl_options={'DEFAULT_CLOSED'}
    def draw(self,ctx):
        layout=self.layout;s=ctx.scene
        if s.wz09_workspace=='MAPS':
            box=layout.box();box.label(text='16-map design layout',icon='WORLD')
            box.prop(s,'wz09_layout_spacing_m');box.prop(s,'wz09_layout_region_gap_m')
            box.operator('wz09.design_layout',icon='ADD')
            box=layout.box();box.label(text='Local source tools',icon='TOOL_SETTINGS')
            box.prop(s,'wz09_asset_root');box.operator('wz09.inspect_offline_tools',icon='VIEWZOOM')
            box.operator('wz09.export_native_preview',icon='PACKAGE');box.operator('wz09.import_last_preview',icon='IMPORT')
        else:
            box=layout.box();box.label(text='Own editable geometry',icon='MESH_CUBE')
            box.prop(s,'wz09_geometry_kind');box.prop(s,'wz09_geometry_height_m');box.prop(s,'wz09_geometry_thickness_m')
            box.prop(s,'wz09_connector_between');box.operator('wz09.connector_template',icon='ADD')
            box.operator('wz09.mark_custom_geometry',icon='CHECKMARK');box.operator('wz09.bind_connector_portals',icon='LINKED')
            box.operator('wz09.bake_reference',icon='MOD_TRIANGULATE')
        if s.get('wz09_pipeline_report_json'):
            try:report=json.loads(s['wz09_pipeline_report_json'])
            except (ValueError,TypeError):report={}
            for key in ('status','kind'):
                if key in report:layout.label(text=f'{key}: {str(report[key])[:60]}')


CLASSES=(WZ09_OT_design_layout,WZ09_OT_connector_template,WZ09_OT_mark_geometry,WZ09_OT_bind_portals,
    WZ09_OT_source_status,WZ09_OT_native_export,WZ09_OT_import_last_preview,WZ09_OT_bake_reference,
    WZ09_OT_inspect_manifest,WZ09_OT_save_version,WZ09_PT_integrated)
PROPS={
    'wz09_layout_spacing_m':lambda:FloatProperty(name='Design map spacing (m)',default=180,min=1),
    'wz09_layout_region_gap_m':lambda:FloatProperty(name='Design region gap (m)',default=700,min=1),
    'wz09_geometry_kind':lambda:EnumProperty(name='Own geometry type',items=[(key,key.title(),key) for key in ('road','bridge','corridor','tunnel','building')],default='road'),
    'wz09_geometry_height_m':lambda:FloatProperty(name='Clear height / building height (m)',default=3,min=.1),
    'wz09_geometry_thickness_m':lambda:FloatProperty(name='Wall/deck thickness (m)',default=.25,min=.01),
    'wz09_connector_between':lambda:BoolProperty(name='Between two selected map/nav points',default=False),
    'wz09_asset_root':lambda:StringProperty(name='Read-only MW2 assets',subtype='DIR_PATH',default=r'C:\Program Files (x86)\Steam\steamapps\common\Call of Duty Modern Warfare 2'),
    'wz09_aircraft_route_id':lambda:StringProperty(name='Flight route ID',default='ac130_route_01'),
    'wz09_aircraft_interval_s':lambda:FloatProperty(name='Keyframe interval (s)',default=5,min=.001),
    'wz09_nav_max_distance_m':lambda:FloatProperty(name='Warn edge distance above (m)',default=40,min=.01),
    'wz09_nav_max_delta_z_m':lambda:FloatProperty(name='Warn edge height above (m)',default=3,min=.01),
}


def register():
    for key,factory in PROPS.items():setattr(bpy.types.Scene,key,factory())
    for cls in CLASSES:bpy.utils.register_class(cls)


def unregister():
    for cls in reversed(CLASSES):bpy.utils.unregister_class(cls)
    for key in PROPS:
        if hasattr(bpy.types.Scene,key):delattr(bpy.types.Scene,key)
