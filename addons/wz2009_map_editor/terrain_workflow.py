"""WZ bindings for tiled terrain, baked biome colour, water and source assets.

Uses the existing preview importer, source asset catalog and GLB exporter.
Generated data is editable design geometry; it never asserts native collision.
"""
import hashlib
import json
import math
import re
import uuid

try:
    from . import terrain_authoring as authoring
except ImportError:
    import terrain_authoring as authoring

try:
    import bpy
    from bpy.props import (BoolProperty, EnumProperty, FloatProperty, IntProperty,
                           FloatVectorProperty, StringProperty)
except ImportError:
    bpy = None

BIOMES = ('sand', 'soil', 'grass', 'snow', 'rock')
MATERIALS = BIOMES + ('asphalt',)
PALETTE = {'sand': (.58, .43, .25), 'soil': (.23, .14, .075),
           'grass': (.16, .25, .075), 'snow': (.8, .86, .88), 'rock': (.28, .29, .30),
           'asphalt': (.075, .08, .085)}
EDGE_SETTINGS_KEY = 'wz09_edge_closure_settings_json'


def assign_generated_normals(mesh, normals, *, replace_encoded=False, bpy_module=None):
    """Store exact free point normals on explicitly owned generated terrain.

    Blender 5 supports FLOAT_VECTOR ``custom_normal`` on the POINT domain.
    The older per-vertex setter converts vectors into fan-relative INT16 data;
    different boundary fans can decode the same requested vector differently.
    See Blender's Mesh::vert_normals/corner_normals in mesh_normals.cc. This
    changes storage only, never the caller's normal calculation or geometry.

    A legacy INT16 attribute may be removed only with explicit authorization
    on an owned derived copy. Native/source meshes must never be passed here.
    """
    api = bpy if bpy_module is None else bpy_module
    if api is None or tuple(api.app.version) < (5, 0, 0):
        raise ValueError('Exact generated terrain normal storage requires Blender5')
    if mesh.get('wz09_generated_terrain_owned') is not True:
        raise ValueError('Only explicitly owned generated terrain meshes may receive normals')
    values = list(normals)
    if len(values) != len(mesh.vertices):
        raise ValueError('Generated terrain needs exactly one normal per vertex')
    normalized = []
    for value in values:
        if len(value) != 3 or any(type(v) not in (int, float) or not math.isfinite(v) for v in value):
            raise ValueError('Generated normals must contain finite 3D vectors')
        length = math.sqrt(sum(v*v for v in value))
        if length <= 1e-12:
            raise ValueError('Generated normal cannot be zero')
        normalized.extend(v/length for v in value)
    current = mesh.attributes.get('custom_normal')
    previous = None if current is None else {'type':current.data_type,'domain':current.domain}
    if current is not None and (current.data_type,current.domain) != ('FLOAT_VECTOR','POINT'):
        if (current.data_type,current.domain) != ('INT16_2D','CORNER') or not replace_encoded:
            raise ValueError('Existing normal storage is incompatible; encoded replacement requires an owned-copy authorization')
    # All guards above are read-only. Do not update topology or fan data after
    # assignment; only the official free point-normal attribute is replaced.
    if current is not None and (current.data_type,current.domain) == ('INT16_2D','CORNER'):
        mesh.attributes.remove(current)
        current = None
    for polygon in mesh.polygons:
        polygon.use_smooth = True
    mesh.update()
    if current is None:
        current = mesh.attributes.new(name='custom_normal',type='FLOAT_VECTOR',domain='POINT')
    if (current.name,current.data_type,current.domain) != ('custom_normal','FLOAT_VECTOR','POINT'):
        raise ValueError('Blender did not create the required generated normal storage')
    current.data.foreach_set('vector',normalized)
    mesh.update()
    return {'storage':'FLOAT_VECTOR_POINT','previous':previous,
        'replaced_encoded':previous == {'type':'INT16_2D','domain':'CORNER'},
        'normals':len(values),'source_positions_or_topology_changed':False}


def _material_channels(settings):
    return MATERIALS if settings.get('material_layers') is not None or settings.get('landscape_plan') else BIOMES


def fingerprint(regions, waters, settings):
    return hashlib.sha256(json.dumps([regions, waters, settings], sort_keys=True,
                                     separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def _edge_module():
    try:from . import terrain_edges
    except ImportError:import terrain_edges
    return terrain_edges


def rebuild_map_edges(ctx, *, configure_missing=True):
    """Shared add-on entry point; source evidence and geometry stay in the helper."""
    if getattr(ctx,'mode','OBJECT')!='OBJECT':raise ValueError('Leave Edit Mode before adapting map edges')
    edges=_edge_module()
    scene=ctx.scene
    if scene.get(EDGE_SETTINGS_KEY):return edges.rebuild(ctx)
    if not configure_missing:return None
    # Configure writes JSON/SHA scene metadata before the helper's atomic
    # geometry transaction. A cancelled first apply must not leave a policy
    # without outputs, nor overwrite a pre-existing orphan's manifest/input.
    prefix='wz09_edge_closure_'
    previous={key:scene[key] for key in scene.keys() if key.startswith(prefix)}
    try:
        edges.configure(scene)
        return edges.rebuild(ctx)
    except Exception:
        for key in list(scene.keys()):
            if key.startswith(prefix) and key not in previous:del scene[key]
        for key,value in previous.items():scene[key]=value
        raise


def remove_map_edges(ctx):
    """Archive only helper-owned role IDs; source data is never a target."""
    if getattr(ctx,'mode','OBJECT')!='OBJECT':raise ValueError('Leave Edit Mode before removing generated map edges')
    return _edge_module().remove(ctx)


def _selected_boundary_core(ctx):
    if getattr(ctx,'mode','OBJECT')!='OBJECT':raise ValueError('Exit Edit Mode first')
    if ctx.scene.get('wz09_heightfield_authoring_json'):
        raise ValueError('Explicitly discard the authored height layer before outline changes; current layer retained')
    owner=getattr(ctx,'active_object',None);seen=set()
    while owner is not None and owner.get('wz09_type')!='map_instance':
        if id(owner) in seen:raise ValueError('Zyklische Map-Hierarchie')
        seen.add(id(owner));owner=owner.parent
    if owner is None:raise ValueError('Select a map or one of its mesh parts')
    if owner.get('wz09_source_map')=='mp_highrise':
        raise ValueError('Highrise uses its elevated roof region and authored facades')
    cores=[obj for obj in ctx.scene.objects if obj.get('wz09_region_kind')=='map_core'
           and obj.parent is owner]
    if len(cores)!=1:raise ValueError('Selected map requires exactly one owned core')
    return owner,cores[0]


def capture_boundary_profile(ctx):
    """Explicit strict capture; a painted height layer is never discarded."""
    owner,_core=_selected_boundary_core(ctx);edges=_edge_module()
    plan=edges.plan_boundary_profiles(ctx,map_codes=[owner['wz09_source_map']],
        max_points=64,max_error_m=2.,min_spacing_m=.01)
    profile=plan['profiles'][0]['profile']
    if profile['status']!='WITHIN_ERROR_BUDGET':
        deviation=profile['fit']['maximum_error_m']
        detail='no verified intersections' if deviation is None else f'{deviation:.2f} instead of at most 2 design meters'
        raise ValueError('Quellkontur mehrdeutig: '+detail+'. Existing heights are retained')
    result=edges.apply_boundary_profiles(ctx,plan,require_tolerance=True)
    return dict(result,source_coverage_fraction=profile['source_coverage_fraction'])


def remove_boundary_profile(ctx):
    """Remove selected owned profile metadata; raw core data never changed."""
    owner,core=_selected_boundary_core(ctx);edges=_edge_module()
    raw=core.get(edges.PROFILE_KEY)
    if not raw:return 0
    value=json.loads(raw);binding=value.get('binding',{})
    if (value.get('kind')!='wz09_bound_core_boundary_profile'
            or binding.get('core_id')!=str(core.get('wz09_region_id'))
            or binding.get('map_id')!=str(owner.get('wz09_id'))):
        raise ValueError('Outline profile belongs to another map/core ID')
    return edges.restore_boundary_profiles(ctx,dict(entries=[dict(core_id=str(core['wz09_region_id']),
        map_id=str(owner['wz09_id']),previous=None,applied_sha256=hashlib.sha256(raw.encode()).hexdigest())]))


def _discard_terrain_revision(collection):
    for obj in list(collection.objects):bpy.data.objects.remove(obj,do_unlink=True)
    bpy.data.collections.remove(collection)


def _commit_terrain_revision(ctx, collection, source, settings):
    """Archive only owned revisions, restoring them if edge rebuilding fails."""
    scene=ctx.scene
    keys=('wz09_terrain_input_sha256','wz09_terrain_settings_json')
    previous={key:(key in scene,scene.get(key)) for key in keys}
    old=[]
    for item in scene.collection.children:
        if item!=collection and item.get('wz09_terrain_generation'):
            objects=[(obj,{key:(key in obj,obj.get(key)) for key in
                ('wz09_custom_geometry','wz09_archived_generation')}) for obj in item.objects]
            old.append((item,item.hide_viewport,item.hide_render,objects))
    try:
        for item,_,_,objects in old:
            item.hide_viewport=True;item.hide_render=True
            for obj,_ in objects:
                obj['wz09_custom_geometry']=False;obj['wz09_archived_generation']=True
        scene['wz09_terrain_input_sha256']=source
        scene['wz09_terrain_settings_json']=json.dumps(settings,sort_keys=True)
        ctx.view_layer.update()
        if scene.get(EDGE_SETTINGS_KEY):
            rebuild_map_edges(ctx,configure_missing=False)
            return True
        return False
    except Exception:
        for key,(present,value) in previous.items():
            if present:scene[key]=value
            elif key in scene:del scene[key]
        for item,hidden_view,hidden_render,objects in old:
            item.hide_viewport=hidden_view;item.hide_render=hidden_render
            for obj,properties in objects:
                for key,(present,value) in properties.items():
                    if present:obj[key]=value
                    elif key in obj:del obj[key]
        _discard_terrain_revision(collection)
        ctx.view_layer.update()
        raise


def tile_range(bounds, cells, cell_size_m, max_tiles=64):
    if len(bounds) != 4 or any(isinstance(v, bool) or not isinstance(v, (int, float))
                              or not math.isfinite(v) for v in bounds):
        raise ValueError('Terrain bounds require four finite design coordinates')
    if bounds[0] >= bounds[2] or bounds[1] >= bounds[3]:
        raise ValueError('Terrain bounds must have positive area')
    if type(cells) is not int or not 4 <= cells <= 128 or not math.isfinite(cell_size_m) or cell_size_m <= 0:
        raise ValueError('Choose 4–128 cells per tile and positive grid spacing')
    width = cells * cell_size_m
    x0, y0 = (math.floor(v / width) for v in bounds[:2])
    x1, y1 = (math.ceil(v / width) for v in bounds[2:])
    if (x1-x0)*(y1-y0) > max_tiles:
        raise ValueError(f'This area needs {(x1-x0)*(y1-y0)} tiles; limit {max_tiles}. Increase grid spacing or reduce bounds.')
    return [(x,y) for y in range(y0,y1) for x in range(x0,x1)]


def _activate(ctx, obj):
    for item in ctx.selected_objects: item.select_set(False)
    obj.hide_set(False)
    obj.select_set(True)
    ctx.view_layer.objects.active = obj
    ctx.view_layer.update()


def _collection(scene, name):
    col = bpy.data.collections.new(name)
    scene.collection.children.link(col)
    return col


def _waters(scene):
    from mathutils import Vector
    result = []
    for obj in scene.objects:
        raw = obj.get('wz09_water_json')
        if not raw: continue
        data = json.loads(raw)
        data['position_m'] = list(obj.matrix_world.translation)
        if data['kind'] == 'river':
            if obj.type != 'CURVE' or len(obj.data.splines) != 1:
                raise ValueError('River requires one editable polyline spline')
            spline = obj.data.splines[0]
            if spline.type != 'POLY' or spline.use_cyclic_u:
                raise ValueError('River must be an open Poly curve; convert Bezier/NURBS explicitly')
            data['points_m'] = [list(obj.matrix_world @ Vector(p.co[:3])) for p in spline.points]
        else:
            data['level_m'] = obj.matrix_world.translation.z
            if any(abs(v) > 1e-6 for v in obj.rotation_euler) or any(abs(v-1) > 1e-6 for v in obj.scale):
                raise ValueError('Sea uses position and explicit extents; apply scale/rotation first')
        result.append(data)
    return sorted(result, key=lambda r:r['id'])


def _inputs(scene):
    try:from .composite_world import serialize_regions
    except ImportError:from composite_world import serialize_regions
    regions = serialize_regions(scene)
    confirmed = [r for r in regions if r['confirmed']]
    if not confirmed: raise ValueError('Create and explicitly confirm map-core regions before terrain generation')
    settings = {'seed':scene.wz09_terrain_seed, 'cells':scene.wz09_terrain_cells,
                'cell_size_m':scene.wz09_terrain_cell_size,
                'noise_amplitude_m':scene.wz09_terrain_noise,
                'noise_scale_m':scene.wz09_terrain_noise_scale,
                'sea_level_m':scene.wz09_sea_level,'bounds_m':list(scene.wz09_terrain_bounds),
                'texture_size':scene.wz09_terrain_texture_size,'texture_repeat_m':scene.wz09_terrain_texture_repeat,
                'texture_images':{b:getattr(scene,'wz09_biome_image_'+b) for b in BIOMES}}
    layers=authoring.material_settings(scene)
    if layers is not None:
        settings['material_layers']=layers
        settings['texture_images']['asphalt']=getattr(scene,'wz09_biome_image_asphalt','')
    if scene.get('wz09_landscape_plan_json'):
        settings['landscape_plan']=json.loads(scene['wz09_landscape_plan_json'])
        settings['texture_images']['asphalt']=getattr(scene,'wz09_biome_image_asphalt','')
    if scene.get('wz09_heightfield_authoring_json'):
        settings['heightfield_authoring']=json.loads(scene['wz09_heightfield_authoring_json'])
    if getattr(scene,'wz09_terrain_fast_bake',False):
        settings['material_bake']='sampled_weight_grid_v1'
    return regions, _waters(scene), settings


def _field(regions, waters, settings, exclusions=()):
    try:from .terrain_core import TerrainField
    except ImportError:from terrain_core import TerrainField
    plan=settings.get('landscape_plan')
    elevated=set(plan.get('elevated_map_ids',[])) if plan else set()
    base=TerrainField([r for r in regions if r['confirmed'] and r['map_instance_id'] not in elevated], seed=settings['seed'],
        noise_amplitude_m=settings['noise_amplitude_m'], noise_scale_m=settings['noise_scale_m'],
        sea_level_m=settings['sea_level_m'], rivers=[dict(w,points=w['points_m']) for w in waters if w['kind']=='river'],
        exclusions=exclusions,seas=[{'id':w['id'],
            'bounds':[w['position_m'][0]-w['extent_m'][0]/2,w['position_m'][1]-w['extent_m'][1]/2,
                      w['position_m'][0]+w['extent_m'][0]/2,w['position_m'][1]+w['extent_m'][1]/2],
            'level_m':w['level_m'],'depth_m':w['depth_m'],
            'coast_width_m':w.get('coast_width_m',30.),'carve_terrain':w.get('carve_terrain',False)}
            for w in waters if w['kind']=='sea'])
    if plan or settings.get('heightfield_authoring'):
        try:
            from . import heightfield,landscape_core
        except ImportError:
            import heightfield,landscape_core
        if plan:landscape_core.validate_plan(plan)
        grid=settings.get('heightfield_authoring')
        if grid and grid['source_sha256']!=heightfield.fingerprint(regions,waters,settings):
            raise ValueError('Heightmap basis changed: recapture or discard the layer')
        base=heightfield.SculptField(base,grid=grid,landscape=plan,
            relief=landscape_core.relief_height,materials=landscape_core.material_weights)
    return authoring.decorate_from_settings(base,settings.get('material_layers'))


def apply_landscape_plan(ctx,plan):
    """Use the existing protected-core generator with an explicit authored plan."""
    from . import landscape_core
    plan=landscape_core.validate_plan(plan)
    ctx.scene['wz09_landscape_plan_json']=json.dumps(plan,sort_keys=True,allow_nan=False)
    ctx.scene.wz09_terrain_bounds=plan['bounds_m']
    return generate_terrain(ctx)


def _image_sampler(scene,biomes=BIOMES):
    """Read only explicitly chosen loaded images; no material-name inference."""
    images = {}
    for biome in biomes:
        name = getattr(scene, 'wz09_biome_image_'+biome, '')
        if not name: continue
        image = bpy.data.images.get(name)
        if image is None or image.size[0] <= 0 or image.size[1] <= 0:
            raise ValueError(f'{biome}: selected texture is missing or unreadable')
        # A bounded downsample avoids retaining multiple full native textures.
        w,h = image.size;ratio=min(1.,256/max(w,h))
        sw,sh=max(1,round(w*ratio)),max(1,round(h*ratio))
        temporary=image.copy()
        try:
            temporary.scale(sw,sh)
            pixels=temporary.pixels[:]
            values=[tuple(pixels[(y*sw+x)*4+i] for i in range(3)) for y in range(sh) for x in range(sw)]
        finally:bpy.data.images.remove(temporary)
        images[biome] = sw,sh,values
    return images


def _baked_material(scene, field, bounds, identifier, samplers,biomes=BIOMES):
    resolution = scene.wz09_terrain_texture_size
    image = bpy.data.images.new('WZ terrain albedo '+identifier[:8], width=resolution, height=resolution, alpha=False)
    image.colorspace_settings.name = 'sRGB'
    x0,y0,x1,y1 = bounds
    pixels = []
    repeat = scene.wz09_terrain_texture_repeat
    if getattr(scene,'wz09_terrain_fast_bake',False):
        from .terrain_bake import bake
        pixels=bake(field,bounds,resolution,repeat,samplers,biomes,PALETTE,
            weight_cells=scene.wz09_terrain_cells)
    else:
        pixels=None
    for y in range(resolution if pixels is None else 0):
        if y==0:pixels=[]
        py = y0+(y+.5)/resolution*(y1-y0)
        for x in range(resolution):
            px = x0+(x+.5)/resolution*(x1-x0)
            weights = field.sample_surface(px,py)['biome_weights']
            color = [0.,0.,0.]
            for biome in biomes:
                weight = weights.get(biome,0.)
                if biome in samplers:
                    w,h,values = samplers[biome]
                    c = values[(int((py/repeat % 1)*h)%h)*w+int((px/repeat % 1)*w)%w]
                else:
                    # Procedural fallback is labelled, not an original shader.
                    variation = .94+.06*math.sin(px*.83+math.sin(py*.51))*math.cos(py*.71)
                    c = [v*variation for v in PALETTE[biome]]
                for i in range(3): color[i] += c[i]*weight
            pixels.extend((*color,1.))
    image.pixels.foreach_set(pixels)
    image.pack()
    material = bpy.data.materials.new('WZ baked ground '+identifier[:8])
    material.use_nodes = True
    nodes = material.node_tree.nodes
    shader = nodes.get('Principled BSDF')
    shader.inputs['Roughness'].default_value = .88
    tex = nodes.new('ShaderNodeTexImage'); tex.image = image
    tex.interpolation = 'Linear'; tex.extension = 'EXTEND'
    material.node_tree.links.new(tex.outputs['Color'], shader.inputs['Base Color'])
    return material


def _mesh_object(col, name, vertices, faces, *, preview=False):
    mesh = bpy.data.meshes.new(name)
    mesh.from_pydata(vertices, [], faces); mesh.update()
    obj = bpy.data.objects.new(name, mesh); col.objects.link(obj)
    if preview:
        obj['wz09_preview_only'] = True
        obj['wz09_preview_id'] = str(uuid.uuid4())
    else:
        obj['wz09_custom_geometry'] = True
        obj['wz09_geometry_id'] = str(uuid.uuid4())
    obj['wz09_collision_verified'] = False
    return obj


def _water_material():
    material = bpy.data.materials.new('WZ water · visual only')
    material.diffuse_color = (.025,.22,.31,.75); material.use_nodes = True
    shader = material.node_tree.nodes.get('Principled BSDF')
    shader.inputs['Base Color'].default_value = (.025,.22,.31,1)
    shader.inputs['Roughness'].default_value = .2
    shader.inputs['Metallic'].default_value = .12
    return material


def _water_surfaces(col, waters, material):
    from .terrain_core import river_mesh, sea_mesh
    objects = []
    for water in waters:
        if water['kind']=='sea':
            x,y,z = water['position_m']; w,h = water['extent_m']
            geometry = sea_mesh([x-w/2,y-h/2,x+w/2,y+h/2],z)
        else:
            geometry = river_mesh(dict(water,points=water['points_m']))
        obj = _mesh_object(col, 'Water surface · '+water['kind'], geometry['vertices'],geometry['faces'])
        obj.data.materials.append(material)
        obj['wz09_generated_water_id'] = water['id']
        objects.append(obj)
    return objects


def generate_terrain(ctx):
    from .terrain_core import generate_tile
    scene=ctx.scene
    if ctx.mode!='OBJECT': raise ValueError('Leave Edit Mode before generating terrain')
    regions,waters,settings=_inputs(scene)
    tiles=tile_range(list(scene.wz09_terrain_bounds),settings['cells'],settings['cell_size_m'])
    field=_field(regions,waters,settings)
    source=fingerprint(regions,waters,settings)
    channels=_material_channels(settings)
    samplers=_image_sampler(scene,channels)
    col=_collection(scene,'WZ09 Terrain · '+str(uuid.uuid4())[:8])
    col['wz09_terrain_generation']=True
    created=[]
    try:
        for tx,ty in tiles:
            data=generate_tile(field,tx,ty,cells=settings['cells'],cell_size_m=settings['cell_size_m'])
            if not data['faces']: continue
            obj=_mesh_object(col,f'Terrain {tx:+d},{ty:+d}',data['vertices'],data['faces'])
            created.append(obj)
            obj.data['wz09_generated_terrain_owned']=True
            assign_generated_normals(obj.data,data['normals'])
            uid=str(uuid.uuid4()); width=settings['cells']*settings['cell_size_m']
            bounds=[tx*width,ty*width,(tx+1)*width,(ty+1)*width]
            layer=obj.data.uv_layers.new(name='UVMap')
            for loop in obj.data.loops:
                p=obj.data.vertices[loop.vertex_index].co
                layer.data[loop.index].uv=((p.x-bounds[0])/width,(p.y-bounds[1])/width)
            for biome in channels:
                attr=obj.data.attributes.new('wz09_weight_'+biome,'FLOAT','POINT')
                for item,weights in zip(attr.data,data['biome_weights']):
                    item.value=weights.get(biome,0)
            obj.data.materials.append(_baked_material(scene,field,bounds,uid,samplers,channels))
            record={'id':uid,'tile_x':tx,'tile_y':ty,'cells':settings['cells'],
                'cell_size_m':settings['cell_size_m'],'input_sha256':source,
                'settings':settings,'region_ids':[r['id'] for r in regions if r['confirmed']],
                'water_ids':[w['id'] for w in waters], 'runtime_integrated':False,'collision_verified':False,
                'material_mode':'baked_biome_albedo','texture_images':dict(settings['texture_images'])}
            obj['wz09_terrain_tile_json']=json.dumps(record,sort_keys=True)
        created.extend(_water_surfaces(col,waters,_water_material()) if waters else [])
    except Exception:
        for obj in list(col.objects): bpy.data.objects.remove(obj,do_unlink=True)
        bpy.data.collections.remove(col)
        raise
    # Edge rebuilding uses exactly the same add-on helper as the explicit UI.
    # A failed rebuild restores the previous terrain and source fingerprint.
    rebuilt_edges=_commit_terrain_revision(ctx,col,source,settings)
    if created: _activate(ctx,created[0])
    result={'tiles':sum(bool(o.get('wz09_terrain_tile_json')) for o in created),'objects':len(created),'input_sha256':source}
    if rebuilt_edges:result['edge_closure_rebuilt']=True
    return result


def create_water(ctx,kind,*,activate=True):
    scene=ctx.scene
    uid=str(uuid.uuid4()); pos=list(scene.cursor.location)
    common={'id':uid,'kind':kind,'position_m':pos,'points_m':[],
            'width_m':scene.wz09_river_width,'depth_m':scene.wz09_river_depth,
            'bank_width_m':scene.wz09_river_bank,'level_m':scene.wz09_sea_level,
            'extent_m':list(scene.wz09_sea_extent),'runtime_integrated':False,'collision_verified':False}
    if kind=='sea':common.update(carve_terrain=scene.wz09_sea_carve,coast_width_m=scene.wz09_sea_coast)
    if kind=='river':
        data=bpy.data.curves.new('River path','CURVE');data.dimensions='3D'
        spline=data.splines.new('POLY');spline.points.add(2)
        for p,co in zip(spline.points,[(0,0,0,1),(50,0,-.5,1),(100,25,-1,1)]):p.co=co
        data.bevel_depth=.2;data.bevel_resolution=0
        obj=bpy.data.objects.new('River · edit path with Tab',data)
    else:
        data=bpy.data.meshes.new('Sea extent')
        w,h=common['extent_m']
        data.from_pydata([(-w/2,-h/2,0),(w/2,-h/2,0),(w/2,h/2,0),(-w/2,h/2,0)],
                         [(0,1),(1,2),(2,3),(3,0)],[])
        obj=bpy.data.objects.new('Sea · level and extent',data)
        obj.display_type='WIRE';obj.hide_render=True
        pos[2]=scene.wz09_sea_level
    # Bounds and the river curve are authoring helpers; generated water is separate.
    obj.hide_render=True
    scene.collection.objects.link(obj);obj.location=pos
    obj['wz09_water_json']=json.dumps(common,sort_keys=True)
    obj['wz09_preview_only']=True;obj['wz09_preview_id']=str(uuid.uuid4())
    obj.show_in_front=True;obj.color=(.05,.65,1.,1.)
    if activate:_activate(ctx,obj)
    return obj


def scatter_assets(ctx):
    try:
        from .asset_browser import selected_asset_id, place_asset, _source_asset, _mesh_parts
        from .authoring_helpers import resolve_owner_map
    except ImportError:
        from asset_browser import selected_asset_id, place_asset, _source_asset, _mesh_parts
        from authoring_helpers import resolve_owner_map
    scene=ctx.scene;owner=resolve_owner_map(ctx.active_object)
    if owner is None: raise ValueError('Select a map as the owner of the scattered assets')
    regions,waters,settings=_inputs(scene)
    source=fingerprint(regions,waters,settings)
    if source!=scene.get('wz09_terrain_input_sha256'): raise ValueError('Terrain inputs changed; generate a new terrain revision first')
    filters=authoring.scatter_settings(scene)
    if _stale_scatter_records(scatter_records(scene),scene.get('wz09_terrain_input_sha256',''),filters):
        raise ValueError('Remove outdated scattering first; previous instances no longer fit the terrain. Scatter again afterwards.')
    asset_id=selected_asset_id(scene)
    source_asset=_source_asset(scene,asset_id)
    exclusions=[]
    # Deliberate safety margins around authored interactions, spawns and nodes.
    if filters is None:
        for obj in scene.objects:
            if obj.get('wz09_type') or obj.get('wz09_action_json'):
                if obj.get('wz09_type')=='map_instance':continue
                x,y,_=obj.matrix_world.translation;r=scene.wz09_scatter_exclusion
                if r>0:exclusions.append({'id':str(uuid.uuid4()),'polygon':[[x-r,y-r],[x+r,y-r],[x+r,y+r],[x-r,y+r]]})
    field=_field(regions,waters,settings,exclusions)
    points=[]
    for tx,ty in tile_range(list(scene.wz09_terrain_bounds),settings['cells'],settings['cell_size_m']):
        points.extend(authoring.scatter_from_settings(field,tx,ty,filters,cells=settings['cells'],cell_size_m=settings['cell_size_m'],
            seed=scene.wz09_scatter_seed,spacing_m=scene.wz09_scatter_spacing,density=scene.wz09_scatter_density,
            slope_max_deg=scene.wz09_scatter_slope,
            biome=None if scene.wz09_scatter_biome=='ALL' else scene.wz09_scatter_biome))
    if len(points)>scene.wz09_scatter_limit:
        raise ValueError(f'{len(points)} points exceed the placement cap. Reduce density or increase spacing.')
    if not points:
        raise ValueError('No matching asset positions. Adjust material/height/water filters or minimum spacing.')
    def key(obj):return obj.as_pointer() if hasattr(obj,'as_pointer') else id(obj)
    scene_keys={key(obj) for obj in scene.objects}
    registry=getattr(getattr(bpy,'data',None),'objects',None)
    children={}
    for obj in (registry if registry is not None else scene.objects):
        parent=getattr(obj,'parent',None)
        if parent is not None:children.setdefault(key(parent),[]).append(obj)
    parts=tuple(_mesh_parts(scene,source_asset[2],_children_index=children,_scene_keys=scene_keys))
    integrity_cache={}
    records=[];created=[];uid=str(uuid.uuid4())
    try:
        for point in points:
            position=point.get('position_m',point.get('position'))
            normal=point.get('normal') if scene.wz09_scatter_align else None
            result=place_asset(ctx,asset_id,owner_map=owner,location=position,normal=normal,
                               _verified_source=source_asset,_verified_parts=parts,_integrity_cache=integrity_cache,
                               _activate_result=False)
            root=result['root'] if isinstance(result,dict) else result
            root['wz09_scatter_set_id']=uid;created.append(root)
            records.append(str(root['wz09_asset_placement_id']))
    except Exception:
        for root in created:
            for child in list(root.children):bpy.data.objects.remove(child,do_unlink=True)
            bpy.data.objects.remove(root,do_unlink=True)
        raise
    record={'id':uid,'asset_id':asset_id,'map_instance_id':str(owner['wz09_id']),
        'seed':scene.wz09_scatter_seed,'spacing_m':scene.wz09_scatter_spacing,
        'density':scene.wz09_scatter_density,'slope_max_deg':scene.wz09_scatter_slope,
        'biome':scene.wz09_scatter_biome,'input_sha256':source,'placement_ids':records,
        'runtime_integrated':False,'collision_verified':False}
    if filters is not None:
        record.update(authoring_filters=filters,authoring_filters_sha256=authoring.settings_sha256(filters))
    sets=json.loads(scene.get('wz09_scatter_sets_json','[]'));sets.append(record)
    scene['wz09_scatter_sets_json']=json.dumps(sets,sort_keys=True)
    _activate(ctx,created[-1])
    return record


def scatter_records(scene):
    """Read persistent generated sets; never discover deletion targets by name."""
    records=json.loads(scene.get('wz09_scatter_sets_json','[]'))
    if not isinstance(records,list):raise ValueError('Generated scatter records must be an array')
    ids=set();placements=set()
    for record in records:
        if not isinstance(record,dict):raise ValueError('Invalid generated scatter record')
        for field in ('id','asset_id','map_instance_id','input_sha256'):
            if not isinstance(record.get(field),str) or not record[field].strip():
                raise ValueError('Generated scatter record missing '+field)
        captured='authoring_filters' in record
        if captured!=('authoring_filters_sha256' in record):
            raise ValueError('Generated scatter requires both authoring filter fields')
        if captured and (not isinstance(record['authoring_filters'],(dict,type(None)))
                or not re.fullmatch('[0-9a-f]{64}',str(record['authoring_filters_sha256']))):
            raise ValueError('Generated scatter authoring filter snapshot or SHA-256 is invalid')
        if record['id'] in ids:raise ValueError('Duplicate generated scatter set ID')
        ids.add(record['id'])
        values=record.get('placement_ids')
        if not isinstance(values,list):raise ValueError('Generated scatter placement IDs must be an array')
        for value in values:
            if not isinstance(value,str) or not value.strip() or value in placements:
                raise ValueError('Missing or duplicate generated scatter placement ID')
            placements.add(value)
    return records


def _stale_scatter_records(records,current,filters):
    """One captured scene snapshot serves every generated set in an operation."""
    expected=authoring.settings_sha256(filters)
    stale=0
    for record in records:
        captured=authoring.validate_scatter_settings(record.get('authoring_filters'))
        actual=authoring.settings_sha256(captured)
        if ('authoring_filters_sha256' in record and record['authoring_filters_sha256']!=actual):
            raise ValueError('Generated scatter authoring filter SHA-256 differs from its snapshot')
        stale+=record['input_sha256']!=current or actual!=expected
    return stale


def stale_scatter_count(scene,*,verify_authoring=False):
    """Cheap on draw; explicit bake/export checks current source poses once."""
    current=scene.get('wz09_terrain_input_sha256','')
    records=scatter_records(scene)
    if not verify_authoring:return sum(record['input_sha256']!=current for record in records)
    if not records:return 0
    return _stale_scatter_records(records,current,authoring.scatter_settings(scene))


def scatter_removal_plan(scene):
    """Validate the complete deletion plan before touching Blender objects.

    Only records plus matching persistent placement/set/source/map IDs authorize
    roots. Descendants are removed deepest-first, preserving mesh datablocks.
    Missing roots are already deleted and may be cleaned from stale records.
    Unrecorded placements, native previews and other scenes are never claimed.
    """
    try:from .authoring_helpers import resolve_owner_map
    except ImportError:from authoring_helpers import resolve_owner_map
    try:from .composite_world import _foreign_scene_index
    except ImportError:from composite_world import _foreign_scene_index
    records=scatter_records(scene);objects=list(scene.objects)
    def key(obj):return obj.as_pointer() if hasattr(obj,'as_pointer') else id(obj)
    members={key(obj):obj for obj in objects}
    foreign=_foreign_scene_index(scene,objects)
    registry=getattr(getattr(bpy,'data',None),'objects',None)
    children=None
    if registry is not None:
        children={}
        # Use all Blender objects, not only scene members: a child living in
        # another scene must still block deletion of its local parent.
        for obj in registry:
            parent=getattr(obj,'parent',None)
            if parent is not None:children.setdefault(key(parent),[]).append(obj)
    indexed={};part_ids=set()
    for obj in objects:
        pid=obj.get('wz09_asset_placement_id')
        if pid:
            part_ids.add(pid)
            if obj.get('wz09_asset_placement'):indexed.setdefault(pid,[]).append(obj)
    roots=[];missing=0
    for record in records:
        for pid in record['placement_ids']:
            matches=indexed.get(pid,[])
            if len(matches)>1:raise ValueError('Duplicate scatter placement root: '+pid)
            if not matches:
                # A remaining detached part cannot safely be assigned by name.
                if pid in part_ids:
                    raise ValueError('Scatter placement root missing while parts remain: '+pid)
                missing+=1;continue
            root=matches[0];owner=resolve_owner_map(root)
            if (root.get('wz09_scatter_set_id')!=record['id']
                    or root.get('wz09_asset_id')!=record['asset_id']
                    or owner is None or owner.get('wz09_id')!=record['map_instance_id']):
                raise ValueError('Generated scatter record disagrees with placement ownership: '+pid)
            roots.append(root)
    root_keys={key(obj) for obj in roots};seen=set();ordered=[]
    def visit(obj,path):
        uid=key(obj)
        if uid in path:raise ValueError('Scatter descendant hierarchy contains a cycle')
        if uid in seen:return
        if uid not in members:raise ValueError('Scatter descendant belongs to another scene')
        if getattr(obj,'library',None) or getattr(obj,'override_library',None):
            raise ValueError('Linked source objects cannot be removed as scatter')
        shared=(uid in foreign if foreign is not None else
                any(key(owner)!=key(scene) for owner in getattr(obj,'users_scene',(scene,))))
        if shared:
            raise ValueError('Scatter objects shared with another scene must be made local first')
        if obj.get('wz09_type') in {'map_instance','nav_node'}:
            raise ValueError('Move maps/navigation out of generated scatter before removing it')
        if obj.get('wz09_asset_placement') and uid not in root_keys:
            raise ValueError('Unrelated placement is parented under generated scatter; detach it first')
        if (obj.get('wz09_preview_only') and not any(obj.get(flag) for flag in
                ('wz09_asset_placement','wz09_asset_part','wz09_audio_zone','wz09_audio_contour',
                 'wz09_action_element','wz09_action_contour'))):
            raise ValueError('Native source preview is parented under scatter; detach it first')
        next_path=path|{uid}
        descendants=children.get(uid,()) if children is not None else getattr(obj,'children',())
        for child in descendants:visit(child,next_path)
        seen.add(uid);ordered.append(obj)
    for root in roots:visit(root,set())
    return {'objects':ordered,'sets':len(records),'placements':len(roots),'already_missing':missing}


def remove_generated_scatter(ctx, *, bpy_module=None):
    """Explicit undoable operator backend; no source mesh/material deletion."""
    if getattr(ctx,'mode','OBJECT')!='OBJECT':raise ValueError('Leave Edit Mode before removing generated scatter')
    plan=scatter_removal_plan(ctx.scene)
    api=bpy if bpy_module is None else bpy_module
    if api is None:raise RuntimeError('Blender object API required')
    for obj in plan['objects']:api.data.objects.remove(obj,do_unlink=True)
    ctx.scene['wz09_scatter_sets_json']='[]'
    return {key:value for key,value in plan.items() if key!='objects'}


def serialize_scene(scene):
    try:from .composite_world import serialize_regions
    except ImportError:from composite_world import serialize_regions
    regions=serialize_regions(scene);waters=_waters(scene)
    tiles=[json.loads(o['wz09_terrain_tile_json']) for o in scene.objects
           if o.get('wz09_terrain_tile_json') and not o.get('wz09_archived_generation')]
    if tiles:
        _,_,settings=_inputs(scene);current=fingerprint(regions,waters,settings)
        if any(t['input_sha256']!=current for t in tiles):
            raise ValueError('Terrain is stale: map cores, water or generation settings changed. Generate a new revision.')
    layers=settings.get('material_layers') if tiles else authoring.material_settings(scene)
    sets=scatter_records(scene)
    if sets and _stale_scatter_records(sets,scene.get('wz09_terrain_input_sha256',''),authoring.scatter_settings(scene)):
        raise ValueError('Scatter is stale: terrain or authored filters/sources changed. Remove generated scatter and bake again.')
    # The helper also refuses orphaned owned rims without their source policy.
    # A legacy scene without settings or rims is an explicit no-op there.
    _edge_module().validate_current(scene)
    if not regions and not waters and not tiles and not sets and layers is None:return None
    result={'version':1,'regions':regions,'terrain_tiles':sorted(tiles,key=lambda t:t['id']),
            'water_features':waters,'scatter_sets':sets,'runtime_integrated':False,'physical_scale_verified':False}
    if layers is not None:result['material_layers']=layers
    return result


if bpy:
    class WZ09_OT_boundary_profile_capture(bpy.types.Operator):
        bl_idname='wz09.boundary_profile_capture';bl_label='Capture source heights at map boundary';bl_options={'REGISTER','UNDO'}
        bl_description='Capture source-bound intersection heights for the selected map; ambiguous outlines are rejected. Regenerate terrain afterwards'
        def execute(self,ctx):
            try:
                result=capture_boundary_profile(ctx)
                if result['source_coverage_fraction']<.99:
                    self.report({'WARNING'},'Some authored heights between verified source intersections; ground unverified')
                self.report({'INFO'},'Konturprofil erfasst · jetzt Terrain neu erzeugen')
                return {'FINISHED'}
            except (ValueError,OSError,TypeError,KeyError,RuntimeError) as exc:
                self.report({'ERROR'},str(exc)[:220]);return {'CANCELLED'}

    class WZ09_OT_boundary_profile_remove(bpy.types.Operator):
        bl_idname='wz09.boundary_profile_remove';bl_label='Remove authored outline profile';bl_options={'REGISTER','UNDO'}
        bl_description='Restore the original core heights of the selected map; regenerate terrain afterwards'
        def execute(self,ctx):
            try:
                remove_boundary_profile(ctx)
                self.report({'INFO'},'Konturprofil entfernt · jetzt Terrain neu erzeugen')
                return {'FINISHED'}
            except (ValueError,OSError,TypeError,KeyError,RuntimeError) as exc:
                self.report({'ERROR'},str(exc)[:220]);return {'CANCELLED'}

    class WZ09_OT_terrain_edges_apply(bpy.types.Operator):
        bl_idname='wz09.terrain_edges_apply';bl_label='Blend map boundaries into terrain';bl_options={'REGISTER','UNDO'}
        bl_description='Fit verified ground intersection boundaries to current terrain as authored boundary geometry; retain source data'
        def execute(self,ctx):
            try:
                rebuild_map_edges(ctx)
                self.report({'INFO'},'Mapkanten an das aktuelle Terrain angepasst')
                return {'FINISHED'}
            except (ValueError,OSError,TypeError,KeyError,RuntimeError) as exc:
                self.report({'ERROR'},str(exc)[:220]);return {'CANCELLED'}

    class WZ09_OT_terrain_edges_remove(bpy.types.Operator):
        bl_idname='wz09.terrain_edges_remove';bl_label='Remove generated boundary adjustment';bl_options={'REGISTER','UNDO'}
        bl_description='Remove only the generated authored boundary adjustment; retain native meshes, terrain and original files'
        def execute(self,ctx):
            try:
                remove_map_edges(ctx)
                self.report({'INFO'},'Eigene Randanpassung entfernt')
                return {'FINISHED'}
            except (ValueError,OSError,TypeError,KeyError,RuntimeError) as exc:
                self.report({'ERROR'},str(exc)[:220]);return {'CANCELLED'}

    class WZ09_OT_terrain_generate(bpy.types.Operator):
        bl_idname='wz09.terrain_generate';bl_label='Regenerate terrain';bl_options={'REGISTER','UNDO'}
        def execute(self,ctx):
            try:
                result=generate_terrain(ctx);self.report({'INFO'},f"{result['tiles']} terrain tiles created; previous version archived")
                if stale_scatter_count(ctx.scene):
                    self.report({'WARNING'},'Previous scattering no longer fits: remove generated scattering and regenerate it')
                return {'FINISHED'}
            except (ValueError,OSError,TypeError,KeyError) as exc:self.report({'ERROR'},str(exc)[:220]);return {'CANCELLED'}

    class WZ09_OT_terrain_bounds(bpy.types.Operator):
        bl_idname='wz09.terrain_bounds';bl_label='Work area from map cores';bl_options={'REGISTER','UNDO'}
        def execute(self,ctx):
            from .composite_world import serialize_regions
            points=[p for r in serialize_regions(ctx.scene) for p in r['polygon']]
            if not points:self.report({'ERROR'},'Create map cores first');return {'CANCELLED'}
            m=ctx.scene.wz09_terrain_margin
            ctx.scene.wz09_terrain_bounds=(min(p[0] for p in points)-m,min(p[1] for p in points)-m,
                                        max(p[0] for p in points)+m,max(p[1] for p in points)+m)
            return {'FINISHED'}

    class WZ09_OT_water_add(bpy.types.Operator):
        bl_idname='wz09.water_add';bl_label='Create water';bl_options={'REGISTER','UNDO'}
        kind:EnumProperty(items=[('sea','Sea',''),('river','River','')],default='river')
        def execute(self,ctx):
            try:create_water(ctx,self.kind);return {'FINISHED'}
            except ValueError as exc:self.report({'ERROR'},str(exc));return {'CANCELLED'}

    class WZ09_OT_water_apply(bpy.types.Operator):
        bl_idname='wz09.water_apply';bl_label='Apply water settings';bl_options={'REGISTER','UNDO'}
        def execute(self,ctx):
            obj=ctx.active_object
            if obj is None or not obj.get('wz09_water_json'):self.report({'ERROR'},'Select a river curve or sea');return {'CANCELLED'}
            data=json.loads(obj['wz09_water_json']);s=ctx.scene
            data.update(width_m=s.wz09_river_width,depth_m=s.wz09_river_depth,bank_width_m=s.wz09_river_bank,
                        extent_m=list(s.wz09_sea_extent))
            if data['kind']=='sea':
                obj.location.z=s.wz09_sea_level
                data.update(carve_terrain=s.wz09_sea_carve,coast_width_m=s.wz09_sea_coast)
                if obj.type=='MESH':
                    w,h=data['extent_m']
                    for point,co in zip(obj.data.vertices,[(-w/2,-h/2,0),(w/2,-h/2,0),(w/2,h/2,0),(-w/2,h/2,0)]):point.co=co
                    obj.data.update()
            obj['wz09_water_json']=json.dumps(data,sort_keys=True)
            return {'FINISHED'}

    class WZ09_OT_water_load(bpy.types.Operator):
        bl_idname='wz09.water_load';bl_label='Apply water settings'
        def execute(self,ctx):
            obj=ctx.active_object
            if obj is None or not obj.get('wz09_water_json'):self.report({'ERROR'},'Select river or sea');return {'CANCELLED'}
            data=next(w for w in _waters(ctx.scene) if w['id']==json.loads(obj['wz09_water_json'])['id']);s=ctx.scene
            s.wz09_river_width=data['width_m'];s.wz09_river_depth=data['depth_m'];s.wz09_river_bank=data['bank_width_m']
            s.wz09_sea_level=data['level_m'];s.wz09_sea_extent=data['extent_m']
            s.wz09_sea_carve=data.get('carve_terrain',False);s.wz09_sea_coast=data.get('coast_width_m',30.)
            return {'FINISHED'}

    class WZ09_OT_scatter_generate(bpy.types.Operator):
        bl_idname='wz09.scatter_generate';bl_label='Scatter selected asset';bl_options={'REGISTER','UNDO'}
        def execute(self,ctx):
            try:
                result=scatter_assets(ctx);self.report({'INFO'},f"{len(result['placement_ids'])} quellgebundene Instanzen angelegt")
                return {'FINISHED'}
            except (ValueError,OSError,KeyError,TypeError) as exc:self.report({'ERROR'},str(exc)[:220]);return {'CANCELLED'}

    class WZ09_OT_scatter_remove(bpy.types.Operator):
        bl_idname='wz09.scatter_remove';bl_label='Remove generated scattering';bl_options={'REGISTER','UNDO'}
        bl_description='Remove only verified generated asset instances and their children; retain source models and other placements'
        def execute(self,ctx):
            try:
                result=remove_generated_scatter(ctx)
                self.report({'INFO'},f"{result['placements']} generated placements removed; scattering can be repeated")
                return {'FINISHED'}
            except (ValueError,TypeError,KeyError,RuntimeError) as exc:
                self.report({'ERROR'},str(exc)[:220]);return {'CANCELLED'}

    class WZ09_PT_terrain(bpy.types.Panel):
        bl_idname='WZ09_PT_terrain';bl_label='Terrain - water - scattering';bl_parent_id='WZ09_PT_main'
        bl_space_type='VIEW_3D';bl_region_type='UI';bl_category='WZ2009'
        @classmethod
        def poll(cls,ctx):return ctx.scene.wz09_workspace=='WORLD'
        def draw(self,ctx):
            a=self.layout;s=ctx.scene
            box=a.box();box.label(text='2 - Connect gaps',icon='MOD_OCEAN')
            space=getattr(ctx,'space_data',None)
            if getattr(space,'type',None)=='VIEW_3D':
                box.prop(space.shading,'type',text='Ground view')
                if space.shading.type=='SOLID':box.label(text='Ground blend: use Material Preview',icon='INFO')
            box.prop(s,'wz09_terrain_margin');box.operator('wz09.terrain_bounds',icon='FULLSCREEN_ENTER')
            box.prop(s,'wz09_terrain_bounds',text='XY work area')
            row=box.row(align=True);row.prop(s,'wz09_terrain_cells');row.prop(s,'wz09_terrain_cell_size')
            box.prop(s,'wz09_terrain_advanced')
            if s.wz09_terrain_advanced:
                box.prop(s,'wz09_terrain_seed');box.prop(s,'wz09_terrain_noise');box.prop(s,'wz09_terrain_noise_scale')
                box.prop(s,'wz09_terrain_texture_size');box.prop(s,'wz09_terrain_texture_repeat')
                box.prop(s,'wz09_terrain_fast_bake')
            box.prop(s,'wz09_terrain_show_textures')
            if s.wz09_terrain_show_textures:
                box.label(text='Empty = procedural ground preview',icon='INFO')
                for biome in MATERIALS:box.prop_search(s,'wz09_biome_image_'+biome,bpy.data,'images',text=biome)
            box.operator('wz09.boundary_profile_capture',icon='EYEDROPPER')
            box.operator('wz09.boundary_profile_remove',icon='LOOP_BACK')
            box.operator('wz09.terrain_generate',icon='MOD_DISPLACE')
            box.operator('wz09.terrain_edges_apply',icon='MOD_SHRINKWRAP')
            box.operator('wz09.terrain_edges_remove',icon='TRASH')
            box.label(text='Authored boundary geometry only - collision unverified',icon='INFO')
            box=a.box();box.label(text='3 - Sea and rivers',icon='MOD_FLUID')
            box.prop(s,'wz09_sea_level');box.prop(s,'wz09_sea_extent')
            box.prop(s,'wz09_sea_carve')
            if s.wz09_sea_carve:
                box.prop(s,'wz09_sea_coast')
                box.prop(s,'wz09_river_depth',text='Seabed depth')
            box.prop(s,'wz09_river_width');box.prop(s,'wz09_river_depth');box.prop(s,'wz09_river_bank')
            row=box.row(align=True);row.operator('wz09.water_add',text='Sea',icon='MESH_GRID').kind='sea'
            row.operator('wz09.water_add',text='River',icon='CURVE_PATH').kind='river'
            row=box.row(align=True);row.operator('wz09.water_load',icon='EYEDROPPER');row.operator('wz09.water_apply',icon='CHECKMARK')
            box.label(text='River: Tab - move / extrude points',icon='EDITMODE_HLT')
            box.label(text='Regenerate terrain after changes',icon='FILE_REFRESH')
            box=a.box();box.label(text='4 - Scatter assets naturally',icon='OUTLINER_OB_GROUP_INSTANCE')
            box.label(text='Select an asset in the browser, then select its owner map')
            for prop in ('seed','spacing','density','slope','biome','align','exclusion','limit'):
                box.prop(s,'wz09_scatter_'+prop)
            box.operator('wz09.scatter_generate',icon='PARTICLES')
            try:
                records=scatter_records(s);stale=stale_scatter_count(s)
                if stale:
                    box.label(text='Scattering outdated: remove and regenerate',icon='ERROR')
                if records:box.operator('wz09.scatter_remove',icon='TRASH')
            except (ValueError,TypeError,KeyError):
                box.label(text='Invalid scatter data; inspect IDs',icon='ERROR')
            box.label(text='New UUIDs during baking - runtime unverified',icon='INFO')

    class WZ09_PT_terrain_routes(bpy.types.Panel):
        bl_label='Roads and rivers across map boundaries';bl_idname='WZ09_PT_terrain_routes'
        bl_space_type='VIEW_3D';bl_region_type='UI';bl_category='WZ2009'
        bl_parent_id='WZ09_PT_terrain';bl_options={'DEFAULT_CLOSED'}
        def draw(self,ctx):
            from . import terrain_routes
            terrain_routes.draw(self.layout,ctx)

    CLASSES=(WZ09_OT_boundary_profile_capture,WZ09_OT_boundary_profile_remove,WZ09_OT_terrain_generate,WZ09_OT_terrain_edges_apply,WZ09_OT_terrain_edges_remove,WZ09_OT_terrain_bounds,WZ09_OT_water_add,WZ09_OT_water_apply,WZ09_OT_water_load,
             WZ09_OT_scatter_generate,WZ09_OT_scatter_remove,WZ09_PT_terrain,WZ09_PT_terrain_routes)


def register():
    if not bpy:return
    for cls in CLASSES:bpy.utils.register_class(cls)
    props={
        'terrain_seed':IntProperty(name='Terrain-Seed',default=2009,min=0),
        'terrain_cells':IntProperty(name='Cells per tile',default=32,min=4,max=128),
        'terrain_cell_size':FloatProperty(name='Grid spacing',default=20,min=.1,subtype='DISTANCE'),
        'terrain_bounds':FloatVectorProperty(name='Terrain area',size=4,default=(-500,-500,500,500)),
        'terrain_margin':FloatProperty(name='Margin around cores',default=150,min=0,subtype='DISTANCE'),
        'terrain_noise':FloatProperty(name='Terrain detail height',default=2,min=0,subtype='DISTANCE'),
        'terrain_noise_scale':FloatProperty(name='Terrain detail width',default=180,min=1,subtype='DISTANCE'),
        'terrain_texture_size':IntProperty(name='Ground texture pixels',default=128,min=32,max=512),
        'terrain_texture_repeat':FloatProperty(name='Texture repeat',default=12,min=.1,subtype='DISTANCE'),
        'terrain_fast_bake':BoolProperty(name='Detailed material splat textures',default=False,
            description='Interpolate material weights, preserve native texture detail and accelerate high-resolution bakes'),
        'terrain_show_textures':BoolProperty(name='Bind source textures',default=False),
        'terrain_advanced':BoolProperty(name='Detail / seed / texture resolution',default=False),
        'sea_level':FloatProperty(name='Sea level',default=-10,subtype='DISTANCE'),
        'sea_extent':FloatVectorProperty(name='Sea width / length',size=2,default=(4000,4000),min=1),
        'sea_carve':BoolProperty(name='Shape seabed',default=False,description='Lower only generated terrain inside the sea rectangle; protected map cores remain intact'),
        'sea_coast':FloatProperty(name='Coast transition',default=30,min=.1,subtype='DISTANCE'),
        'river_width':FloatProperty(name='River width',default=12,min=.1,subtype='DISTANCE'),
        'river_depth':FloatProperty(name='Riverbed depth',default=3,min=.1,subtype='DISTANCE'),
        'river_bank':FloatProperty(name='Bank transition',default=10,min=.1,subtype='DISTANCE'),
        'scatter_seed':IntProperty(name='Scatter seed',default=2009,min=0),
        'scatter_spacing':FloatProperty(name='Minimum spacing',default=35,min=.5,subtype='DISTANCE'),
        'scatter_density':FloatProperty(name='Density',default=.15,min=0,max=1),
        'scatter_slope':FloatProperty(name='Maximum slope angle',default=30,min=0,max=89),
        'scatter_biome':EnumProperty(name='Ground filter',items=[('ALL','All','')]+[(b,b,'') for b in BIOMES]),
        'scatter_align':BoolProperty(name='Align to slope (rocks)',default=False),
        'scatter_exclusion':FloatProperty(name='Distance to actions/navigation',default=5,min=0,subtype='DISTANCE'),
        'scatter_limit':IntProperty(name='Maximum instances per bake',default=1000,min=1,max=5000)}
    props.update({'biome_image_'+b:StringProperty(name=b+'-Textur',default='') for b in MATERIALS})
    for name,prop in props.items():setattr(bpy.types.Scene,'wz09_'+name,prop)
    globals()['_PROP_NAMES']=list(props)


def unregister():
    if not bpy:return
    for name in globals().get('_PROP_NAMES',[]):
        if hasattr(bpy.types.Scene,'wz09_'+name):delattr(bpy.types.Scene,'wz09_'+name)
    for cls in reversed(CLASSES):bpy.utils.unregister_class(cls)
