"""Blender scene serialization. Design-only: does not import native IW4 maps."""
import json, math
import os
from pathlib import Path
import tempfile
import uuid
try:
    from .calibration import validate_calibration, verify_calibration_source, matching_calibration_transform, has_physical_scale
    from .source_bundle import verify_preview_bundle
    from .authoring_helpers import aircraft_routes
    from .obj_bundle import verify_obj_bundle
except ImportError:  # Direct offline module loading; no bpy required.
    from calibration import validate_calibration, verify_calibration_source, matching_calibration_transform, has_physical_scale
    from source_bundle import verify_preview_bundle
    from authoring_helpers import aircraft_routes
    from obj_bundle import verify_obj_bundle

WORLD_ID='wz09_karvorsk'
MAPS={'mp_afghan','mp_derail','mp_estate','mp_favela','mp_highrise','mp_invasion',
      'mp_checkpoint','mp_quarry','mp_rundown','mp_rust','mp_boneyard',
      'mp_nightshift','mp_subbase','mp_terminal','mp_underpass','mp_brecourt'}
MARKERS={'loot_crate','buy_station','player_spawn','loadout_drop','mission','poi','aircraft_keyframe','nav_node'}

def v3(v):
    a=[float(v[i]) for i in range(3)]
    if any(not math.isfinite(c) for c in a): raise ValueError('Nonfinite coordinate')
    return [round(c,7) for c in a]

def idstr(obj):
    value=obj.get('wz09_id','')
    return value if isinstance(value,str) else ''

def euler_deg(obj):
    return v3([math.degrees(x) for x in obj.matrix_world.to_euler('XYZ')])

def _json_property(owner, key, expected):
    raw=owner.get(key,'')
    if raw in ('',None): return None
    try:
        data=json.loads(str(raw),parse_constant=lambda value: (_ for _ in ()).throw(ValueError(f'Nonfinite JSON value: {value}')))
    except (TypeError,ValueError) as exc:
        raise ValueError(f'{key}: invalid JSON: {exc}') from exc
    if not isinstance(data,expected):
        raise ValueError(f'{key}: expected {expected.__name__}')
    return data

def _map_transform(obj):
    matrix=obj.matrix_world.to_3x3()
    columns=[[float(matrix[row][column]) for row in range(3)] for column in range(3)]
    if any(not math.isfinite(v) for col in columns for v in col):
        raise ValueError(f'Map {idstr(obj)}: nonfinite transformation')
    for i,col in enumerate(columns):
        if not math.isclose(sum(v*v for v in col),1.0,rel_tol=1e-6,abs_tol=1e-6):
            raise ValueError(f'Map {idstr(obj)}: apply unit scale; map scaling is not supported')
        for other in columns[i+1:]:
            if abs(sum(a*b for a,b in zip(col,other)))>1e-6:
                raise ValueError(f'Map {idstr(obj)}: shear is not supported')
    a,b,c=columns
    determinant=a[0]*(b[1]*c[2]-b[2]*c[1])-b[0]*(a[1]*c[2]-a[2]*c[1])+c[0]*(a[1]*b[2]-a[2]*b[1])
    if determinant<=0:
        raise ValueError(f'Map {idstr(obj)}: reflected transformations are not supported')

def _parent_map(obj):
    current=getattr(obj,'parent',None);visited=set()
    while current is not None:
        if id(current) in visited: raise ValueError(f'Parent cycle at {idstr(obj)}')
        visited.add(id(current))
        if current.get('wz09_type','')=='map_instance': return idstr(current)
        current=getattr(current,'parent',None)
    return None

def _map_provenance(obj,calibration):
    provenance=_json_property(obj,'wz09_map_provenance_json',dict)
    if provenance is not None and 'preview_bundle' in provenance:
        verify_preview_bundle(provenance['preview_bundle'])
    if provenance is not None and 'obj_preview_bundle' in provenance:
        verify_obj_bundle(provenance['obj_preview_bundle'])
    records=_json_property(obj,'wz09_calibrations_json',list)
    embedded=provenance.get('calibration_sources') if provenance is not None else None
    if embedded is not None and not isinstance(embedded,list):
        raise ValueError(f'Map {idstr(obj)}: provenance.calibration_sources must be a list')
    if records is None and embedded is None:return provenance
    normalized={}
    for record in (records or [])+(embedded or []):
        if calibration is None:
            raise ValueError(f'Map {idstr(obj)}: additional calibration sources require a principal calibration')
        evidence=validate_calibration(record)
        verify_calibration_source(evidence)
        if not matching_calibration_transform(calibration,evidence):
            raise ValueError(f'Map {idstr(obj)}: calibration sources define different map transforms')
        key=(evidence['source_path'],evidence['source_sha256'])
        previous=normalized.get(key)
        if previous is not None and previous!=evidence:
            raise ValueError(f'Map {idstr(obj)}: conflicting calibration evidence for {evidence["source_path"]}')
        normalized[key]=evidence
    provenance=dict(provenance or {})
    provenance['calibration_sources']=[normalized[key] for key in sorted(normalized)]
    return provenance

def _edge_records(scene, nodes, all_ids):
    records=_json_property(scene,'wz09_edges_json',list) or []
    edges=[];pairs=set();ids=set(all_ids)
    for record in records:
        if not isinstance(record,dict): raise ValueError('wz09_edges_json: every edge must be an object')
        if not {'id','from','to','kind','status'}<=record.keys():
            raise ValueError('wz09_edges_json: every edge requires id/from/to/kind/status')
        if any(not isinstance(record[key],str) or not record[key].strip()
               for key in ('id','from','to','kind','status')):
            raise ValueError('wz09_edges_json: id/from/to/kind/status must be nonempty text')
        uid=record['id'];src=record['from'];dst=record['to']
        if not isinstance(uid,str) or not uid.strip() or uid in ids:
            raise ValueError(f'Missing or duplicate persistent edge id: {uid}')
        if src not in nodes or dst not in nodes:
            raise ValueError(f'Nav link {src} -> {dst}: endpoints must be nav nodes')
        if src==dst: raise ValueError(f'Nav self link at {src}')
        if (src,dst) in pairs: raise ValueError(f'Duplicate directed nav link: {src} -> {dst}')
        if record['status']!='proposed':
            raise ValueError(f'Nav link {uid}: editor-authored links must remain proposed')
        if record['kind'] not in {'walk','drop','break_glass','climb','jump','mantle'}:
            raise ValueError(f'Nav link {uid}: unsupported edge kind {record["kind"]}')
        if set(record)-{'id','from','to','kind','status','evidence','enabled','condition'}:
            raise ValueError(f'Nav link {uid}: unknown edge fields')
        if 'enabled' in record and type(record['enabled']) is not bool:
            raise ValueError(f'Nav link {uid}: enabled must be boolean')
        if 'condition' in record and (not isinstance(record['condition'],str) or len(record['condition'])>512):
            raise ValueError(f'Nav link {uid}: condition must be text of at most 512 characters')
        if 'evidence' in record and not isinstance(record['evidence'],dict):
            raise ValueError(f'Nav link {uid}: evidence must be an object')
        edges.append(dict(record));ids.add(uid);pairs.add((src,dst))
    return edges

def migrate_edges(scene):
    """Explicit one-time conversion of old per-node links to stored edge UUIDs.

    The caller runs this within a Blender undoable operation. Export itself never
    invents IDs or mutates the authoring scene.
    """
    objects=[o for o in scene.objects if o.get('wz09_type','')]
    all_ids=[idstr(o) for o in objects]
    if any(not uid.strip() for uid in all_ids) or len(set(all_ids))!=len(all_ids):
        raise ValueError('Cannot migrate links with missing or duplicate object IDs')
    nodes={idstr(o) for o in objects if o.get('wz09_type')=='nav_node'}
    records=_edge_records(scene,nodes,all_ids)
    pairs={(r['from'],r['to']) for r in records}
    for obj in objects:
        if obj.get('wz09_type')!='nav_node': continue
        for dst in [x.strip() for x in str(obj.get('wz09_links','')).split(',') if x.strip()]:
            src=idstr(obj)
            if dst not in nodes or dst==src:
                raise ValueError(f'Nav link {src} -> {dst}: migration requires distinct nav-node endpoints')
            if (src,dst) not in pairs:
                records.append({'id':str(uuid.uuid4()),'from':src,'to':dst,
                    'kind':str(obj.get('wz09_edge_kind','walk')),'status':'proposed'})
                pairs.add((src,dst))
    # Validate all records before modifying either the scene or legacy properties.
    holder={'wz09_edges_json':json.dumps(records,allow_nan=False)}
    _edge_records(holder,nodes,all_ids)
    scene['wz09_edges_json']=json.dumps(records,ensure_ascii=False,sort_keys=True)
    for obj in objects:
        if obj.get('wz09_type')=='nav_node' and obj.get('wz09_links',''):
            obj['wz09_links']=''
    return records

def export_blender_scene(scene):
    # On file reload Blender evaluates only the visible scene immediately.
    # Refresh the requested scene before reading parented world transforms;
    # otherwise an unchanged inactive terrain can appear spuriously stale.
    for layer in getattr(scene,'view_layers',()):layer.update()
    maps=[]; markers=[]; nodes=[]; edges=[]; IDs=set();nav_objects=[]
    for o in sorted(scene.objects,key=lambda x:x.name):
        typ=o.get('wz09_type','')
        if not typ: continue
        uid=idstr(o)
        if not uid.strip() or uid in IDs: raise ValueError(f'Missing or duplicate wz09_id: {o.name}')
        IDs.add(uid)
        pos=v3(o.matrix_world.translation)
        if typ=='map_instance':
            _map_transform(o)
            source=str(o.get('wz09_source_map',''))
            if source not in MAPS:raise ValueError(f'Unsupported original map: {source}')
            scale=float(o.get('wz09_source_scale',0.0))
            if not math.isfinite(scale) or scale<0:raise ValueError(f'Map {uid}: invalid source scale')
            calibration=_json_property(o,'wz09_calibration_json',dict)
            if scale>0 and calibration is None:
                raise ValueError(f'Map {uid}: positive source scale requires a measured calibration file')
            entry={'id':uid,'source_map':source,'position_m':pos,'rotation_deg':euler_deg(o),
                'source_units_to_meter':None,'calibration_status':'unverified'}
            if calibration is not None:
                calibration=validate_calibration(calibration)
                verify_calibration_source(calibration)
                if scale>0 and not math.isclose(scale,calibration['source_units_to_meter'],rel_tol=1e-8,abs_tol=1e-12):
                    raise ValueError(f'Map {uid}: source scale differs from its measured calibration')
                entry['calibration']=calibration
                if has_physical_scale(calibration):
                    entry['source_units_to_meter']=calibration['source_units_to_meter']
                    entry['calibration_status']='calibrated'
            provenance=_map_provenance(o,calibration)
            if provenance is not None:entry['provenance']=provenance
            maps.append(entry)
        elif typ=='nav_node':
            owner=str(o.get('wz09_owner_map','')) or None
            entry={'id':uid,'owner_map':owner,'kind':str(o.get('wz09_nav_kind','walk')),
                   'position_m':pos,'source':str(o.get('wz09_nav_source','manual'))}
            properties=_json_property(o,'wz09_nav_properties_json',dict)
            if properties is not None:entry['properties']=properties
            provenance=_json_property(o,'wz09_nav_provenance_json',dict)
            if provenance is not None:entry['provenance']=provenance
            nodes.append(entry)
            nav_objects.append(o)
        elif typ in MARKERS:
            properties=_json_property(o,'wz09_properties_json',dict) or {}
            properties['profile']=str(o.get('wz09_profile','default'))
            if typ=='loot_crate':
                properties['loot_table']=str(o.get('wz09_loot_table','standard'))
            elif typ=='buy_station':
                properties['shop_catalog']=str(o.get('wz09_shop_catalog','standard'))
            elif typ=='poi':
                properties['name']=str(o.get('wz09_poi_name',o.name))
            elif typ=='aircraft_keyframe' and o.get('wz09_route_id'):
                properties.update(route_id=str(o['wz09_route_id']),sequence=o.get('wz09_route_sequence'),time_s=o.get('wz09_route_time_s'))
            owner=_parent_map(o)
            if owner is not None:properties['editor_owner_map']=owner
            markers.append({'id':uid,'kind':typ,'position_m':pos,'rotation_deg':euler_deg(o),
                'properties':properties})
        else: raise ValueError(f'Unsupported WZ09 object type: {typ}')
    map_ids={m['id'] for m in maps};node_ids={n['id'] for n in nodes}
    for o in nav_objects:
        owner=str(o.get('wz09_owner_map','')) or None
        if owner is not None and owner not in map_ids:
            raise ValueError(f'Nav node {idstr(o)}: owner_map must reference a map instance')
        if _parent_map(o)!=owner:
            raise ValueError(f'Nav node {idstr(o)}: owner_map and map parent are inconsistent')
        refs=str(o.get('wz09_links','')).strip()
        if refs:raise ValueError(f'Nav node {idstr(o)}: migrate legacy links before export')
    assets=[]
    if any(o.get('wz09_asset_placement') for o in scene.objects):
        try:from .asset_browser import serialize_placements
        except ImportError:from asset_browser import serialize_placements
        assets=serialize_placements(scene)
        for record in assets:
            if record['id'] in IDs:raise ValueError('Duplicate global asset placement ID: '+record['id'])
            IDs.add(record['id'])
    edges=_edge_records(scene,node_ids,IDs)
    aircraft_routes(markers)
    document={'schema_version':1,'world_id':WORLD_ID,'units':'meters',
        'maps':sorted(maps,key=lambda x:x['id']),
        'markers':sorted(markers,key=lambda x:x['id']),
        'nav':{'nodes':sorted(nodes,key=lambda x:x['id']),
               'edges':sorted(edges,key=lambda x:x['id'])},
        'geometry':[],
        'tactical_map':{'status':'not_generated'}}
    if assets:document['asset_placements']=assets
    extension_ids=set(IDs)
    if any(o.get('wz09_audio_zone') or o.get('wz09_action_element') for o in scene.objects):
        try:from . import audio_zones,action_elements
        except ImportError:import audio_zones,action_elements
        for key,module in (('audio_zones',audio_zones),('action_elements',action_elements)):
            records=module.serialize_scene(scene)
            if records:document[key]=records
    if (any(o.get('wz09_region_kind') or o.get('wz09_water_json') or o.get('wz09_terrain_tile_json')
            for o in scene.objects) or scene.get('wz09_scatter_sets_json')):
        from .terrain_workflow import serialize_scene as serialize_composition
        composition=serialize_composition(scene)
        if composition:document['composition']=composition
    if any(k in document for k in ('composition','audio_zones','action_elements')):
        try:from .composition_data import validate_extensions
        except ImportError:from composition_data import validate_extensions
        validate_extensions(document,extension_ids)
    return document

def safe_output_path(path,root=None):
    """Resolve an output inside the explicit workspace root, following symlinks."""
    if root is None:
        root=Path(__file__).resolve().parents[2]
        if not (root/'AGENTS.md').is_file():
            raise ValueError('Set an explicit editor output root for the installed add-on')
    root=Path(root).expanduser().resolve()
    path=Path(path).expanduser()
    path=(root/path).resolve() if not path.is_absolute() else path.resolve()
    try:relative=path.relative_to(root)
    except ValueError as exc:raise ValueError(f'Output must stay inside editor root: {root}') from exc
    if not relative.parts:raise ValueError('Output must be a file inside the editor root')
    return path

def atomic_write(path,doc,root=None):
    """Publish new JSON atomically; repeated identical exports never replace files."""
    path=safe_output_path(path,root)
    content=(json.dumps(doc,ensure_ascii=False,indent=2,sort_keys=True,allow_nan=False)+'\n').encode('utf-8')
    if path.exists():
        if path.read_bytes()==content:return path
        raise FileExistsError(f'Refusing to overwrite a different existing export: {path}; choose a new filename')
    path.parent.mkdir(parents=True,exist_ok=True)
    if safe_output_path(path,root)!=path:raise ValueError('Output parent changed during export')
    fd,tmp=tempfile.mkstemp(prefix='.wz09-',suffix='.tmp',dir=path.parent)
    try:
        with os.fdopen(fd,'wb') as stream:
            stream.write(content);stream.flush();os.fsync(stream.fileno())
        try:os.link(tmp,path)  # Exclusive atomic publication; never replaces a raced-in target.
        except FileExistsError:
            if path.read_bytes()!=content:
                raise FileExistsError(f'Refusing to overwrite a different existing export: {path}; choose a new filename')
    finally:
        Path(tmp).unlink(missing_ok=True)
    return path
