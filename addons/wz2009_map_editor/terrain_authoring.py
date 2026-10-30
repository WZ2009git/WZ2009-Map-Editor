"""Editable terrain material masks, typed filters and bounded diagnostics UI.

Reuses terrain_core/terrain_extensions and existing generated tile metadata.
No source mesh is edited, no native ground/collision is guessed, and opening a
panel never performs a geometry bake, source import or complete diagnostics.
"""
import copy
import hashlib
import json
import math
import uuid

try:
    from . import terrain_core as core, terrain_extensions as extensions
    from .authoring_helpers import resolve_owner_map
except ImportError:
    import terrain_core as core
    import terrain_extensions as extensions
    from authoring_helpers import resolve_owner_map

_CLASSES=[]
MATERIAL_LABELS={'sand':'Sand','soil':'Erde','grass':'Gras','snow':'Schnee','rock':'Fels','asphalt':'Asphalt'}


def _json(owner,key,default):
    try:
        return json.loads(owner.get(key,json.dumps(default)),parse_constant=lambda v:
            (_ for _ in ()).throw(ValueError('Non-finite JSON value')))
    except (ValueError,TypeError) as exc:
        raise ValueError(key+': invalid authoring metadata') from exc


def settings_sha256(record):
    return hashlib.sha256(json.dumps(record,sort_keys=True,separators=(',',':'),allow_nan=False).encode()).hexdigest()


def boundary_ring(vertices,faces):
    """One simple selected-face boundary; holes/islands/nonmanifold rings fail."""
    if not isinstance(vertices,(list,tuple)) or not isinstance(faces,(list,tuple)) or not faces:
        raise ValueError('Select one connected polygon surface')
    points=[core._xyz(v,'Maskenpunkt') for v in vertices]
    edges={}
    for face in faces:
        if not isinstance(face,(list,tuple)) or len(face)<3:
            raise ValueError('Mask surface requires at least three vertices')
        indices=[core._integer(i,'Maskenindex',0,len(points)-1) for i in face]
        if len(set(indices))!=len(indices):
            raise ValueError('Duplicate surface vertices')
        for a,b in zip(indices,indices[1:]+indices[:1]):
            edge=tuple(sorted((a,b)));edges[edge]=edges.get(edge,0)+1
            if edges[edge]>2:
                raise ValueError('Nichtmannigfaltige Maskenauswahl')
    adjacency={}
    for (a,b),count in edges.items():
        if count==1:
            adjacency.setdefault(a,[]).append(b);adjacency.setdefault(b,[]).append(a)
    if len(adjacency)<3 or any(len(neighbors)!=2 for neighbors in adjacency.values()):
        raise ValueError('Mask selection must have exactly one closed boundary')
    start=min(adjacency);ring=[start];previous=None;current=start
    while True:
        next_index=min(v for v in adjacency[current] if v!=previous)
        if next_index==start:
            break
        if next_index in ring:
            raise ValueError('Maskenrand kreuzt sich')
        ring.append(next_index);previous,current=current,next_index
    if len(ring)!=len(adjacency):
        raise ValueError('Multiple islands or holes: select a single surface')
    return [points[i] for i in ring]


def _world_point(matrix,point):
    point=core._xyz(tuple(point[:3]),'Maskenpunkt')
    return core._xyz(tuple(sum(float(matrix[i][j])*point[j] for j in range(3))+float(matrix[i][3]) for i in range(3)),
                     'Maskenpunkt in Weltkoordinaten')


def _planar_polygon(points):
    if not points or max(p[2] for p in points)-min(p[2] for p in points)>1e-4:
        raise ValueError('Masks must be horizontal in world XY; draw an outline at the cursor or flatten Z')
    return [list(p) for p in core.validate_polygon([p[:2] for p in points])]


def _helper_polygon(obj):
    if getattr(obj,'type','')=='CURVE':
        if len(obj.data.splines)!=1:
            raise ValueError('Mask helper requires exactly one closed poly curve')
        spline=obj.data.splines[0]
        if spline.type!='POLY' or not spline.use_cyclic_u:
            raise ValueError('Mask must remain a closed poly curve')
        points=[_world_point(obj.matrix_world,p.co) for p in spline.points]
    elif getattr(obj,'type','')=='MESH' and len(obj.data.polygons)==1:
        points=[_world_point(obj.matrix_world,obj.data.vertices[i].co) for i in obj.data.polygons[0].vertices]
    else:
        raise ValueError('A single polygon or closed poly curve is required')
    return _planar_polygon(points)


def _mask_record(obj,exclusion=False):
    key='wz09_scatter_exclusion_json' if exclusion else 'wz09_material_mask_json'
    record=_json(obj,key,{})
    if not isinstance(record,dict) or record.get('id')!=obj.get('wz09_terrain_mask_id'):
        raise ValueError('Mask ID differs from the saved record')
    record['polygon']=_helper_polygon(obj)
    if exclusion:
        return dict(id=core._identifier(record.get('id')),polygon=record['polygon'])
    return extensions.validate_material_masks([record])[0]


def material_settings(scene):
    """Canonical live data, or None for unchanged legacy scenes; no new IDs."""
    rules=extensions.validate_material_rules(_json(scene,'wz09_material_rules_json',[]))
    helpers=[o for o in scene.objects if o.get('wz09_material_mask_json')]
    helpers.sort(key=lambda o:(core._integer(o.get('wz09_terrain_mask_order',0),'Maskenreihenfolge',0,1000000),
                               str(o.get('wz09_terrain_mask_id',''))))
    masks=extensions.validate_material_masks([_mask_record(o) for o in helpers])
    if not rules and not masks:
        return None
    return validate_material_settings(dict(kind='wz09_terrain_material_layers',version=1,channels=list(extensions.MATERIALS),
        rules=rules,masks=masks,coordinate_space='world_editor_design_metres',physical_scale_verified=False))


def validate_material_settings(record,*,all_ids=None):
    """Strict scene-independent canonical material product, or None legacy."""
    if record is None:return None
    fields={'kind','version','channels','rules','masks','coordinate_space','physical_scale_verified'}
    if (not isinstance(record,dict) or set(record)!=fields or record.get('kind')!='wz09_terrain_material_layers'
            or type(record.get('version')) is not int or record['version']!=1
            or record.get('channels')!=list(extensions.MATERIALS)
            or record.get('coordinate_space')!='world_editor_design_metres'
            or record.get('physical_scale_verified') is not False):
        raise ValueError('Terrainmaterial: exakter kanonischer Version-1-Datensatz erforderlich')
    rules=extensions.validate_material_rules(record['rules'])
    masks=extensions.validate_material_masks(record['masks'])
    ids=set(all_ids or ())
    for original,validated in ((record['rules'],rules),(record['masks'],masks)):
        for source,normalized in zip(original,validated):
            if set(source)!=set(normalized):raise ValueError('Material layer fields differ from version 1')
            if normalized['id'] in ids:raise ValueError('Doppelte globale Materiallagen-ID: '+normalized['id'])
            ids.add(normalized['id'])
    result=copy.deepcopy(record);result.update(rules=rules,masks=masks)
    return result


def decorate_from_settings(base_field,settings):
    if settings is None:
        return base_field
    settings=validate_material_settings(settings)
    return extensions.MaterialField(base_field,rules=settings['rules'],masks=settings['masks'])


def decorate_field(scene,base_field):
    return decorate_from_settings(base_field,material_settings(scene))


def upsert_rule(scene,record):
    record=extensions.validate_material_rules([record])[0]
    rules=extensions.validate_material_rules(_json(scene,'wz09_material_rules_json',[]))
    for index,item in enumerate(rules):
        if item['id']==record['id']:
            rules[index]=record;break
    else:
        rules.append(record)
    scene['wz09_material_rules_json']=json.dumps(rules,sort_keys=True,allow_nan=False)
    return record


def remove_rule(scene,rule_id):
    rules=extensions.validate_material_rules(_json(scene,'wz09_material_rules_json',[]))
    scene['wz09_material_rules_json']=json.dumps([r for r in rules if r['id']!=rule_id],sort_keys=True)


def move_rule(scene,rule_id,direction):
    if direction not in (-1,1):raise ValueError('Rule direction must be -1 or 1')
    rules=extensions.validate_material_rules(_json(scene,'wz09_material_rules_json',[]))
    index=next((i for i,r in enumerate(rules) if r['id']==rule_id),None)
    if index is None:raise ValueError('Selected rule no longer exists')
    target=index+direction
    if 0<=target<len(rules):rules[index],rules[target]=rules[target],rules[index]
    scene['wz09_material_rules_json']=json.dumps(rules,sort_keys=True,allow_nan=False)


def _selected_points(context):
    obj=context.active_object
    if obj is None or getattr(obj,'type','')!='MESH':
        raise ValueError('Select a polygon surface or create a mask at the cursor')
    if getattr(context,'mode','OBJECT')=='EDIT_MESH':
        import bmesh
        mesh=bmesh.from_edit_mesh(obj.data)
        faces=[f for f in mesh.faces if f.select]
        vertices=[];indices={};polygons=[]
        for face in faces:
            polygon=[]
            for vertex in face.verts:
                if vertex not in indices:
                    indices[vertex]=len(vertices);vertices.append(_world_point(obj.matrix_world,vertex.co))
                polygon.append(indices[vertex])
            polygons.append(polygon)
        points=boundary_ring(vertices,polygons)
    else:
        if len(obj.data.polygons)!=1:
            raise ValueError('Select connected faces in Edit Mode; outside scenery is not outlined automatically')
        points=[_world_point(obj.matrix_world,obj.data.vertices[i].co) for i in obj.data.polygons[0].vertices]
    _planar_polygon(points)
    return points


def create_mask(context,*,from_selection=False,exclusion=False):
    """Create a separate editable curve. Original selection/data remain intact."""
    import bpy
    from mathutils import Matrix
    settings=context.scene.wz09_terrain_authoring
    selected=context.active_object
    if from_selection:
        points=_selected_points(context)
        if getattr(context,'mode','OBJECT')=='EDIT_MESH':
            bpy.ops.object.mode_set(mode='OBJECT')
    else:
        x,y,z=context.scene.cursor.location
        width,depth=settings.mask_width_m,settings.mask_depth_m
        if settings.mask_shape=='CIRCLE':
            points=[(x+math.cos(i*math.tau/64)*width/2,y+math.sin(i*math.tau/64)*depth/2,z) for i in range(64)]
        else:
            points=[(x-width/2,y-depth/2,z),(x+width/2,y-depth/2,z),
                    (x+width/2,y+depth/2,z),(x-width/2,y+depth/2,z)]
    polygon=_planar_polygon(points)
    identity=str(uuid.uuid4())
    record=dict(id=identity,polygon=polygon) if exclusion else dict(id=identity,polygon=polygon,
        material=settings.material,strength=settings.strength,falloff_m=settings.falloff_m,enabled=settings.enabled)
    if not exclusion:
        record=extensions.validate_material_masks([record])[0]
    curve=bpy.data.curves.new('WZ Terrain Mask','CURVE');curve.dimensions='3D'
    spline=curve.splines.new('POLY');spline.points.add(len(points)-1);spline.use_cyclic_u=True
    center=tuple(sum(p[i] for p in points)/len(points) for i in range(3))
    for target,point in zip(spline.points,points):
        target.co=(*(point[i]-center[i] for i in range(3)),1)
    obj=bpy.data.objects.new('Exclusion area' if exclusion else 'Bodenmaske · '+MATERIAL_LABELS[record['material']],curve)
    context.scene.collection.objects.link(obj)
    owner=resolve_owner_map(selected)
    if owner is not None:
        obj.parent=owner
    obj.matrix_world=Matrix.Translation(center)
    obj['wz09_terrain_mask_id']=identity
    obj['wz09_terrain_mask_order']=max((o.get('wz09_terrain_mask_order',-1) for o in context.scene.objects),default=-1)+1
    obj['wz09_scatter_exclusion_json' if exclusion else 'wz09_material_mask_json']=json.dumps(record,sort_keys=True)
    obj['wz09_editor_overlay']=True;obj['wz09_preview_only']=True;obj['wz09_preview_id']=str(uuid.uuid4())
    obj['wz09_mask_source_json']=json.dumps(dict(kind='explicit_selected_polygon' if from_selection else 'explicit_editor_contour',
        source_object_id=next((selected.get(k) for k in ('wz09_geometry_id','wz09_asset_placement_id','wz09_preview_id','wz09_id')
            if selected is not None and selected.get(k)),None),collision_verified=False),sort_keys=True)
    obj.hide_render=True;obj.show_in_front=True;obj.display_type='WIRE'
    obj.color=(1,.3,.1,1) if exclusion else (.2,.95,.45,1)
    for previous in context.selected_objects:
        previous.select_set(False)
    obj.select_set(True);context.view_layer.objects.active=obj
    return obj


def _near_bounds(points,bounds,margin):
    return not (max(p[0] for p in points)<bounds[0]-margin or min(p[0] for p in points)>bounds[2]+margin
        or max(p[1] for p in points)<bounds[1]-margin or min(p[1] for p in points)>bounds[3]+margin)


def scatter_settings(scene):
    """Snapshot exact source IDs/poses and explicit filters once per scatter bake."""
    s=getattr(scene,'wz09_terrain_authoring',None)
    if s is None or not s.scatter_extended:
        return None
    low=s.scatter_min_height_m if s.scatter_height_min_enabled else None
    high=s.scatter_max_height_m if s.scatter_height_max_enabled else None
    low=None if low is None else core._number(low,'Minimum scatter height')
    high=None if high is None else core._number(high,'Maximum scatter height')
    if low is not None and high is not None and high<low:
        raise ValueError('Maximum scatter height is below the minimum')
    bounds=[core._number(v,'Scatter-Arbeitsbereich') for v in scene.wz09_terrain_bounds]
    if len(bounds)!=4 or bounds[2]<=bounds[0] or bounds[3]<=bounds[1]:
        raise ValueError('Define a scatter work area first')
    # Include the Matérn neighbor halo, so independently baked adjacent tiles
    # retain the same candidate eligibility near working-area borders.
    halo=3*core._number(getattr(scene,'wz09_scatter_spacing',5),'Scatter spacing',positive=True)
    filters=dict(min_height_m=low,max_height_m=high,min_water_distance_m=s.scatter_water_min_m,
        max_water_distance_m=s.scatter_water_max_m if s.scatter_water_max_enabled else None,
        navigation_clearance_m=s.scatter_nav_clearance_m,interaction_clearance_m=s.scatter_action_clearance_m,
        collision_clearance_m=s.scatter_collision_clearance_m,
        material=None if s.scatter_material=='ALL' else s.scatter_material,
        material_min_weight=s.scatter_material_weight,allow_underwater=s.scatter_underwater)
    for key in ('min_water_distance_m','navigation_clearance_m','interaction_clearance_m','collision_clearance_m'):
        filters[key]=core._number(filters[key],key,nonnegative=True)
    if filters['max_water_distance_m'] is not None:
        filters['max_water_distance_m']=core._number(filters['max_water_distance_m'],'max_water_distance_m',nonnegative=True)
    filters['material_min_weight']=extensions._unit(filters['material_min_weight'],'material_min_weight')
    if filters['material'] is not None and filters['material'] not in extensions.MATERIALS:
        raise ValueError('Unbekanntes Scattermaterial')
    if not isinstance(filters['allow_underwater'],bool):raise ValueError('Underwater filter requires a boolean')
    clearances=[];exclusions=[];sources=[];nodes={};action_objects=[];prefab_objects=[]
    for obj in scene.objects:
        if obj.get('wz09_type')=='nav_node' and s.scatter_nav_clearance_m>0:
            uid=core._identifier(obj.get('wz09_id'),'NavNodeId')
            if uid in nodes:
                raise ValueError('Doppelte NavNodeId')
            if obj.get('wz09_links',''):
                raise ValueError('Migrate legacy navigation links with the existing navigation tool before scattering')
            nodes[uid]=core._xyz(tuple(obj.matrix_world.translation))
        if obj.get('wz09_prefab') and s.scatter_action_clearance_m>0:
            prefab_objects.append(obj)
        elif obj.get('wz09_action_element') and s.scatter_action_clearance_m>0:
            action_objects.append(obj)
        if obj.get('wz09_scatter_exclusion_json'):
            record=_mask_record(obj,True)
            if _near_bounds(record['polygon'],bounds,halo+s.scatter_collision_clearance_m):
                exclusions.append(record)
                clearances.append(dict(id=record['id'],kind='collision',points=record['polygon']+[record['polygon'][0]],radius_m=0))
                sources.append(dict(id=record['id'],kind='explicit_xy_exclusion',collision_verified=False))
    for uid,point in nodes.items():
        if _near_bounds([point],bounds,halo+s.scatter_nav_clearance_m):
            clearances.append(dict(id=uid,kind='navigation',points=[list(point[:2])],radius_m=0))
            sources.append(dict(id=uid,kind='nav_node'))
    if s.scatter_nav_clearance_m>0:
        try:
            from .scene_io import _edge_records
        except ImportError:
            from scene_io import _edge_records
        object_ids={o.get('wz09_id') for o in scene.objects if o.get('wz09_type')}
        for edge in _edge_records(scene,set(nodes),object_ids):
            if edge.get('enabled') is False:
                continue
            points=[list(nodes[edge[key]][:2]) for key in ('from','to')]
            if _near_bounds(points,bounds,halo+s.scatter_nav_clearance_m):
                clearances.append(dict(id=edge['id'],kind='navigation',points=points,radius_m=0))
                sources.append(dict(id=edge['id'],kind='nav_edge',status=edge['status'],from_id=edge['from'],to_id=edge['to']))
    try:
        from .audio_zones import _object_record as action_pose
    except ImportError:
        from audio_zones import _object_record as action_pose
    for obj in action_objects:
        record,scale=action_pose(obj,'wz09_action_json')
        if record['id']!=obj.get('wz09_action_id'):
            raise ValueError('ActionElementId differs from saved sources')
        core._identifier(record['id'],'ActionElementId')
        if type(record.get('enabled')) is not bool or record.get('runtime_integrated') is not False:
            raise ValueError('Action source requires a boolean enabled flag and unverified runtime')
        if not record['enabled']:
            continue
        if max(scale)-min(scale)>1e-5:
            raise ValueError('Action distances require uniform scale')
        radius=core._number(record['radius_m'],'Aktionsradius',positive=True)*scale[0]
        point=record['position_m'][:2]
        if _near_bounds([point],bounds,halo+s.scatter_action_clearance_m+radius):
            clearances.append(dict(id=record['id'],kind='interaction',points=[point],radius_m=radius))
            sources.append(dict(id=record['id'],kind='action_element'))
    if prefab_objects:
        try:
            from .prefab_authoring import _object_record as prefab_pose
        except ImportError:
            from prefab_authoring import _object_record as prefab_pose
        for obj in prefab_objects:
            record=prefab_pose(obj)
            if not record['parameters']['enabled']:
                continue
            radius=record['parameters']['interaction_radius_m'];point=record['position_m'][:2]
            if _near_bounds([point],bounds,halo+s.scatter_action_clearance_m+radius):
                clearances.append(dict(id=record['id'],kind='interaction',points=[point],radius_m=radius))
                sources.append(dict(id=record['id'],kind='gameplay_prefab',action_element_id=record['action_element_id']))
    clearances=[dict(record,points=[list(p) for p in record['points']])
        for record in extensions.validate_clearances(sorted(clearances,key=lambda r:r['id']))]
    if filters['max_water_distance_m'] is not None and filters['max_water_distance_m']<filters['min_water_distance_m']:
        raise ValueError('Maximum water distance is below the minimum')
    return validate_scatter_settings(dict(kind='wz09_scatter_authoring_filters',version=1,filters=filters,
        clearances=clearances,exclusions=sorted(exclusions,key=lambda r:r['id']),source_refs=sorted(sources,key=lambda r:r['id']),
        physical_scale_verified=False,collision_verified=False,navigation_verified=False))


def validate_scatter_settings(record,*,world=None,prefabs=None):
    """Strict scene-independent snapshot validation, with optional world refs.

    ``prefabs`` may be the separate prefab product or its records. Opaque/absent
    prefab products are not implicitly searched. Explicit exclusion polygons
    are self-contained authored envelopes; they never claim native collision.
    """
    if record is None:return None
    keys={'kind','version','filters','clearances','exclusions','source_refs',
          'physical_scale_verified','collision_verified','navigation_verified'}
    if (not isinstance(record,dict) or set(record)!=keys or record.get('kind')!='wz09_scatter_authoring_filters'
            or type(record.get('version')) is not int or record['version']!=1):
        raise ValueError('Scatterfilter: exakter Version-1-Datensatz erforderlich')
    result=copy.deepcopy(record)
    if any(result[k] is not False for k in ('physical_scale_verified','collision_verified','navigation_verified')):
        raise ValueError('Scatter filters cannot verify collision, navigation or physical calibration')
    f=result['filters']
    expected={'min_height_m','max_height_m','min_water_distance_m','max_water_distance_m',
        'navigation_clearance_m','interaction_clearance_m','collision_clearance_m','material',
        'material_min_weight','allow_underwater'}
    if not isinstance(f,dict) or set(f)!=expected:raise ValueError('Scatter filter fields differ from version 1')
    for key in ('min_height_m','max_height_m'):
        if f[key] is not None:f[key]=core._number(f[key],key)
    if f['min_height_m'] is not None and f['max_height_m'] is not None and f['max_height_m']<f['min_height_m']:
        raise ValueError('Maximum scatter height is below the minimum')
    for key in ('min_water_distance_m','navigation_clearance_m','interaction_clearance_m','collision_clearance_m'):
        f[key]=core._number(f[key],key,nonnegative=True)
    if f['max_water_distance_m'] is not None:
        f['max_water_distance_m']=core._number(f['max_water_distance_m'],'max_water_distance_m',nonnegative=True)
        if f['max_water_distance_m']<f['min_water_distance_m']:raise ValueError('Water distance limits are reversed')
    if f['material'] is not None and f['material'] not in extensions.MATERIALS:raise ValueError('Unbekanntes Scattermaterial')
    f['material_min_weight']=extensions._unit(f['material_min_weight'],'material_min_weight')
    if type(f['allow_underwater']) is not bool:raise ValueError('Unterwasserfilter muss boolesch sein')
    for value in extensions._records(result['clearances'],'clearances',4096):
        if set(value)!={'id','kind','points','radius_m'}:raise ValueError('Unbekannte Abstandsfelder')
    result['clearances']=[dict(r,points=[list(p) for p in r['points']]) for r in
        extensions.validate_clearances(result['clearances'])]
    polygons=[]
    for value in extensions._records(result['exclusions'],'exclusions'):
        if set(value)!={'id','polygon'}:raise ValueError('Unknown exclusion area fields')
        polygons.append(dict(id=value['id'],polygon=[list(p) for p in core.validate_polygon(value['polygon'])]))
    result['exclusions']=polygons
    by_clear={r['id']:r for r in result['clearances']};by_exclusion={r['id']:r for r in polygons}
    sources=extensions._records(result['source_refs'],'source_refs',4096)
    if {s['id'] for s in sources}!=set(by_clear):raise ValueError('Distance sources must identify all envelopes exactly')
    source_fields={'nav_node':{'id','kind'},'nav_edge':{'id','kind','status','from_id','to_id'},
        'action_element':{'id','kind'},'gameplay_prefab':{'id','kind','action_element_id'},
        'explicit_xy_exclusion':{'id','kind','collision_verified'}}
    for source in sources:
        kind=source.get('kind');clear=by_clear[source['id']]
        if kind not in source_fields or set(source)!=source_fields[kind]:raise ValueError('Unbekannte Scatter-Quellreferenz')
        expected_kind='navigation' if kind.startswith('nav_') else 'collision' if kind=='explicit_xy_exclusion' else 'interaction'
        if clear['kind']!=expected_kind:raise ValueError('Distance type differs from source type')
        if kind=='nav_edge':
            if source['status']!='proposed':raise ValueError('Navigation distance must not assert a verified passage')
            for key in ('from_id','to_id'):core._identifier(source[key],key)
            if source['from_id']==source['to_id'] or len(clear['points'])!=2 or clear['radius_m']!=0:
                raise ValueError('Navigation edge requires two distinct referenced endpoints')
        elif kind=='explicit_xy_exclusion':
            polygon=by_exclusion.get(source['id'])
            if (source['collision_verified'] is not False or polygon is None or clear['radius_m']!=0
                    or clear['points']!=polygon['polygon']+[polygon['polygon'][0]]):
                raise ValueError('Exclusion boundary differs from the declared polygon surface')
        else:
            if len(clear['points'])!=1:raise ValueError('Point source requires exactly one point')
            if kind=='nav_node' and clear['radius_m']!=0:raise ValueError('Navigation node radius is not verified')
            if kind=='gameplay_prefab' and source['action_element_id'] is not None:
                core._identifier(source['action_element_id'],'action_element_id')
    if set(by_exclusion)!={s['id'] for s in sources if s['kind']=='explicit_xy_exclusion'}:
        raise ValueError('Exclusion area and source references differ')
    if world is not None:
        if not isinstance(world,dict):raise ValueError('World manifest must be an object')
        nodes={r['id']:r for r in world.get('nav',{}).get('nodes',[])}
        edges={r['id']:r for r in world.get('nav',{}).get('edges',[])}
        action_refs={r['id']:r for r in world.get('action_elements',[])}
        def same_point(a,b):return all(abs(x-y)<=1e-6 for x,y in zip(a,b))
        for source in sources:
            kind=source['kind'];clear=by_clear[source['id']]
            if kind=='nav_node':
                target=nodes.get(source['id'])
                if target is None or not same_point(clear['points'][0],core._xyz(target['position_m'])[:2]):
                    raise ValueError('Navigation node source missing or moved after scattering')
            elif kind=='nav_edge':
                target=edges.get(source['id'])
                if (target is None or target.get('enabled') is False or target.get('status')!=source['status']
                        or target.get('from')!=source['from_id'] or target.get('to')!=source['to_id']
                        or any(uid not in nodes for uid in (source['from_id'],source['to_id']))):
                    raise ValueError('Navigation edge source missing or changed after scattering')
                if any(not same_point(point,core._xyz(nodes[source[key]]['position_m'])[:2])
                        for point,key in zip(clear['points'],('from_id','to_id'))):
                    raise ValueError('Navkantenendpunkte wurden nach dem Scatter verschoben')
            elif kind=='action_element':
                target=action_refs.get(source['id'])
                if (target is None or target.get('enabled') is not True
                        or not same_point(clear['points'][0],core._xyz(target['position_m'])[:2])
                        or abs(clear['radius_m']-target['radius_m'])>1e-6):
                    raise ValueError('Action source missing or changed after scattering')
    if prefabs is not None:
        values=prefabs.get('prefabs') if isinstance(prefabs,dict) else prefabs
        if not isinstance(values,list):raise ValueError('Prefab product requires records')
        known={r['id']:r for r in values}
        for source in sources:
            if source['kind']!='gameplay_prefab':continue
            target=known.get(source['id']);clear=by_clear[source['id']]
            if (target is None or target['parameters']['enabled'] is not True
                    or target['action_element_id']!=source['action_element_id']
                    or any(abs(a-b)>1e-6 for a,b in zip(clear['points'][0],core._xyz(target['position_m'])[:2]))
                    or abs(clear['radius_m']-target['parameters']['interaction_radius_m'])>1e-6):
                raise ValueError('Prefab distance source missing or changed')
    for key in ('clearances','exclusions','source_refs'):result[key]=sorted(result[key],key=lambda r:r['id'])
    return result


def scatter_from_settings(field,tile_x,tile_y,settings,**base_options):
    """Use one captured settings record for every tile; no repeated scene scans."""
    if settings is None:
        return core.scatter_tile(field,tile_x,tile_y,**base_options)
    if settings.get('kind')!='wz09_scatter_authoring_filters' or settings.get('version')!=1:
        raise ValueError('Unbekannte Scatter-Filterversion')
    settings=validate_scatter_settings(settings)
    options=dict(base_options)
    options.pop('biome',None);options.pop('biome_min_weight',None)
    options.update(settings['filters'])
    return extensions.scatter_filtered_tile(field,tile_x,tile_y,clearances=settings['clearances'],
        exclusions=settings['exclusions'],**options)


def create_support_probe(context):
    """An explicitly chosen ground-contact point, not an inferred asset origin."""
    import bpy
    from mathutils import Matrix
    obj=bpy.data.objects.new('Terrainkontakt · G bewegen',None)
    context.scene.collection.objects.link(obj)
    owner=resolve_owner_map(context.active_object)
    if owner is not None:obj.parent=owner
    obj.matrix_world=Matrix.Translation(context.scene.cursor.location)
    obj.empty_display_type='PLAIN_AXES';obj.empty_display_size=1
    obj['wz09_support_probe_id']=str(uuid.uuid4())
    obj['wz09_support_max_gap_m']=context.scene.wz09_terrain_authoring.support_gap_m
    obj['wz09_editor_overlay']=True;obj.show_in_front=True;obj.show_name=True;obj.hide_render=True
    for item in context.selected_objects:item.select_set(False)
    obj.select_set(True);context.view_layer.objects.active=obj
    return obj


def diagnose_scene(context,*,all_current=False):
    """Inspect actual selected/current generated geometry, including edited UVs."""
    try:
        from . import terrain_workflow as workflow
    except ImportError:
        import terrain_workflow as workflow
    scene=context.scene;s=scene.wz09_terrain_authoring
    if getattr(context,'mode','OBJECT')!='OBJECT':
        raise ValueError('Press Tab to exit Edit Mode before diagnostics')
    objects=[o for o in (scene.objects if all_current else context.selected_objects)
        if o.get('wz09_terrain_tile_json') and not o.get('wz09_archived_generation')]
    if not objects:
        raise ValueError('Select current generated terrain tiles')
    plans=[];tile_ids=[];used_vertices=0
    for obj in objects:
        if getattr(obj,'type','')!='MESH':
            raise ValueError('Terrain tiles must be meshes')
        metadata=_json(obj,'wz09_terrain_tile_json',{})
        mesh=obj.data;mesh.calc_loop_triangles()
        used_vertices+=3*len(mesh.loop_triangles)
        if used_vertices>s.diagnostic_vertex_budget:
            raise ValueError('Diagnostic budget exceeded; select fewer tiles')
        vertices=[];faces=[];uvs=[];layer=mesh.uv_layers.active
        for triangle in mesh.loop_triangles:
            start=len(vertices)
            for vi,li in zip(triangle.vertices,triangle.loops):
                vertices.append(_world_point(obj.matrix_world,mesh.vertices[vi].co))
                if layer is not None:
                    uvs.append(tuple(layer.data[li].uv))
            faces.append((start,start+1,start+2))
        width=metadata['cells']*metadata['cell_size_m'];tx,ty=metadata['tile_x'],metadata['tile_y']
        plans.append(dict(vertices=vertices,faces=faces,uvs=uvs,tile=[tx,ty],
            bounds_m=[tx*width,ty*width,(tx+1)*width,(ty+1)*width]))
        identity=core._identifier(metadata.get('id'),'TerrainTileId')
        if identity in tile_ids:raise ValueError('Doppelte aktuelle TerrainTileId')
        tile_ids.append(identity)
    regions,waters,settings=workflow._inputs(scene)
    field=workflow._field(regions,waters,settings)
    expected=workflow.tile_range(settings['bounds_m'],settings['cells'],settings['cell_size_m']) if all_current else ()
    contacts=[]
    for obj in scene.objects:
        if obj.get('wz09_support_probe_id'):
            point=core._xyz(tuple(obj.matrix_world.translation),'Kontaktpunkt')
            if all_current or any(p['bounds_m'][0]<=point[0]<=p['bounds_m'][2]
                and p['bounds_m'][1]<=point[1]<=p['bounds_m'][3] for p in plans):
                contacts.append(dict(id=core._identifier(obj['wz09_support_probe_id']),position_m=list(point),
                    max_gap_m=core._number(obj.get('wz09_support_max_gap_m',.1),'Contact height tolerance',nonnegative=True)))
    result=extensions.diagnose_tiles(plans,field=field,expected_tile_indices=expected,
        support_points=contacts,
        max_vertices=s.diagnostic_vertex_budget,max_faces=s.diagnostic_vertex_budget,
        max_geometry_checks=s.diagnostic_check_budget,height_tolerance_m=s.diagnostic_height_tolerance_m,
        slope_limit_deg=s.diagnostic_slope_deg,max_issues=200)
    result['tile_ids']=tile_ids
    scene['wz09_terrain_diagnostics_json']=json.dumps(result,sort_keys=True,allow_nan=False)
    return result


def _rule_from_controls(settings,identity):
    return extensions.validate_material_rules([dict(id=identity,material=settings.material,
        strength=settings.strength,enabled=settings.enabled,
        height_range_m=[settings.height_min_m,settings.height_max_m] if settings.height_enabled else None,
        slope_range_deg=[settings.slope_min_deg,settings.slope_max_deg] if settings.slope_enabled else None,
        moisture_range=[settings.moisture_min,settings.moisture_max] if settings.moisture_enabled else None,
        height_falloff_m=settings.height_falloff_m,slope_falloff_deg=settings.slope_falloff_deg,
        moisture_falloff=settings.moisture_falloff)])[0]


def _load_rule(settings,record):
    settings.rule_id=record['id'];settings.material=record['material']
    for key in ('strength','enabled','height_falloff_m','slope_falloff_deg','moisture_falloff'):
        setattr(settings,key,record[key])
    for channel,key,low,high in (('height','height_range_m','height_min_m','height_max_m'),
        ('slope','slope_range_deg','slope_min_deg','slope_max_deg'),('moisture','moisture_range','moisture_min','moisture_max')):
        setattr(settings,channel+'_enabled',record[key] is not None)
        if record[key] is not None:
            setattr(settings,low,record[key][0]);setattr(settings,high,record[key][1])


def draw(layout,context):
    scene=context.scene;s=scene.wz09_terrain_authoring
    layout.label(text='Ground layers - six materials',icon='MATERIAL')
    row=layout.row(align=True);row.prop(s,'material',text='');row.prop(s,'enabled')
    row=layout.row(align=True);row.prop(s,'strength');row.prop(s,'falloff_m')
    box=layout.box();box.label(text='Editable outline',icon='CURVE_DATA')
    row=box.row(align=True);row.prop(s,'mask_shape',text='');row.prop(s,'mask_width_m');row.prop(s,'mask_depth_m')
    row=box.row(align=True)
    row.operator('wz09.terrain_mask_add',text='Am Cursor',icon='ADD')
    row.operator('wz09.terrain_mask_add',text='From selected faces',icon='MESH_DATA').from_selection=True
    row=box.row(align=True);row.operator('wz09.terrain_mask_read',text='Read mask',icon='EYEDROPPER')
    row.operator('wz09.terrain_mask_apply',text='Apply mask',icon='CHECKMARK')
    active=context.active_object
    if active is not None and active.get('wz09_material_mask_json'):
        box.prop(active,'["wz09_terrain_mask_order"]',text='Blend order (later overrides earlier)')
    box.label(text='G / S - Tab: edit vertices - X: delete mask',icon='EDITMODE_HLT')
    box=layout.box();box.label(text='Rules - later overrides earlier',icon='MOD_NODES')
    if not any((s.height_enabled,s.slope_enabled,s.moisture_enabled)):
        box.label(text='No filter: entire terrain surface',icon='INFO')
    for channel,label,fields in (('height','Height',('height_min_m','height_max_m','height_falloff_m')),
        ('slope','Neigung',('slope_min_deg','slope_max_deg','slope_falloff_deg')),
        ('moisture','Feuchte',('moisture_min','moisture_max','moisture_falloff'))):
        box.prop(s,channel+'_enabled',text=label)
        if getattr(s,channel+'_enabled'):
            row=box.row(align=True)
            for field in fields:row.prop(s,field)
    row=box.row(align=True);row.operator('wz09.terrain_rule_save',text='New rule',icon='ADD')
    row.operator('wz09.terrain_rule_save',text='Replace selected',icon='CHECKMARK').replace=True
    for index,rule in enumerate(extensions.validate_material_rules(_json(scene,'wz09_material_rules_json',[]))):
        row=box.row(align=True)
        op=row.operator('wz09.terrain_rule_load',text=f"{index+1} · {MATERIAL_LABELS[rule['material']]} · {rule['id'][:6]}",depress=s.rule_id==rule['id']);op.rule_id=rule['id']
        for direction,icon in ((-1,'TRIA_UP'),(1,'TRIA_DOWN')):
            op=row.operator('wz09.terrain_rule_move',text='',icon=icon);op.rule_id=rule['id'];op.direction=direction
        op=row.operator('wz09.terrain_rule_remove',text='',icon='X');op.rule_id=rule['id']
    box=layout.box();box.prop(s,'scatter_extended')
    if s.scatter_extended:
        for enabled,value in (('scatter_height_min_enabled','scatter_min_height_m'),('scatter_height_max_enabled','scatter_max_height_m'),('scatter_water_max_enabled','scatter_water_max_m')):
            row=box.row(align=True);row.prop(s,enabled)
            if getattr(s,enabled):row.prop(s,value)
        for key in ('scatter_water_min_m','scatter_material','scatter_material_weight','scatter_underwater',
            'scatter_nav_clearance_m','scatter_action_clearance_m','scatter_collision_clearance_m'):
            box.prop(s,key)
        op=box.operator('wz09.terrain_mask_add',text='Selection as exclusion area',icon='RESTRICT_SELECT_ON');op.from_selection=True;op.exclusion=True
        op=box.operator('wz09.terrain_mask_add',text='Exclusion area at cursor',icon='ADD');op.exclusion=True
        box.label(text='Nav/action IDs and distances are recorded during baking',icon='INFO')
    box=layout.box();box.label(text='Check geometry of selected tiles',icon='VIEWZOOM')
    row=box.row(align=True);row.prop(s,'diagnostic_slope_deg');row.prop(s,'diagnostic_height_tolerance_m')
    row=box.row(align=True);row.operator('wz09.terrain_diagnose',text='Check selection',icon='CHECKMARK')
    row.operator('wz09.terrain_diagnose',text='All current',icon='WORLD').all_current=True
    row=box.row(align=True);row.prop(s,'support_gap_m')
    row.operator('wz09.terrain_support_probe',text='Contact at cursor',icon='EMPTY_AXIS')
    active=context.active_object
    if active is not None and active.get('wz09_support_probe_id'):
        box.prop(active,'["wz09_support_max_gap_m"]',text='Contact height tolerance')
    box.prop(s,'diagnostic_vertex_budget');box.prop(s,'diagnostic_check_budget')
    result=_json(scene,'wz09_terrain_diagnostics_json',{})
    if result:
        box.label(text=f"{result.get('issue_count',0)} Hinweise · {result.get('checked_tiles',0)} Kacheln",icon='INFO')
        for kind,count in list(result.get('counts',{}).items())[:8]:
            box.label(text=f'{kind}: {count}',icon='ERROR')
        box.label(text='Geometry notes do not verify walkability',icon='INFO')


def register():
    import bpy
    from bpy.props import (BoolProperty,EnumProperty,FloatProperty,IntProperty,PointerProperty,StringProperty)
    materials=[(m,MATERIAL_LABELS[m],'Explizite Autorenlage') for m in extensions.MATERIALS]
    props=dict(material=EnumProperty(name='Material',items=materials,default='grass'),
        enabled=BoolProperty(name='Enabled',default=True),strength=FloatProperty(name='Strength',default=1,min=0,max=1),
        falloff_m=FloatProperty(name='Soft edge',default=5,min=0),
        mask_shape=EnumProperty(name='Shape',items=[('RECTANGLE','Rectangle',''),('CIRCLE','Circle / ellipse','')]),
        mask_width_m=FloatProperty(name='Width',default=40,min=.01),mask_depth_m=FloatProperty(name='Depth',default=20,min=.01),
        rule_id=StringProperty(name='Selected rule ID'),height_enabled=BoolProperty(name='Use height'),
        height_min_m=FloatProperty(name='Min.',default=0),height_max_m=FloatProperty(name='Max.',default=100),
        height_falloff_m=FloatProperty(name='Transition',default=10,min=0),slope_enabled=BoolProperty(name='Use slope'),
        slope_min_deg=FloatProperty(name='Min. °',default=0,min=0,max=90),slope_max_deg=FloatProperty(name='Max. °',default=90,min=0,max=90),
        slope_falloff_deg=FloatProperty(name='Transition angle',default=5,min=0,max=90),moisture_enabled=BoolProperty(name='Use moisture'),
        moisture_min=FloatProperty(name='Min.',default=0,min=0,max=1),moisture_max=FloatProperty(name='Max.',default=1,min=0,max=1),
        moisture_falloff=FloatProperty(name='Transition',default=.1,min=0,max=1),
        scatter_extended=BoolProperty(name='Advanced scatter filters',default=False),
        scatter_height_min_enabled=BoolProperty(name='Minimum height'),scatter_height_max_enabled=BoolProperty(name='Maximum height'),
        scatter_min_height_m=FloatProperty(name='Minimum height',default=0),scatter_max_height_m=FloatProperty(name='Maximum height',default=1000),
        scatter_water_min_m=FloatProperty(name='Minimum water distance',default=1,min=0),scatter_water_max_enabled=BoolProperty(name='Maximum water distance'),
        scatter_water_max_m=FloatProperty(name='Maximum water distance',default=100,min=0),
        scatter_material=EnumProperty(name='Ground filter',items=[('ALL','All materials','')]+materials),
        scatter_material_weight=FloatProperty(name='Minimum material weight',default=.5,min=0,max=1),scatter_underwater=BoolProperty(name='Allow underwater'),
        scatter_nav_clearance_m=FloatProperty(name='Distance to navigation nodes/links',default=2,min=0),
        scatter_action_clearance_m=FloatProperty(name='Extra clearance around action radii',default=3,min=0),
        scatter_collision_clearance_m=FloatProperty(name='Distance to explicit exclusion areas',default=1,min=0),
        diagnostic_vertex_budget=IntProperty(name='Maximum diagnostic vertices',default=300000,min=100,max=2000000),
        diagnostic_check_budget=IntProperty(name='Maximum pair checks',default=200000,min=100,max=2000000),
        diagnostic_height_tolerance_m=FloatProperty(name='Height tolerance',default=.01,min=.001),
        diagnostic_slope_deg=FloatProperty(name='Steeper than angle',default=45,min=0,max=90),
        support_gap_m=FloatProperty(name='Maximum contact distance',default=.1,min=0))
    Settings=type('WZ09_TerrainAuthoringSettings',(bpy.types.PropertyGroup,),{'__annotations__':props,'__module__':__name__})

    class WZ09_OT_terrain_mask_add(bpy.types.Operator):
        bl_idname='wz09.terrain_mask_add';bl_label='Create Editable Terrain Mask';bl_options={'REGISTER','UNDO'}
        from_selection:BoolProperty(default=False)
        exclusion:BoolProperty(default=False)
        def execute(self,context):
            try:
                create_mask(context,from_selection=self.from_selection,exclusion=self.exclusion);return {'FINISHED'}
            except (ValueError,RuntimeError,KeyError) as exc:
                self.report({'ERROR'},str(exc)[:220]);return {'CANCELLED'}

    class WZ09_OT_terrain_mask_read(bpy.types.Operator):
        bl_idname='wz09.terrain_mask_read';bl_label='Read Terrain Mask'
        def execute(self,context):
            try:
                obj=context.active_object
                if obj is None or not obj.get('wz09_material_mask_json'):raise ValueError('Select a material mask')
                record=_mask_record(obj);settings=context.scene.wz09_terrain_authoring
                for key in ('material','strength','falloff_m','enabled'):setattr(settings,key,record[key])
                return {'FINISHED'}
            except (ValueError,KeyError) as exc:
                self.report({'ERROR'},str(exc)[:220]);return {'CANCELLED'}

    class WZ09_OT_terrain_mask_apply(bpy.types.Operator):
        bl_idname='wz09.terrain_mask_apply';bl_label='Apply Terrain Mask';bl_options={'REGISTER','UNDO'}
        def execute(self,context):
            try:
                obj=context.active_object
                if obj is None or not obj.get('wz09_material_mask_json'):raise ValueError('Select a material mask')
                try:
                    from .composite_world import _require_local
                except ImportError:
                    from composite_world import _require_local
                _require_local(context.scene,obj)
                record=_mask_record(obj);settings=context.scene.wz09_terrain_authoring
                for key in ('material','strength','falloff_m','enabled'):record[key]=getattr(settings,key)
                record=extensions.validate_material_masks([record])[0]
                obj['wz09_material_mask_json']=json.dumps(record,sort_keys=True);return {'FINISHED'}
            except (ValueError,KeyError) as exc:
                self.report({'ERROR'},str(exc)[:220]);return {'CANCELLED'}

    class WZ09_OT_terrain_rule_save(bpy.types.Operator):
        bl_idname='wz09.terrain_rule_save';bl_label='Save Material Rule';bl_options={'REGISTER','UNDO'}
        replace:BoolProperty(default=False)
        def execute(self,context):
            try:
                s=context.scene.wz09_terrain_authoring
                identity=s.rule_id if self.replace else str(uuid.uuid4())
                if not identity:raise ValueError('Select a rule from the list first')
                record=upsert_rule(context.scene,_rule_from_controls(s,identity));s.rule_id=record['id']
                return {'FINISHED'}
            except (ValueError,KeyError) as exc:
                self.report({'ERROR'},str(exc)[:220]);return {'CANCELLED'}

    class WZ09_OT_terrain_rule_load(bpy.types.Operator):
        bl_idname='wz09.terrain_rule_load';bl_label='Read Material Rule'
        rule_id:StringProperty()
        def execute(self,context):
            try:
                records=extensions.validate_material_rules(_json(context.scene,'wz09_material_rules_json',[]))
                record=next(r for r in records if r['id']==self.rule_id)
                _load_rule(context.scene.wz09_terrain_authoring,record);return {'FINISHED'}
            except (ValueError,KeyError,StopIteration) as exc:
                self.report({'ERROR'},str(exc)[:220]);return {'CANCELLED'}

    class WZ09_OT_terrain_rule_remove(bpy.types.Operator):
        bl_idname='wz09.terrain_rule_remove';bl_label='Remove Material Rule';bl_options={'REGISTER','UNDO'}
        rule_id:StringProperty()
        def execute(self,context):
            try:
                remove_rule(context.scene,self.rule_id);return {'FINISHED'}
            except ValueError as exc:
                self.report({'ERROR'},str(exc)[:220]);return {'CANCELLED'}

    class WZ09_OT_terrain_rule_move(bpy.types.Operator):
        bl_idname='wz09.terrain_rule_move';bl_label='Reorder Material Rule';bl_options={'REGISTER','UNDO'}
        rule_id:StringProperty()
        direction:IntProperty(default=1,min=-1,max=1)
        def execute(self,context):
            try:move_rule(context.scene,self.rule_id,self.direction);return {'FINISHED'}
            except ValueError as exc:
                self.report({'ERROR'},str(exc)[:220]);return {'CANCELLED'}

    class WZ09_OT_terrain_diagnose(bpy.types.Operator):
        bl_idname='wz09.terrain_diagnose';bl_label='Inspect Generated Terrain'
        all_current:BoolProperty(default=False)
        def execute(self,context):
            try:
                result=diagnose_scene(context,all_current=self.all_current)
                self.report({'WARNING'} if result['issue_count'] else {'INFO'},str(result['issue_count'])+' geometry notes; runtime unverified')
                return {'FINISHED'}
            except (ValueError,RuntimeError,KeyError,TypeError) as exc:
                self.report({'ERROR'},str(exc)[:220]);return {'CANCELLED'}

    class WZ09_OT_terrain_support_probe(bpy.types.Operator):
        bl_idname='wz09.terrain_support_probe';bl_label='Create Explicit Terrain Contact';bl_options={'REGISTER','UNDO'}
        def execute(self,context):
            try:create_support_probe(context);return {'FINISHED'}
            except (ValueError,RuntimeError) as exc:
                self.report({'ERROR'},str(exc)[:220]);return {'CANCELLED'}

    class WZ09_PT_terrain_authoring(bpy.types.Panel):
        bl_label='Terrain - materials and spacing';bl_idname='WZ09_PT_terrain_authoring';bl_parent_id='WZ09_PT_main'
        bl_space_type='VIEW_3D';bl_region_type='UI';bl_category='WZ2009';bl_options={'DEFAULT_CLOSED'}
        @classmethod
        def poll(cls,context):return getattr(context.scene,'wz09_workspace','')=='WORLD'
        def draw(self,context):
            try:draw(self.layout,context)
            except (ValueError,KeyError) as exc:self.layout.label(text=str(exc)[:100],icon='ERROR')

    _CLASSES[:]=[Settings,WZ09_OT_terrain_mask_add,WZ09_OT_terrain_mask_read,WZ09_OT_terrain_mask_apply,
        WZ09_OT_terrain_rule_save,WZ09_OT_terrain_rule_load,WZ09_OT_terrain_rule_remove,WZ09_OT_terrain_rule_move,WZ09_OT_terrain_diagnose,
        WZ09_OT_terrain_support_probe,WZ09_PT_terrain_authoring]
    for cls in _CLASSES:bpy.utils.register_class(cls)
    bpy.types.Scene.wz09_terrain_authoring=PointerProperty(type=Settings)


def unregister():
    import bpy
    if hasattr(bpy.types.Scene,'wz09_terrain_authoring'):del bpy.types.Scene.wz09_terrain_authoring
    for cls in reversed(_CLASSES):bpy.utils.unregister_class(cls)
    _CLASSES.clear()
