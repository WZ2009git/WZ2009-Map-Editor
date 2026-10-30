"""Native Blender Asset Browser backed by the existing WZ source catalog.

Collection assets share the existing meshes/materials. Native drag and drop
instances are converted through place_asset, preserving its source/owner/UUID
contract. No second asset database or native reimport is introduced.
"""
import json
import uuid
import datetime
from pathlib import Path

try:
    from .asset_catalog import filter_assets
except ImportError:
    from asset_catalog import filter_assets

_CLASSES=[]
_pending=[]
_known=set()
_queued=False
_prefab_enum=[]
_loading=False
_workspace_request=None

ROLE_CATEGORIES=(('buildings','Buildings'),('vehicles','Vehicles'),('vegetation','Vegetation'),
    ('rocks','Rocks'),('terrain_props','Terrain Props'),('furniture','Furniture'),
    ('industrial','Industrial'),('military','Military'),('weapons','Weapons'),('loot','Loot'),
    ('buy_stations','Buy Stations'),('destructibles','Destructibles'),
    ('interactive','Interactive Objects'),('characters','Characters'),('animations','Animations'),
    ('audio','Audio'),('vfx','VFX'))


def role_matches(entry,role):
    """Advisory name/category search; this never grants gameplay capabilities."""
    if role=='ALL':return True
    if role not in dict(ROLE_CATEGORIES):raise ValueError('Unknown asset search role')
    text=entry['model'].casefold();category=entry['category']
    direct={'vehicles':'vehicles','vegetation':'vegetation','rocks':'rocks','furniture':'furniture'}
    if role in direct:return category==direct[role]
    tokens={'buildings':('building','house','shack','hangar','hut','roof','wall'),
        'terrain_props':('terrain','debris','rubble','ground','road','barrier','sandbag'),
        'industrial':('pipe','electric','generator','machinery','factory','tank','pump','industrial'),
        'military':('mil_','military','sandbag','radar','artillery','bunker','ammo'),
        'weapons':('weapon','rifle','pistol','shotgun','rpg','launcher','machinegun'),
        'loot':('loot','supply_crate'), 'buy_stations':('buy_station','buystation'),
        'destructibles':('destructible','_dest','_dam','destroyed'),
        'interactive':('door','button','switch','radio','computer','laptop'),
        'characters':('character','mp_body','viewmodel','body_'),
        'animations':(), 'audio':(), 'vfx':('fx_','vfx','effect','fire','smoke')}
    return any(token in text for token in tokens.get(role,()))


def _object_key(obj):
    # Blender can reuse the address of a deleted drop Empty immediately.
    # session_uid belongs to the ID lifetime and survives ordinary undo updates.
    return getattr(obj,'session_uid',None) or obj.as_pointer()


def catalog_record(source_map,category):
    for value in (source_map,category):
        if not isinstance(value,str) or not value or any(c in value for c in ':\\/\r\n'):
            raise ValueError('Invalid source-map/category catalog path')
    path='MW2/Maps/'+source_map+'/'+category
    return str(uuid.uuid5(uuid.NAMESPACE_URL,'wz09:asset-catalog:'+path)),path,category


def _write_catalog(context,entry):
    # Official Blender CDF format: UUID:path:simple name, VERSION 1.
    # https://developer.blender.org/docs/features/asset_system/backend/asset_catalogs/
    root=Path(context.scene.wz09_project_root).resolve()
    if not (root/'AGENTS.md').is_file():raise ValueError('Set the standalone editor project root')
    directory=Path(__import__('bpy').data.filepath).parent
    if not directory.is_relative_to(root):directory=root/'blender_projects'
    directory.mkdir(parents=True,exist_ok=True)
    path=directory/'blender_assets.cats.txt';record=catalog_record(entry['source_map'],entry['category'])
    existing=path.read_bytes() if path.exists() else b''
    if existing:
        lines=[line.strip() for line in existing.decode('utf-8-sig').splitlines() if line.strip() and not line.lstrip().startswith('#')]
        if not lines or lines[0]!='VERSION 1':raise ValueError('Existing Blender asset catalog format is unsupported')
        for line in lines[1:]:
            values=line.split(':',2)
            if values[0]==record[0]:
                if len(values)<2 or values[1]!=record[1]:raise ValueError('Existing asset catalog UUID has another path')
                return record[0]
        backup=path.with_name('blender_assets.cats.backup-'+datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%dT%H%M%S%f')+'.txt')
        with backup.open('xb') as stream:stream.write(existing)
    with path.open('ab') as stream:
        if not existing:stream.write(b'# Blender Asset Catalog Definition: WZ2009 source catalog\nVERSION 1\n')
        elif not existing.endswith(b'\n'):stream.write(b'\n')
        stream.write((':'.join(record)+'\n').encode('utf-8'))
    return record[0]


def favorites(scene):
    values=json.loads(scene.get('wz09_asset_favorites_json','[]'))
    if not isinstance(values,list) or any(not isinstance(v,str) for v in values):
        raise ValueError('Asset favorites must be persistent source IDs')
    return set(values)


def library_view(catalog,*,favorite_ids=(),favorites_only=False,query='',category='ALL',source_map='ALL',sort='NAME',role='ALL'):
    if sort not in {'NAME','SOURCE','CATEGORY'}:raise ValueError('Unknown asset sort')
    records=filter_assets(catalog,query,category,source_map)
    records=[r for r in records if role_matches(r,role)]
    favorite_ids=set(favorite_ids)
    if favorites_only:records=[r for r in records if r['id'] in favorite_ids]
    key={'NAME':lambda r:(r['name'].casefold(),r['source_map'],r['id']),
         'SOURCE':lambda r:(r['source_map'],r['name'].casefold(),r['id']),
         'CATEGORY':lambda r:(r['category'],r['name'].casefold(),r['id'])}[sort]
    return sorted(records,key=key)


def ensure_source_loaded(context,asset_id,entry=None,bundle=None,*,force_exact_source=False):
    """Load one exact verified static mesh, retaining its original catalog ID."""
    import bpy
    from . import asset_browser,asset_slices,asset_source_guard
    if type(force_exact_source) is not bool:raise ValueError('force_exact_source must be boolean')
    scene=context.scene
    if entry is None:
        catalog=asset_browser._catalog(scene)
        matches=[r for r in catalog['assets'] if r['id']==asset_id]
        if len(matches)!=1:raise ValueError('Asset source is missing or ambiguous')
        entry=matches[0];bundle=catalog['sources'][entry['source_sha256']]['preview_bundle']
    key=(entry.get('source_map_instance_id',''),entry['source_sha256'],entry['model'])
    found=asset_browser._model_roots(scene,source_bound=True).get(key)
    if found is not None:
        parts=asset_browser._mesh_parts(scene,found)
        if asset_source_guard.may_reuse_source(parts,
                is_exact_slice=bool(found.get('wz09_slice_bundle_json')),
                force_exact_source=force_exact_source):return found
    owner=next((o for o in scene.objects if o.get('wz09_type')=='map_instance'
                and o.get('wz09_id')==key[0]),None)
    if owner is None:raise ValueError('The source map proxy is missing')
    root=Path(scene.wz09_project_root).resolve()
    if not (root/'AGENTS.md').is_file():raise ValueError('Set the standalone editor project root')
    sliced=asset_slices.capture_slice(entry,bundle,root/'exports/asset-slices-v1')
    previous=list(context.selected_objects);active=context.view_layer.objects.active
    before={o.as_pointer() for o in bpy.data.objects};created=[]
    try:
        bpy.ops.import_scene.gltf(filepath=sliced['reference_path'])
        created=[o for o in bpy.data.objects if o.as_pointer() not in before]
        from .preview_geometry import repair_preview_winding
        repair_preview_winding(scene,objects=created)
        models=[o for o in created if asset_browser.model_metadata(o)
                and asset_browser.model_metadata(o)['model']==entry['model']]
        if len(models)!=1:raise ValueError('Exact mesh slice did not produce one source model')
        keys={o.as_pointer() for o in created}
        for obj in created:
            if obj.parent is None or obj.parent.as_pointer() not in keys:
                local=obj.matrix_world.copy();obj.parent=owner;obj.matrix_parent_inverse.identity();obj.matrix_basis=local
            obj['wz09_preview_only']=True;obj['wz09_editor_overlay']=True
            obj['wz09_preview_id']=str(uuid.uuid4())
            obj['wz09_native_visual_source_sha']=entry['source_sha256']
            obj['wz09_slice_bundle_json']=json.dumps(sliced,sort_keys=True)
            obj.hide_set(True);obj.hide_render=True;obj.hide_select=True
        context.view_layer.update()
        asset_source_guard.require_uncropped_source(asset_browser._mesh_parts(scene,models[0]))
        return models[0]
    except Exception:
        if not created:created=[o for o in bpy.data.objects if o.as_pointer() not in before]
        for obj in reversed(created):bpy.data.objects.remove(obj,do_unlink=True)
        raise
    finally:
        for obj in context.selected_objects:obj.select_set(False)
        for obj in previous:
            if obj.name in scene.objects:obj.select_set(True)
        if active is not None and active.name in scene.objects:context.view_layer.objects.active=active


def _collection(context,entry):
    import bpy
    from . import asset_browser
    found=[c for c in bpy.data.collections if c.get('wz09_browser_asset_id')==entry['id']]
    if len(found)>1:raise ValueError('Duplicate native browser asset IDs')
    if found:return found[0]
    verified=asset_browser._source_asset(context.scene,entry['id'])
    owner=next(o for o in context.scene.objects if o.get('wz09_type')=='map_instance'
               and o.get('wz09_id')==entry['source_map_instance_id'])
    root=asset_browser.place_asset(context,entry['id'],owner_map=owner,location=(0,0,0),_verified_source=verified)
    parts=[root,*list(root.children)]
    collection=bpy.data.collections.new(entry['model'])
    collection['wz09_browser_asset_id']=entry['id']
    collection['wz09_browser_source_sha256']=entry['source_sha256']
    try:
        # New placement objects belong only to the active scene's master
        # collection. Unlink only those known objects; source data stays shared.
        for obj in parts:
            context.scene.collection.objects.unlink(obj);collection.objects.link(obj)
            for key in list(obj.keys()):del obj[key]
            obj['wz09_browser_template']=True
            obj.hide_render=False;obj.hide_select=False
        matrix=root.matrix_world.copy();root.parent=None;root.matrix_world=matrix
        collection.asset_mark()
        metadata=collection.asset_data
        metadata.description=entry['model']+' | '+entry['source_map']+' | visual_only | '+entry['id']
        metadata.author='WZ2009 local native source catalog'
        metadata.catalog_id=_write_catalog(context,entry)
        for tag in (entry['source_map'],entry['category'],'visual_only'):
            metadata.tags.new(tag,skip_if_exists=True)
        return collection
    except Exception:
        for obj in reversed(parts):bpy.data.objects.remove(obj,do_unlink=True)
        bpy.data.collections.remove(collection)
        raise


def _owner(scene):
    selected=getattr(scene,'wz09_browser_target_map',None)
    if selected is not None and selected.name in scene.objects and selected.get('wz09_type')=='map_instance':return selected
    target=scene.get('wz09_browser_target_map_id')
    return next((o for o in scene.objects if o.get('wz09_type')=='map_instance' and o.get('wz09_id')==target),None)


def _prefab_items(self,context):
    global _prefab_enum
    items=[('AUTO','Apply matching blueprint','Apply one uniquely identified prefab'),
           ('VISUAL','Visual only','Bewusste rein visuelle Platzierung')]
    if context is not None:
        try:
            from . import asset_browser,prefab_authoring
            records=prefab_authoring.templates_for_asset(context.scene,asset_browser.selected_asset_id(context.scene))
            items.extend((r['id'],r['kind']+' · '+r['id'][:8],r['parameters'].get('damage_profile','')) for r in records)
        except ValueError:pass
    _prefab_enum=items;return _prefab_enum


def adopt_drop(context,obj,owner=None,prefab_choice='AUTO'):
    """Adopt a real native Collection drop through the existing asset placer."""
    import bpy
    from . import asset_browser,prefab_authoring
    owner=owner if owner is not None else _owner(context.scene)
    if obj.instance_collection is None or owner is None:raise ValueError('Select the target map before dragging and dropping')
    asset_id=obj.instance_collection.get('wz09_browser_asset_id')
    if not asset_id:raise ValueError('No source-bound WZ asset')
    matches=prefab_authoring.templates_for_asset(context.scene,asset_id)
    template=None
    if prefab_choice in ('AUTO',''):
        if len(matches)>1:raise ValueError('Multiple blueprints: select one in the asset panel or choose Visual Only')
        template=matches[0] if matches else None
    elif prefab_choice!='VISUAL':
        template=next((r for r in matches if r['id']==prefab_choice),None)
        if template is None:raise ValueError('Selected blueprint does not belong to this asset')
    before={o.as_pointer() for o in bpy.data.objects}
    try:
        matrix=obj.matrix_world.copy()
        result=asset_browser.place_asset(context,asset_id,owner_map=owner,location=matrix.translation)
        result.matrix_world=matrix;context.view_layer.update()
        if template is not None:prefab_authoring.clone_for_asset(context,template,result)
        bpy.data.objects.remove(obj,do_unlink=True)
        return result
    except Exception:
        for created in [o for o in bpy.data.objects if o.as_pointer() not in before]:bpy.data.objects.remove(created,do_unlink=True)
        raise


def _finish_drops():
    global _queued
    import bpy
    _queued=False
    pending=list(_pending);_pending.clear()
    for obj in pending:
        try:
            if not obj.instance_collection or not obj.instance_collection.get('wz09_browser_asset_id'):continue
            with bpy.context.temp_override(active_object=obj,object=obj):
                bpy.ops.wz09.adopt_browser_drop(object_name=obj.name)
        except (ReferenceError,RuntimeError,ValueError):continue
    return None


def _watch_drops(scene,depsgraph):
    global _queued
    import bpy
    if _loading:return
    # KH 2026-10-19: drops only come from the Assets tab; skip 168k-object scans elsewhere
    if getattr(scene,'wz09_workspace','')!='ASSETS':return
    for update in depsgraph.updates:
        obj=update.id.original
        if not isinstance(obj,bpy.types.Object):continue
        key=_object_key(obj)
        if key in _known:continue
        _known.add(key)
        collection=obj.instance_collection
        if collection is not None and collection.get('wz09_browser_asset_id') and not obj.get('wz09_browser_preview'):
            _pending.append(obj)
    if _pending and not _queued:
        _queued=True;bpy.app.timers.register(_finish_drops,first_interval=.05)


def _before_load(*args):
    global _loading,_queued,_workspace_request
    import bpy
    _loading=True;_queued=False;_pending.clear();_known.clear()
    _workspace_request=None
    if bpy.app.timers.is_registered(_finish_drops):bpy.app.timers.unregister(_finish_drops)
    if bpy.app.timers.is_registered(_finish_workspace_layout):bpy.app.timers.unregister(_finish_workspace_layout)


def _after_load(*args):
    global _loading,_queued
    import bpy
    _pending.clear();_known.clear();_known.update(_object_key(o) for o in bpy.data.objects)
    _loading=False;_queued=False


def validate_adoptions(scene):
    for obj in scene.objects:
        collection=getattr(obj,'instance_collection',None)
        if collection is not None and collection.get('wz09_browser_asset_id') and not obj.get('wz09_browser_preview'):
            raise ValueError('Asset tile not applied: select a target map and apply the selected tile')


def _activate_panel():
    import bpy
    for window in bpy.context.window_manager.windows:
        if window.workspace.name!='WZ2009 Assets':continue
        for area in window.screen.areas:
            if area.type!='VIEW_3D':continue
            for region in area.regions:
                if region.type=='UI':
                    try:region.active_panel_category='WZ2009'
                    except (AttributeError,TypeError):pass
    return None


def _finish_workspace_layout():
    """Workspace switches finish after the operator returns to Blender's UI."""
    global _workspace_request
    import bpy
    if _workspace_request is None:return None
    window,workspace,attempts,divided=_workspace_request
    try:
        if window.workspace!=workspace:
            if attempts>=20:_workspace_request=None;return None
            _workspace_request=(window,workspace,attempts+1,divided);return .1
        with bpy.context.temp_override(window=window):
            screen=window.screen
            browsers=[a for a in screen.areas if a.type=='FILE_BROWSER' and a.ui_type=='ASSETS']
            if not browsers:
                if not divided:
                    area=max((a for a in screen.areas if a.type=='VIEW_3D'),key=lambda a:a.width*a.height)
                    with bpy.context.temp_override(area=area):bpy.ops.screen.area_split(direction='VERTICAL',factor=.42)
                    _workspace_request=(window,workspace,attempts,True)
                    return .05
                views=sorted((a for a in screen.areas if a.type=='VIEW_3D'),key=lambda a:a.x)
                browser=views[0];browser.type='FILE_BROWSER';browser.ui_type='ASSETS';browsers=[browser]
            for browser in browsers:
                params=browser.spaces.active.params
                if params is None:
                    if attempts>=20:raise ValueError('Blender Asset Browser is not ready yet')
                    _workspace_request=(window,workspace,attempts+1,divided);return .1
                if hasattr(params,'asset_library_reference'):params.asset_library_reference='LOCAL'
            for area in screen.areas:
                if area.type=='VIEW_3D':area.spaces.active.show_region_ui=True
        _workspace_request=None
        if not bpy.app.timers.is_registered(_activate_panel):bpy.app.timers.register(_activate_panel,first_interval=.1)
    except (ReferenceError,RuntimeError,ValueError):_workspace_request=None
    return None


def open_workspace(context):
    global _workspace_request
    import bpy
    from .authoring_helpers import resolve_owner_map
    owner=resolve_owner_map(context.active_object)
    if owner is not None:
        context.scene['wz09_browser_target_map_id']=str(owner['wz09_id'])
        context.scene.wz09_browser_target_map=owner
    context.scene.wz09_workspace='ASSETS'
    if _workspace_request is not None:return
    existing=bpy.data.workspaces.get('WZ2009 Assets')
    if existing is None:
        before={w.as_pointer() for w in bpy.data.workspaces}
        bpy.ops.workspace.duplicate()
        created=[w for w in bpy.data.workspaces if w.as_pointer() not in before]
        if len(created)!=1:raise ValueError('Blender could not create a unique asset workspace')
        existing=created[0];existing.name='WZ2009 Assets'
    elif context.window.workspace!=existing:
        context.window.workspace=existing
    _workspace_request=(context.window,existing,0,False)
    if not bpy.app.timers.is_registered(_finish_workspace_layout):bpy.app.timers.register(_finish_workspace_layout,first_interval=.1)


def show_preview(context,entry):
    """One shared-data collection instance in the existing scene's local view."""
    import bpy
    area=next((a for a in context.screen.areas if a.type=='VIEW_3D'),None)
    if area is None:raise ValueError('Open Asset Browser first to create its 3D preview')
    collection=_collection(context,entry)
    for obj in [o for o in context.scene.objects if o.get('wz09_browser_preview')]:
        bpy.data.objects.remove(obj,do_unlink=True)
    obj=bpy.data.objects.new('Asset preview · '+entry['model'],None)
    obj['wz09_browser_preview']=True;obj['wz09_editor_overlay']=True
    obj['wz09_preview_only']=True;obj['wz09_preview_id']=str(uuid.uuid4())
    obj.instance_type='COLLECTION';obj.instance_collection=collection
    context.scene.collection.objects.link(obj)
    for item in context.selected_objects:item.select_set(False)
    obj.select_set(True);context.view_layer.objects.active=obj
    region=next(r for r in area.regions if r.type=='WINDOW')
    with context.temp_override(area=area,region=region):
        if area.spaces.active.local_view is not None:bpy.ops.view3d.localview(frame_selected=False)
        bpy.ops.view3d.localview(frame_selected=True)
    return obj


def close_preview(context):
    import bpy
    for area in context.screen.areas:
        if area.type=='VIEW_3D' and area.spaces.active.local_view is not None:
            region=next(r for r in area.regions if r.type=='WINDOW')
            with context.temp_override(area=area,region=region):bpy.ops.view3d.localview(frame_selected=False)
    for obj in [o for o in context.scene.objects if o.get('wz09_browser_preview')]:bpy.data.objects.remove(obj,do_unlink=True)
    owner=_owner(context.scene)
    if owner is not None:
        for obj in context.selected_objects:obj.select_set(False)
        owner.hide_set(False);owner.select_set(True);context.view_layer.objects.active=owner


def draw(layout,context):
    from . import asset_browser
    layout.operator('wz09.open_asset_browser',text='Open Asset Browser',icon='ASSET_MANAGER')
    row=layout.row(align=True)
    row.operator('wz09.browser_asset_prepare',text='Prepare tile',icon='ASSET_MANAGER')
    row.operator('wz09.browser_favorite',text='Favorite',icon='SOLO_ON')
    layout.operator('wz09.browser_prepare_page',text='Prepare next 6 tiles',icon='IMGDISPLAY')
    layout.operator('wz09.browser_thumbnail',text='Generate thumbnail',icon='IMAGE_DATA')
    row=layout.row(align=True)
    row.operator('wz09.browser_preview',text='3D preview',icon='VIEWZOOM')
    row.operator('wz09.browser_preview_close',text='World',icon='WORLD')
    layout.prop(context.scene,'wz09_asset_source_filter',text='Provenance')
    layout.prop(context.scene,'wz09_asset_role',text='Search role')
    layout.label(text='Search roles are hints from original names',icon='INFO')
    row=layout.row(align=True)
    op=row.operator('wz09.asset_role_open',text='Animations',icon='ACTION');op.workspace='ANIMATION'
    op=row.operator('wz09.asset_role_open',text='Audio',icon='SOUND');op.workspace='AUDIO'
    layout.prop(context.scene,'wz09_asset_favorites_only')
    layout.prop(context.scene,'wz09_asset_sort')
    layout.prop(context.scene,'wz09_browser_prefab_choice',text='When placing')
    layout.prop(context.scene,'wz09_browser_target_map',text='Target map')
    layout.label(text='Drag and drop tiles to place',icon='MOUSE_LMB')
    active=context.active_object
    if active is not None and active.instance_collection is not None and active.instance_collection.get('wz09_browser_asset_id') and not active.get('wz09_browser_preview'):
        layout.operator('wz09.adopt_browser_drop',text='Apply selected tile',icon='IMPORT')
    owner=_owner(context.scene)
    layout.label(text='Target: '+(owner.name if owner is not None else 'Select a map before opening'),icon='OUTLINER_OB_EMPTY')
    try:
        entry=next(r for r in asset_browser._catalog(context.scene)['assets']
                   if r['id']==asset_browser.selected_asset_id(context.scene))
    except (ValueError,StopIteration):return
    box=layout.box();box.label(text=entry['model'],icon='OBJECT_DATA')
    box.label(text=entry['source_map']+' · '+entry['category'])
    box.label(text=str(entry['primitive_count'])+' Materialteile · visual_only')
    box.label(text='Source: '+entry['source_sha256'][:16])
    box.label(text='Shared mesh and texture data',icon='LINKED')


def register():
    import bpy
    from bpy.props import StringProperty,BoolProperty,EnumProperty,PointerProperty
    bpy.types.Scene.wz09_lazy_assets_enabled=BoolProperty(name='Load assets on demand',default=True)
    from .scene_io import MAPS
    from .asset_browser import _update_filter
    bpy.types.Scene.wz09_asset_source_filter=EnumProperty(name='Provenance',items=[('ALL','All sources',''),('common_mp','Gemeinsame MW2-Modelle','Native common zone; no additional map')]+[(m,m,'') for m in MAPS],update=_update_filter)
    bpy.types.Scene.wz09_asset_favorites_only=BoolProperty(name='Favorites only',default=False,update=_update_filter)
    bpy.types.Scene.wz09_asset_sort=EnumProperty(name='Sort',items=[('NAME','Name',''),('SOURCE','Map',''),('CATEGORY','Category','')])
    bpy.types.Scene.wz09_asset_role=EnumProperty(name='Search role',items=[('ALL','All roles','Original catalog entries remain unchanged')]+
        [(key,label,'Search aid; does not confirm a gameplay function') for key,label in ROLE_CATEGORIES],update=_update_filter)
    bpy.types.Scene.wz09_browser_prefab_choice=EnumProperty(name='Blueprint',items=_prefab_items)
    bpy.types.Scene.wz09_browser_target_map=PointerProperty(name='Target map',type=bpy.types.Object,
        poll=lambda self,obj:obj.get('wz09_type')=='map_instance')
    class WZ09_OT_asset_role_open(bpy.types.Operator):
        bl_idname='wz09.asset_role_open';bl_label='Open asset tools'
        workspace:StringProperty()
        def execute(self,context):
            if self.workspace not in {'ANIMATION','AUDIO'}:return {'CANCELLED'}
            context.scene.wz09_workspace=self.workspace;return {'FINISHED'}
    class WZ09_OT_browser_prepare_page(bpy.types.Operator):
        bl_idname='wz09.browser_prepare_page';bl_label='Prepare six more filtered tiles';bl_options={'REGISTER','UNDO'}
        def execute(self,context):
            from . import asset_browser
            try:
                scene=context.scene;catalog=asset_browser._catalog(scene)
                rows=library_view(catalog,favorite_ids=favorites(scene),favorites_only=scene.wz09_asset_favorites_only,
                    query=scene.wz09_asset_search,category=scene.wz09_asset_category,
                    source_map=scene.wz09_asset_source_filter,sort=scene.wz09_asset_sort,role=scene.wz09_asset_role)
                available={c.get('wz09_browser_asset_id') for c in bpy.data.collections if c.asset_data}
                rows=[r for r in rows if r['id'] not in available][:6]
                for entry in rows:
                    collection=_collection(context,entry)
                    if collection.preview is None or not collection.preview.image_size[0]:collection.asset_generate_preview()
                self.report({'INFO'},str(len(rows))+' additional tiles prepared');return {'FINISHED'}
            except (ValueError,RuntimeError,OSError,KeyError) as exc:self.report({'ERROR'},str(exc)[:180]);return {'CANCELLED'}
    class WZ09_OT_browser_preview(bpy.types.Operator):
        bl_idname='wz09.browser_preview';bl_label='Native 3D asset preview'
        bl_options={'REGISTER','UNDO'}
        def execute(self,context):
            try:
                from . import asset_browser
                entry=next(r for r in asset_browser._catalog(context.scene)['assets']
                           if r['id']==asset_browser.selected_asset_id(context.scene))
                show_preview(context,entry);return {'FINISHED'}
            except (ValueError,RuntimeError,KeyError,StopIteration) as exc:
                self.report({'ERROR'},str(exc)[:180]);return {'CANCELLED'}
    class WZ09_OT_browser_preview_close(bpy.types.Operator):
        bl_idname='wz09.browser_preview_close';bl_label='Exit asset preview'
        bl_options={'REGISTER','UNDO'}
        def execute(self,context):close_preview(context);return {'FINISHED'}
    class WZ09_OT_open_asset_browser(bpy.types.Operator):
        bl_idname='wz09.open_asset_browser';bl_label='Open Asset Browser'
        def execute(self,context):
            try:open_workspace(context);return {'FINISHED'}
            except (ValueError,RuntimeError,AttributeError) as exc:
                self.report({'ERROR'},str(exc)[:180]);return {'CANCELLED'}
    class WZ09_OT_browser_prepare(bpy.types.Operator):
        bl_idname='wz09.browser_asset_prepare';bl_label='Selected asset as tile';bl_options={'REGISTER','UNDO'}
        def execute(self,context):
            try:
                from . import asset_browser
                asset_id=asset_browser.selected_asset_id(context.scene)
                entry=next(r for r in asset_browser._catalog(context.scene)['assets'] if r['id']==asset_id)
                collection=_collection(context,entry)
                if hasattr(collection,'asset_generate_preview') and (collection.preview is None or not collection.preview.image_size[0]):collection.asset_generate_preview()
                return {'FINISHED'}
            except (ValueError,RuntimeError,KeyError,StopIteration) as exc:
                self.report({'ERROR'},str(exc)[:180]);return {'CANCELLED'}
    class WZ09_OT_browser_thumbnail(bpy.types.Operator):
        bl_idname='wz09.browser_thumbnail';bl_label='Generate cached asset preview'
        def execute(self,context):
            from . import asset_browser
            try:
                entry=next(r for r in asset_browser._catalog(context.scene)['assets'] if r['id']==asset_browser.selected_asset_id(context.scene))
                _collection(context,entry).asset_generate_preview();return {'FINISHED'}
            except (ValueError,RuntimeError,KeyError,StopIteration) as exc:self.report({'ERROR'},str(exc)[:180]);return {'CANCELLED'}
    class WZ09_OT_browser_favorite(bpy.types.Operator):
        bl_idname='wz09.browser_favorite';bl_label='Toggle favorite';bl_options={'REGISTER','UNDO'}
        def execute(self,context):
            from . import asset_browser
            try:
                asset_id=asset_browser.selected_asset_id(context.scene);values=favorites(context.scene)
                if asset_id in values:values.remove(asset_id)
                else:values.add(asset_id)
                context.scene['wz09_asset_favorites_json']=json.dumps(sorted(values))
                for collection in bpy.data.collections:
                    if collection.get('wz09_browser_asset_id')==asset_id and collection.asset_data:
                        tags=collection.asset_data.tags;tag=tags.get('favorite')
                        if asset_id in values and tag is None:tags.new('favorite')
                        elif asset_id not in values and tag is not None:tags.remove(tag)
                return {'FINISHED'}
            except ValueError as exc:self.report({'ERROR'},str(exc));return {'CANCELLED'}
    class WZ09_OT_browser_adopt(bpy.types.Operator):
        bl_idname='wz09.adopt_browser_drop';bl_label='Place WZ asset';bl_options={'REGISTER','UNDO'}
        object_name:StringProperty(options={'HIDDEN'})
        def execute(self,context):
            from . import asset_browser
            obj=bpy.data.objects.get(self.object_name) if self.object_name else context.active_object;owner=_owner(context.scene)
            if obj is None or obj.instance_collection is None:return {'CANCELLED'}
            asset_id=obj.instance_collection.get('wz09_browser_asset_id')
            if not asset_id or owner is None:
                self.report({'ERROR'},'Select the target map in the editor before dragging and dropping');return {'CANCELLED'}
            try:
                adopt_drop(context,obj,owner,prefab_choice=context.scene.wz09_browser_prefab_choice)
                return {'FINISHED'}
            except (ValueError,RuntimeError,OSError) as exc:
                self.report({'ERROR'},str(exc)[:180]);return {'CANCELLED'}
    _CLASSES[:]=[WZ09_OT_asset_role_open,WZ09_OT_browser_prepare_page,WZ09_OT_browser_preview_close,WZ09_OT_browser_preview,WZ09_OT_open_asset_browser,WZ09_OT_browser_prepare,WZ09_OT_browser_thumbnail,
                WZ09_OT_browser_favorite,WZ09_OT_browser_adopt]
    for cls in _CLASSES:bpy.utils.register_class(cls)
    _known.update(_object_key(o) for o in bpy.data.objects)
    for handler in (_watch_drops,_before_load,_after_load):bpy.app.handlers.persistent(handler)
    if _watch_drops not in bpy.app.handlers.depsgraph_update_post:bpy.app.handlers.depsgraph_update_post.append(_watch_drops)
    if _before_load not in bpy.app.handlers.load_pre:bpy.app.handlers.load_pre.append(_before_load)
    if _after_load not in bpy.app.handlers.load_post:bpy.app.handlers.load_post.append(_after_load)
    for handlers,handler in ((bpy.app.handlers.undo_pre,_before_load),(bpy.app.handlers.redo_pre,_before_load),
            (bpy.app.handlers.undo_post,_after_load),(bpy.app.handlers.redo_post,_after_load)):
        if handler not in handlers:handlers.append(handler)


def unregister():
    global _queued,_loading,_workspace_request
    import bpy
    if _watch_drops in bpy.app.handlers.depsgraph_update_post:bpy.app.handlers.depsgraph_update_post.remove(_watch_drops)
    if bpy.app.timers.is_registered(_finish_drops):bpy.app.timers.unregister(_finish_drops)
    if bpy.app.timers.is_registered(_activate_panel):bpy.app.timers.unregister(_activate_panel)
    if bpy.app.timers.is_registered(_finish_workspace_layout):bpy.app.timers.unregister(_finish_workspace_layout)
    if _before_load in bpy.app.handlers.load_pre:bpy.app.handlers.load_pre.remove(_before_load)
    if _after_load in bpy.app.handlers.load_post:bpy.app.handlers.load_post.remove(_after_load)
    for handlers,handler in ((bpy.app.handlers.undo_pre,_before_load),(bpy.app.handlers.redo_pre,_before_load),
            (bpy.app.handlers.undo_post,_after_load),(bpy.app.handlers.redo_post,_after_load)):
        if handler in handlers:handlers.remove(handler)
    _pending.clear();_known.clear()
    _queued=False;_loading=False
    _workspace_request=None
    for cls in reversed(_CLASSES):bpy.utils.unregister_class(cls)
    _CLASSES.clear()
    for name in ('wz09_lazy_assets_enabled','wz09_asset_source_filter','wz09_asset_favorites_only','wz09_asset_sort','wz09_asset_role','wz09_browser_prefab_choice','wz09_browser_target_map'):
        if hasattr(bpy.types.Scene,name):delattr(bpy.types.Scene,name)
