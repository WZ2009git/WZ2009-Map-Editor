"""0.9 editable road/river curves over existing protected terrain authoring.

Only new owned helpers, height grids and export geometry are edited. Existing
TerrainField carving/material masks and custom-GLB export remain authoritative.
Bridge decks are editor prototypes, with no structural or collision acceptance.
"""
import copy
import hashlib
import importlib
import json
import math
import uuid

try:
    from . import terrain_core as core, heightfield, terrain_authoring, terrain_sculpt
except ImportError:
    import terrain_core as core
    import heightfield
    import terrain_authoring
    import terrain_sculpt

KEY='wz09_terrain_route_json'
_CLASSES=[]


def _workflow():
    return importlib.import_module('.terrain_workflow' if __package__ else 'terrain_workflow',__package__ or None)


def _encoded(value):
    return json.dumps(value,sort_keys=True,separators=(',',':'),allow_nan=False)


def _id(route_id,role):
    return str(uuid.uuid5(uuid.NAMESPACE_URL,'wz09:terrain-route:'+route_id+':'+role))


def validate_route(record):
    keys={'kind','version','id','route_kind','points_m','width_m','shoulder_m','strength','max_grade_percent',
          'geometry_kind','make_geometry','depth_m','bank_width_m','from_map','to_map',
          'physical_scale_verified','collision_verified','navigation_verified'}
    if (not isinstance(record,dict) or set(record)!=keys or record.get('kind')!='wz09_terrain_route'
            or type(record.get('version')) is not int or record['version']!=1):
        raise ValueError('Exact version 1 road/river record required')
    core._identifier(record['id'],'route.id')
    if record['route_kind'] not in ('road','river') or record['geometry_kind'] not in ('road','bridge'):
        raise ValueError('Route requires road/river and road/bridge types')
    if not isinstance(record['points_m'],(list,tuple)) or not 2<=len(record['points_m'])<=2048:
        raise ValueError('Route requires 2-2048 points')
    points=[core._xyz(p,'route.point') for p in record['points_m']]
    for a,b in zip(points,points[1:]):
        horizontal=math.dist(a[:2],b[:2])
        if horizontal<.001:raise ValueError('Route points require horizontal separation')
        if record['route_kind']=='road' and abs(b[2]-a[2])/horizontal*100>record['max_grade_percent']+1e-7:
            raise ValueError('Road slope exceeds the selected maximum')
    for name in ('width_m','depth_m','bank_width_m'):
        core._number(record[name],name,positive=True)
    core._number(record['shoulder_m'],'shoulder_m',nonnegative=True)
    core._number(record['strength'],'strength',nonnegative=True)
    core._number(record['max_grade_percent'],'max_grade_percent',nonnegative=True)
    if record['strength']>1 or record['max_grade_percent']>100:
        raise ValueError('Strength must be 0-1 and maximum slope 0-100 percent')
    if type(record['make_geometry']) is not bool:
        raise ValueError('Geometry selection requires a boolean')
    for name in ('physical_scale_verified','collision_verified','navigation_verified'):
        if record[name] is not False:raise ValueError('Routenfreigaben bleiben false')
    for name in ('from_map','to_map'):
        if record[name]:core._identifier(record[name],name)
    if bool(record['from_map'])!=bool(record['to_map']):
        raise ValueError('Map connection requires two explicit map IDs or none')
    if record['route_kind']=='river':
        core.validate_river(dict(id=record['id'],points=points,width_m=record['width_m'],
                                 depth_m=record['depth_m'],bank_width_m=record['bank_width_m']))
    return copy.deepcopy(record)


def new_route(points,*,kind='road',identity=None,width_m=8,shoulder_m=8,strength=1,
              max_grade_percent=12,geometry_kind='road',make_geometry=False,
              depth_m=3,bank_width_m=10):
    return validate_route(dict(kind='wz09_terrain_route',version=1,id=identity or str(uuid.uuid4()),
        route_kind=kind,points_m=[list(p) for p in points],width_m=width_m,shoulder_m=shoulder_m,
        strength=strength,max_grade_percent=max_grade_percent,geometry_kind=geometry_kind,
        make_geometry=make_geometry,depth_m=depth_m,bank_width_m=bank_width_m,from_map='',to_map='',
        physical_scale_verified=False,collision_verified=False,navigation_verified=False))


def nearest_profile(x,y,points):
    """Distance and linearly graded centreline Z, in the same world frame."""
    nearest=None
    for a,b in zip(points,points[1:]):
        dx,dy=b[0]-a[0],b[1]-a[1]
        t=max(0.,min(1.,((x-a[0])*dx+(y-a[1])*dy)/(dx*dx+dy*dy)))
        value=(math.hypot(x-a[0]-t*dx,y-a[1]-t*dy),a[2]+t*(b[2]-a[2]))
        if nearest is None or value[0]<nearest[0]:nearest=value
    return nearest


def flatten_grid(source,route,*,sample_surface):
    """One bounded centreline flatten step, retaining all original locks/data.

    Freshness is checked by the scene adapter before this pure operation. Current
    source core/water samples are checked too, so a stale or incomplete mask can
    never permit a native core or water edit. No second height generator is used.
    """
    heightfield.validate_grid(source);route=validate_route(route)
    if route['route_kind']!='road':raise ValueError('Only roads flatten the authored heightmap')
    if route['geometry_kind']=='bridge':
        raise ValueError('Create a bridge deck without flattening terrain; preserve the water bed')
    out=copy.deepcopy(source);w,h=source['width'],source['height'];x0,y0,x1,y1=source['bounds_m']
    half=route['width_m']/2;shoulder=route['shoulder_m'];changed=0
    for row in range(h):
        y=y0+row/(h-1)*(y1-y0)
        for col in range(w):
            index=row*w+col
            if source['locked'][index] or col in (0,w-1) or row in (0,h-1):continue
            x=x0+col/(w-1)*(x1-x0)
            distance,target=nearest_profile(x,y,route['points_m'])
            if distance>half+shoulder:continue
            protection=sample_surface(x,y)
            if protection.get('protected') or protection.get('underwater') or protection.get('core_distance_m',1e8)<2:continue
            alpha=route['strength']
            if distance>half:
                alpha*=1-core._smooth((distance-half)/shoulder) if shoulder else 0
            value=source['samples'][index]+alpha*(target-source['samples'][index])
            out['samples'][index]=value;changed+=value!=source['samples'][index]
    out['operations'].append(dict(kind='road_profile',route_id=route['id'],
        route_sha256=hashlib.sha256(_encoded(route).encode()).hexdigest(),width_m=route['width_m'],
        shoulder_m=route['shoulder_m'],strength=route['strength'],changed_samples=changed))
    return heightfield.validate_grid(out)


def resample(points,step_m,max_points=4096):
    result=[list(points[0])]
    for a,b in zip(points,points[1:]):
        count=max(1,math.ceil(math.dist(a[:2],b[:2])/step_m))
        if len(result)+count>max_points:raise ValueError('Route too long/detailed; increase point spacing')
        result.extend([[a[i]+(b[i]-a[i])*n/count for i in range(3)] for n in range(1,count+1)])
    return result


def road_plan(route,*,sample_surface,core_polygons=(),step_m=8):
    """Owned CCW strip + existing-format asphalt masks, omitting protected cores.

    Native source images are selected later by the scene adapter. Bridge strips
    have no invented supports and retain an explicit editor-prototype status.
    """
    route=validate_route(route)
    if route['route_kind']!='road':raise ValueError('Road plan requires a road route')
    step_m=core._number(step_m,'step_m',positive=True)
    points=resample(route['points_m'],step_m,max_points=1025)
    half=route['width_m']/2;vertices=[];faces=[];uvs=[];masks=[];length=0.
    edges=[]
    for index,point in enumerate(points):
        before=points[max(0,index-1)];after=points[min(len(points)-1,index+1)]
        dx,dy=after[0]-before[0],after[1]-before[1];norm=math.hypot(dx,dy)
        if norm<1e-8:raise ValueError('Road turn too tight; widen the curve')
        nx,ny=-dy/norm,dx/norm
        adjacent=(points[index+1] if index<len(points)-1 else point)
        start=(point if index<len(points)-1 else points[index-1])
        sx,sy=adjacent[0]-start[0],adjacent[1]-start[1];segment=math.hypot(sx,sy)
        projection=abs(nx*(-sy/segment)+ny*(sx/segment))
        miter=min(half*2,half/max(.25,projection))
        edges.append(([point[0]-nx*miter,point[1]-ny*miter,point[2]],
                      [point[0]+nx*miter,point[1]+ny*miter,point[2]]))
    for index,(a,b) in enumerate(zip(points,points[1:])):
        distance=math.dist(a[:2],b[:2])
        quad=[edges[index][0],edges[index+1][0],edges[index+1][1],edges[index][1]]
        polygon=core.validate_polygon([p[:2] for p in quad],'road.polygon')
        center=[(a[i]+b[i])/2 for i in range(3)]
        probes=[sample_surface(*p[:2]) for p in [*quad,center]]
        blocked=any(p.get('protected') for p in probes) or any(core._overlap_area(polygon,p)>1e-8 for p in core_polygons)
        if route['geometry_kind']=='road':blocked=blocked or any(p.get('underwater') for p in probes)
        if blocked:
            length+=distance;continue
        offset=len(vertices)
        vertices.extend([(x,y,z+.025) for x,y,z in quad])
        faces.extend([(offset,offset+1,offset+2),(offset,offset+2,offset+3)])
        uv=[(0,length/route['width_m']),(0,(length+distance)/route['width_m']),
            (1,(length+distance)/route['width_m']),(1,length/route['width_m'])]
        uvs.extend([[uv[i] for i in face] for face in ((0,1,2),(0,2,3))])
        if route['geometry_kind']=='road':
            masks.append(dict(id=_id(route['id'],'asphalt:'+str(index)),polygon=[list(p) for p in polygon],
                material='asphalt',strength=1.,falloff_m=route['shoulder_m'],enabled=True))
        length+=distance
    if not faces:raise ValueError('Route is entirely inside protected cores or water; draw an authored outer route')
    return dict(id=_id(route['id'],'geometry'),vertices=vertices,faces=faces,face_uvs=uvs,
        surfaces=['asphalt']*len(faces),winding='ccw',
        masks=masks,kind=route['geometry_kind'],surface='asphalt',route_id=route['id'],
        status='editor_bridge_prototype' if route['geometry_kind']=='bridge' else 'authored_editor_road',
        collision_verified=False,navigation_verified=False,structural_verified=False)


def cubic_points(a,handle_a,handle_b,b,steps=12):
    return [[(1-t)**3*a[i]+3*(1-t)**2*t*handle_a[i]+3*(1-t)*t*t*handle_b[i]+t**3*b[i]
             for i in range(3)] for t in (n/steps for n in range(1,steps+1))]


def curve_points(obj):
    if getattr(obj,'type','')!='CURVE' or len(obj.data.splines)!=1:
        raise ValueError('Select one open road/river curve')
    spline=obj.data.splines[0]
    if spline.use_cyclic_u:raise ValueError('Route requires an open curve')
    if spline.type=='POLY':points=[list(p.co[:3]) for p in spline.points]
    elif spline.type=='BEZIER':
        source=list(spline.bezier_points);points=[list(source[0].co)]
        for a,b in zip(source,source[1:]):points.extend(cubic_points(a.co,a.handle_right,b.handle_left,b.co))
    else:raise ValueError('Explicitly convert NURBS to poly or Bezier first')
    if not 2<=len(points)<=2048:raise ValueError('Route requires 2-2048 points')
    return [terrain_authoring._world_point(obj.matrix_world,p) for p in points]


def _local(context,obj,*,allow_helper=False):
    if obj is None:raise ValueError('Select a local curve')
    if getattr(obj,'library',None) or getattr(obj,'override_library',None) or getattr(obj.data,'library',None):
        raise ValueError('Linked/native source data remains unchanged')
    owners=list(getattr(obj,'users_scene',()))
    if len(owners)!=1 or owners[0]!=context.scene or getattr(obj.data,'users',1)!=1:
        raise ValueError('Route requires authored data in the current scene')
    parent=obj
    while parent is not None:
        helper=bool(parent.get(KEY) or parent.get('wz09_water_json') or
                    parent.get('wz09_route_generated') and parent.get('wz09_material_mask_json')) if allow_helper else False
        if parent.get('wz09_type') or parent.get('wz09_asset_placement') or parent.get('wz09_preview_only') and not helper:
            raise ValueError('Native maps/assets are not overwritten as authoring routes')
        parent=getattr(parent,'parent',None)


def _controls(context,points,kind=None,identity=None):
    s=context.scene.wz09_terrain_routes
    return new_route(points,kind=kind or s.route_kind,identity=identity,width_m=s.width_m,
        shoulder_m=s.shoulder_m,strength=s.strength,max_grade_percent=s.max_grade_percent,
        geometry_kind=s.geometry_kind,make_geometry=s.make_geometry,depth_m=s.depth_m,bank_width_m=s.bank_width_m)


def _curve(context,points,name,api):
    data=api.data.curves.new(name,'CURVE');data.dimensions='3D';obj=None
    try:
        spline=data.splines.new('POLY');spline.points.add(len(points)-1)
        for target,point in zip(spline.points,points):target.co=(*point,1.)
        obj=api.data.objects.new(name,data);context.scene.collection.objects.link(obj)
        return obj
    except Exception:
        if obj is not None:api.data.objects.remove(obj,do_unlink=True)
        if data.users==0:api.data.curves.remove(data)
        raise


def _activate(context,obj):
    for previous in context.selected_objects:previous.select_set(False)
    obj.select_set(True);context.view_layer.objects.active=obj


def create_route(context,kind,*,from_selected=False,bpy_module=None):
    """Copy a local authoring curve or create a new route; never edit its source."""
    if getattr(context,'mode','OBJECT')!='OBJECT':
        raise ValueError('Press Tab to return to Object Mode first')
    if bpy_module is None:import bpy as bpy_module
    if from_selected:
        _local(context,context.active_object,allow_helper=True);points=curve_points(context.active_object)
    else:
        x,y,z=map(float,context.scene.cursor.location);length=context.scene.wz09_terrain_routes.length_m
        points=[[x,y,z],[x+length*.5,y+length*.08,z],[x+length,y,z]]
    record=_controls(context,points,kind=kind)
    obj=_curve(context,points,'River curve' if kind=='river' else 'Road curve',bpy_module)
    obj[KEY]=_encoded(record);obj['wz09_preview_only']=True;obj['wz09_editor_overlay']=True
    obj['wz09_preview_id']=str(uuid.uuid4());obj.hide_render=True;obj.show_in_front=True
    obj.color=(.05,.65,1.,1.) if kind=='river' else (1.,.55,.08,1.)
    obj.data.bevel_depth=.12;obj.data.bevel_resolution=0
    _activate(context,obj)
    context.scene.wz09_terrain_routes.route_kind=kind
    return obj


def _native_material(scene,api):
    name=getattr(scene,'wz09_biome_image_asphalt','')
    image=api.data.images.get(name) if name else None
    if image is None:raise ValueError('Select the verified native asphalt texture under Source textures first')
    material=api.data.materials.new('WZ09 native asphalt · owned route');material.use_nodes=True
    shader=material.node_tree.nodes.get('Principled BSDF')
    texture=material.node_tree.nodes.new('ShaderNodeTexImage');texture.image=image
    material.node_tree.links.new(texture.outputs['Color'],shader.inputs['Base Color'])
    shader.inputs['Roughness'].default_value=.9
    return material


def _outputs(context,record,plan,api):
    created=[];material=None
    try:
        for order,mask in enumerate(plan['masks']):
            points=[[*p,0.] for p in mask['polygon']]
            obj=_curve(context,points,'Road - asphalt mask',api);created.append(obj)
            obj.data.splines[0].use_cyclic_u=True
            obj['wz09_material_mask_json']=_encoded(mask);obj['wz09_terrain_mask_id']=mask['id']
            obj['wz09_terrain_mask_order']=10000+order;obj['wz09_preview_only']=True
            obj['wz09_preview_id']=_id(record['id'],'mask-preview:'+str(order));obj['wz09_editor_overlay']=True
            obj.hide_render=True;obj.hide_set(True)
        if record['make_geometry']:
            material=_native_material(context.scene,api)
            mesh=api.data.meshes.new('Owned route deck');obj=None
            try:
                mesh.from_pydata(plan['vertices'],[],plan['faces']);mesh.update()
                uv=mesh.uv_layers.new(name='UVMap')
                for polygon,coordinates in zip(mesh.polygons,plan['face_uvs']):
                    for loop,point in zip(polygon.loop_indices,coordinates):uv.data[loop].uv=point
                obj=api.data.objects.new('Bridge - editor prototype' if record['geometry_kind']=='bridge' else 'Road',mesh)
                context.scene.collection.objects.link(obj);created.append(obj)
            except Exception:
                if obj is None and mesh.users==0:api.data.meshes.remove(mesh)
                raise
            obj.data.materials.append(material)
            obj['wz09_custom_geometry']=True;obj['wz09_geometry_id']=plan['id']
            obj['wz09_geometry_kind']=record['geometry_kind'];obj['wz09_geometry_status']=plan['status']
            obj['wz09_connector_from']=record['from_map'];obj['wz09_connector_to']=record['to_map']
            obj['wz09_route_source_json']=_encoded(record)
        for obj in created:
            obj['wz09_route_generated']=True;obj['wz09_route_owner_id']=record['id']
        return created
    except Exception:
        for obj in reversed(created):_remove_output(obj,api)
        if material is not None and material.users==0:api.data.materials.remove(material)
        raise


def _remove_output(obj,api):
    data=obj.data;kind=obj.type
    api.data.objects.remove(obj,do_unlink=True)
    if data.users==0:
        (api.data.meshes if kind=='MESH' else api.data.curves).remove(data)


def apply_route(context,*,workflow=None,bpy_module=None):
    """Plan first, then transactionally update only outputs owned by this route."""
    if getattr(context,'mode','OBJECT')!='OBJECT':
        raise ValueError('Finish curve editing with Tab, then apply the route')
    if bpy_module is None:import bpy as bpy_module
    workflow=workflow or _workflow();scene=context.scene;obj=context.active_object
    _local(context,obj,allow_helper=True)
    if not obj.get(KEY):raise ValueError('Create an authored road/river curve or copy the selection first')
    old_record=validate_route(json.loads(obj[KEY]));record=_controls(context,curve_points(obj),
        kind=old_record['route_kind'],identity=old_record['id'])
    record['from_map']=old_record['from_map'];record['to_map']=old_record['to_map']
    old_meta={key:obj.get(key) for key in (KEY,'wz09_water_json')}
    old_scene={key:scene.get(key) for key in (terrain_sculpt.KEY,terrain_sculpt.SUMMARY)}
    created=[];detached=[]
    regions,waters,source=workflow._inputs(scene)
    if record['route_kind']=='river':
        # Existing validation/carving remains the only river implementation.
        common=dict(id=record['id'],kind='river',position_m=list(obj.matrix_world.translation),
            points_m=record['points_m'],width_m=record['width_m'],depth_m=record['depth_m'],
            bank_width_m=record['bank_width_m'],level_m=record['points_m'][0][2],
            extent_m=[100,100],runtime_integrated=False,collision_verified=False)
        # Height grids are bound to exact water geometry. Refuse stale reuse;
        # clearing this derived layer is explicit in the route UI description.
        if scene.get(terrain_sculpt.KEY):
            raise ValueError('River changes the water source: discard the heightmap first, apply the river, then recapture')
        plan=None;grid=None
    else:
        if record['geometry_kind']=='bridge' and not record['make_geometry']:
            raise ValueError('Enable authored deck geometry for a bridge prototype')
        grid=terrain_sculpt.settings(scene,regions,waters,source)
        if grid is None and record['geometry_kind']!='bridge':
            raise ValueError('Capture the current heightmap first; native cores/water remain protected')
        base_source={k:v for k,v in source.items() if k!='heightfield_authoring'}
        field=workflow._field(regions,waters,base_source)
        if record['geometry_kind']=='road':grid=flatten_grid(grid,record,sample_surface=field.sample_surface)
        plan=road_plan(record,sample_surface=field.sample_surface,core_polygons=[r['polygon'] for r in field.regions],
            step_m=max(.5,min(8.,source['cell_size_m']/2)))
        if record['geometry_kind']=='road' or record['make_geometry']:
            image_name=getattr(scene,'wz09_biome_image_asphalt','')
            if not image_name or bpy_module.data.images.get(image_name) is None:
                raise ValueError('Select the native asphalt texture before exporting a road/bridge')
        old_outputs=[o for o in scene.objects if o.get('wz09_route_generated') and o.get('wz09_route_owner_id')==record['id']]
        for previous in old_outputs:
            _local(context,previous,allow_helper=bool(previous.get('wz09_material_mask_json')))
    try:
        if record['route_kind']=='river':obj['wz09_water_json']=_encoded(common)
        else:
            for previous in old_outputs:
                collections=list(previous.users_collection)
                detached.append((previous,collections))
                for collection in collections:collection.objects.unlink(previous)
            created=_outputs(context,record,plan,bpy_module)
            if grid is not None:
                # Masks alter the material source, not the captured height base.
                # Rebind only after validating the prior exact source above.
                current_regions,current_waters,current_source=workflow._inputs(scene)
                grid['source_sha256']=heightfield.fingerprint(current_regions,current_waters,current_source)
                terrain_sculpt._store(context,grid,workflow=workflow,regenerate=False)
        obj[KEY]=_encoded(record)
        if scene.wz09_terrain_routes.regenerate:workflow.generate_terrain(context)
    except Exception:
        for output in reversed(created):_remove_output(output,bpy_module)
        for previous,collections in detached:
            for collection in collections:collection.objects.link(previous)
        for key,value in old_meta.items():
            if value is None:
                if key in obj:del obj[key]
            else:obj[key]=value
        for key,value in old_scene.items():
            if value is None:
                if key in scene:del scene[key]
            else:scene[key]=value
        raise
    for previous,_ in detached:_remove_output(previous,bpy_module)
    return dict(id=record['id'],kind=record['route_kind'],generated_objects=len(created),
        changed_samples=grid['operations'][-1].get('changed_samples',0) if grid and grid['operations'] else 0,
        collision_verified=False,navigation_verified=False)


def load_route(context):
    obj=context.active_object
    if obj is None or not obj.get(KEY):raise ValueError('Select an authored road/river curve')
    record=validate_route(json.loads(obj[KEY]));s=context.scene.wz09_terrain_routes
    for name in ('route_kind','width_m','shoulder_m','strength','max_grade_percent','geometry_kind','make_geometry','depth_m','bank_width_m'):
        setattr(s,name,record[name])
    return record


def draw(layout,context):
    s=context.scene.wz09_terrain_routes
    layout.label(text='Tab: edit points - Tab: return - Apply')
    row=layout.row(align=True);row.operator('wz09.route_new_road',text='Road curve',icon='CURVE_PATH');row.operator('wz09.route_new_river',text='River curve',icon='MOD_FLUID')
    layout.prop(s,'length_m');layout.prop(s,'route_kind')
    layout.operator('wz09.route_copy_curve',text='Copy selected curve as an authored route',icon='DUPLICATE')
    layout.operator('wz09.route_load',text='Read route settings',icon='EYEDROPPER')
    layout.prop(s,'width_m')
    if s.route_kind=='road':
        layout.prop(s,'shoulder_m');layout.prop(s,'strength');layout.prop(s,'max_grade_percent')
        layout.prop(s,'geometry_kind');layout.prop(s,'make_geometry')
        layout.prop_search(context.scene,'wz09_biome_image_asphalt',__import__('bpy').data,'images',text='Native asphalt texture')
        if s.geometry_kind=='bridge':layout.label(text='Bridge deck: editor prototype; structural safety unverified',icon='INFO')
        else:layout.label(text='Authored heightmap only - map cores and water protected',icon='LOCKED')
    else:
        layout.prop(s,'depth_m');layout.prop(s,'bank_width_m')
        layout.label(text='Water changed: discard/recapture the heightmap first',icon='INFO')
    layout.prop(s,'regenerate');layout.operator('wz09.route_apply',text='Apply / update route',icon='CHECKMARK')


def register():
    import bpy
    from bpy.props import BoolProperty,EnumProperty,FloatProperty,PointerProperty
    class WZ09_TerrainRouteSettings(bpy.types.PropertyGroup):
        route_kind:EnumProperty(name='Route type',items=[('road','Road',''),('river','River','')])
        length_m:FloatProperty(name='Starting length',default=150,min=1,max=10000,subtype='DISTANCE')
        width_m:FloatProperty(name='Width',default=8,min=.1,max=200,subtype='DISTANCE')
        shoulder_m:FloatProperty(name='Soft edge',default=8,min=0,max=200,subtype='DISTANCE')
        strength:FloatProperty(name='Flatten',default=1,min=0,max=1)
        max_grade_percent:FloatProperty(name='Maximum slope percent',default=12,min=0,max=100)
        geometry_kind:EnumProperty(name='Construction type',items=[('road','Terrain road',''),('bridge','Bridge deck - prototype','')])
        make_geometry:BoolProperty(name='Authored exportable deck geometry',default=False)
        depth_m:FloatProperty(name='Water depth',default=3,min=.1,max=200,subtype='DISTANCE')
        bank_width_m:FloatProperty(name='Bank transition',default=10,min=.1,max=500,subtype='DISTANCE')
        regenerate:BoolProperty(name='Regenerate terrain afterwards',default=True)
    def operator(name,label,action):
        def execute(self,context):
            try:
                action(context);self.report({'INFO'},'Eigene Route aktualisiert');return {'FINISHED'}
            except (ValueError,TypeError,KeyError,RuntimeError,OSError) as exc:
                self.report({'ERROR'},str(exc)[:220]);return {'CANCELLED'}
        return type('WZ09_OT_'+name,(bpy.types.Operator,),{'__module__':__name__,'bl_idname':'wz09.'+name,
            'bl_label':label,'bl_options':{'REGISTER','UNDO'},'execute':execute})
    classes=[WZ09_TerrainRouteSettings,operator('route_new_road','New road curve',lambda c:create_route(c,'road')),
        operator('route_new_river','Neue Flusskurve',lambda c:create_route(c,'river')),
        operator('route_copy_curve','Selection as authored route',lambda c:create_route(c,c.scene.wz09_terrain_routes.route_kind,from_selected=True)),
        operator('route_load','Apply route settings',load_route),operator('route_apply','Route anwenden',apply_route)]
    for cls in classes:bpy.utils.register_class(cls)
    bpy.types.Scene.wz09_terrain_routes=PointerProperty(type=WZ09_TerrainRouteSettings);_CLASSES[:]=classes


def unregister():
    import bpy
    if hasattr(bpy.types.Scene,'wz09_terrain_routes'):del bpy.types.Scene.wz09_terrain_routes
    for cls in reversed(_CLASSES):bpy.utils.unregister_class(cls)
    _CLASSES.clear()
