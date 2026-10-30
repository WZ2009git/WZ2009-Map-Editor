"""Strict cross-reference checks for optional editor composition records."""
import importlib
import importlib.util
import math
import sys
import hashlib
import json
from pathlib import Path


def _module(name):
    if __package__:
        return importlib.import_module('.'+name,__package__)
    spec=importlib.util.spec_from_file_location('_wz09_composition_'+name,Path(__file__).with_name(name+'.py'))
    module=importlib.util.module_from_spec(spec)
    parent=str(Path(__file__).parent);sys.path.insert(0,parent)
    try:spec.loader.exec_module(module)
    finally:sys.path.remove(parent)
    return module


def _id(record, ids, where):
    value=record.get('id')
    if not isinstance(value,str) or not value.strip() or value in ids:
        raise ValueError(where+': missing or duplicate global ID')
    ids.add(value)
    return value


def validate_composition(data, map_ids, asset_ids=(), placement_ids=(), all_ids=None,*,world=None):
    if not isinstance(data,dict) or data.get('version')!=1:
        raise ValueError('composition: version 1 required')
    if data.get('runtime_integrated') is not False or data.get('physical_scale_verified') is not False:
        raise ValueError('composition: runtime and physical scale must remain unverified')
    ids=set() if all_ids is None else all_ids
    core=_module('terrain_core')
    authoring=_module('terrain_authoring')
    regions=data.get('regions',[]);region_ids=set();confirmed=[]
    for record in regions:
        rid=_id(record,ids,'composition.regions');region_ids.add(rid)
        if record['map_instance_id'] not in map_ids:raise ValueError('composition.regions: unknown map owner')
        core.validate_polygon(record['polygon'])
        if record['confirmed']:confirmed.append(record)
        if not math.isfinite(record['height_m']) or record['transition_m']<=0:
            raise ValueError('composition.regions: invalid height/transition')
    if confirmed:core.validate_regions(confirmed)
    waters=data.get('water_features',[]);water_ids=set();seas=[]
    for water in waters:
        wid=_id(water,ids,'composition.water_features');water_ids.add(wid)
        if water['kind']=='river':core.validate_river(dict(water,points=water['points_m']))
        else:
            x,y,z=water['position_m'];w,h=water['extent_m']
            core.sea_mesh([x-w/2,y-h/2,x+w/2,y+h/2],z)
            seas.append(core.validate_sea({'id':water['id'],'bounds':[x-w/2,y-h/2,x+w/2,y+h/2],
                'level_m':water['level_m'],'depth_m':water['depth_m'],
                'coast_width_m':water.get('coast_width_m',30.),'carve_terrain':water.get('carve_terrain',False)}))
            if abs(z-water['level_m'])>1e-6:raise ValueError('composition.water_features: sea position and level disagree')
    # Match TerrainField's positive-area sea overlap rule, including manifests
    # with only draft cores where constructing a terrain field is inappropriate.
    for i,a in enumerate(seas):
        for b in seas[i+1:]:
            ab,bb=a['bounds'],b['bounds']
            if (min(ab[2],bb[2])-max(ab[0],bb[0])>1e-8
                    and min(ab[3],bb[3])-max(ab[1],bb[1])>1e-8
                    and abs(a['level_m']-b['level_m'])>1e-8):
                raise ValueError('composition.water_features: overlapping seas have conflicting water levels')
    layers=authoring.validate_material_settings(data.get('material_layers'),all_ids=ids)
    if 'material_layers' in data and layers is None:
        raise ValueError('composition.material_layers: omit the field for legacy material settings')
    if layers is not None:
        for category in ('rules','masks'):
            for record in layers[category]:_id(record,ids,'composition.material_layers.'+category)
    locations=set()
    terrain_inputs=set()
    checked_authoring=set();reserved_authoring={}
    for tile in data.get('terrain_tiles',[]):
        _id(tile,ids,'composition.terrain_tiles')
        loc=(tile['tile_x'],tile['tile_y'])
        if loc in locations:raise ValueError('composition.terrain_tiles: overlapping active tile coordinates')
        locations.add(loc)
        expected=hashlib.sha256(json.dumps([regions,waters,tile['settings']],sort_keys=True,
                    separators=(',',':'),allow_nan=False).encode()).hexdigest()
        if tile['input_sha256']!=expected:raise ValueError('composition.terrain_tiles: stale or forged input fingerprint')
        if tile['cells']!=tile['settings']['cells'] or tile['cell_size_m']!=tile['settings']['cell_size_m']:
            raise ValueError('composition.terrain_tiles: grid disagrees with generation settings')
        if tile['texture_images']!=tile['settings']['texture_images']:
            raise ValueError('composition.terrain_tiles: texture profiles disagree with generation settings')
        settings=tile['settings'];plan=settings.get('landscape_plan')
        channels=set(('sand','soil','grass','snow','rock','asphalt') if plan else
                     core.BIOMES if layers is None else layers['channels'])
        if set(tile['texture_images'])!=channels:
            raise ValueError('composition.terrain_tiles: texture channels disagree with material layers')
        if (tile['settings'].get('material_layers')!=layers
                or layers is None and 'material_layers' in tile['settings']):
            raise ValueError('composition.terrain_tiles: material layers disagree with composition; regenerate terrain')
        # Immutable authoring snapshots can occur on every tile. Validate and
        # reserve their persistent IDs once; repeated copies must be identical.
        if expected not in checked_authoring:
            checked_authoring.add(expected)
            records=[]
            if plan is not None:
                _module('landscape_core').validate_plan(plan)
                if {row['id'] for row in plan['maps']}!=set(map_ids):
                    raise ValueError('composition.terrain_tiles: landscape map identities differ')
                if world is not None:
                    poses={row['id']:row for row in world['maps']}
                    for row in plan['maps']:
                        actual=poses[row['id']]
                        if (row['source_map']!=actual['source_map'] or
                                any(abs(a-b)>.002 for a,b in zip(row['position_m'],actual['position_m'])) or
                                any(abs(a-b)>.002 for a,b in zip(row['rotation_deg'],actual['rotation_deg']))):
                            raise ValueError('composition.terrain_tiles: landscape pose differs from map; regenerate terrain')
                records.extend(plan['relief']['features'])
            grid=settings.get('heightfield_authoring')
            if grid is not None:
                heightfield=_module('heightfield');heightfield.validate_grid(grid)
                if grid['source_sha256']!=heightfield.fingerprint(regions,waters,settings):
                    raise ValueError('composition.terrain_tiles: heightmap source fingerprint differs; regenerate terrain')
                records.append(grid)
            if settings.get('material_bake') not in (None,'sampled_weight_grid_v1'):
                raise ValueError('composition.terrain_tiles: unsupported material bake')
            for record in records:
                digest=hashlib.sha256(json.dumps(record,sort_keys=True,separators=(',',':'),allow_nan=False).encode()).hexdigest()
                if record['id'] not in reserved_authoring:
                    _id(record,ids,'composition.terrain_tiles.authoring')
                    reserved_authoring[record['id']]=digest
                elif reserved_authoring[record['id']]!=digest:
                    raise ValueError('composition.terrain_tiles: conflicting authoring snapshots share one ID')
        terrain_inputs.add(expected)
        if not set(tile['region_ids']).issubset({r['id'] for r in confirmed}):
            raise ValueError('composition.terrain_tiles: missing or unconfirmed protected core')
        if not set(tile['water_ids']).issubset(water_ids):raise ValueError('composition.terrain_tiles: missing water reference')
        if tile['runtime_integrated'] is not False or tile['collision_verified'] is not False:
            raise ValueError('composition.terrain_tiles: runtime/collision unverified')
    claimed=set();declared_exclusions={}
    for scatter in data.get('scatter_sets',[]):
        _id(scatter,ids,'composition.scatter_sets')
        if scatter['map_instance_id'] not in map_ids:raise ValueError('composition.scatter_sets: missing map')
        if scatter['asset_id'] not in asset_ids:raise ValueError('composition.scatter_sets: missing source asset')
        if scatter['input_sha256'] not in terrain_inputs:raise ValueError('composition.scatter_sets: no matching active terrain generation')
        if ('authoring_filters' in scatter)!=('authoring_filters_sha256' in scatter):
            raise ValueError('composition.scatter_sets: both authoring filter fields are required')
        if 'authoring_filters' in scatter:
            captured=authoring.validate_scatter_settings(scatter['authoring_filters'],world=world)
            if authoring.settings_sha256(captured)!=scatter['authoring_filters_sha256']:
                raise ValueError('composition.scatter_sets: stale or forged authoring filter fingerprint')
            # Nav/actions are references to already existing identities. An
            # explicit exclusion is authored here and must reserve its own ID
            # once, even when several sets share the same immutable snapshot.
            for exclusion in (captured or {}).get('exclusions',[]):
                if exclusion['id'] not in declared_exclusions:
                    _id(exclusion,ids,'composition.scatter_sets.explicit_exclusion')
                    declared_exclusions[exclusion['id']]=exclusion['polygon']
                elif declared_exclusions[exclusion['id']]!=exclusion['polygon']:
                    raise ValueError('composition.scatter_sets: one exclusion ID has conflicting polygons')
        for pid in scatter['placement_ids']:
            if pid not in placement_ids or pid in claimed:raise ValueError('composition.scatter_sets: missing or duplicate placement')
            claimed.add(pid)
    return data


def validate_extensions(world, ids=None):
    """Schema supplies field types; helpers enforce semantic/reference invariants."""
    used=set() if ids is None else ids
    if ids is None:
        for key in ('maps','markers','asset_placements','geometry'):
            for record in world.get(key,[]):_id(record,used,key)
        for key in ('nodes','edges'):
            for record in world.get('nav',{}).get(key,[]):_id(record,used,'nav.'+key)
    maps={m['id'] for m in world.get('maps',[])}
    assets=world.get('asset_placements',[])
    checked_native=set()
    if 'composition' in world:
        validate_composition(world['composition'],maps,{a['asset_id'] for a in assets},
                             {a['id'] for a in assets},used,world=world)
    for key,module_name in (('audio_zones','audio_zones'),('action_elements','action_elements')):
        if key not in world:continue
        module=_module(module_name)
        module.validate_records(world[key],map_ids=maps,all_ids=used,verify_sources=True)
        for record in world[key]:
            evidence=record.get('evidence',{})
            source=evidence.get('source_path','')
            if evidence.get('status')=='source_bound' and source not in checked_native:
                checked_native.add(source)
                path=Path(source)
                if path.suffix.lower()=='.json' and path.stat().st_size<=64*1024*1024:
                    try:payload=json.loads(path.read_text(encoding='utf-8'))
                    except (ValueError,UnicodeError):payload=None
                    if isinstance(payload,dict) and payload.get('kind')=='wz09_native_semantics':
                        _module('native_semantics').read_semantics(path,verify_sources=True)
            # Helper APIs may validate against a copied set; keep global identity here.
            if record['id'] not in used:used.add(record['id'])
            if key=='audio_zones' and record.get('asset_placement_id') is not None:
                asset=next((a for a in assets if a['id']==record['asset_placement_id']),None)
                if asset is None or asset['owner_map']!=record['map_instance_id']:
                    raise ValueError('audio_zones: missing visual asset or conflicting map owner')
            if key=='action_elements':
                marker=record.get('marker_id');placement=record.get('asset_placement_id')
                marker_lookup={m['id']:m for m in world.get('markers',[])}
                placement_lookup={a['id']:a for a in assets}
                if marker is not None:
                    if marker not in marker_lookup:raise ValueError('action_elements: missing marker')
                    if marker_lookup[marker]['properties'].get('editor_owner_map')!=record['map_instance_id']:
                        raise ValueError('action_elements: marker and action map disagree')
                    if record['kind'] in {'loot_crate','buy_station'} and marker_lookup[marker]['kind']!=record['kind']:
                        raise ValueError('action_elements: marker kind and behavior disagree')
                if placement is not None:
                    if placement not in placement_lookup:raise ValueError('action_elements: missing asset placement')
                    if placement_lookup[placement]['owner_map']!=record['map_instance_id']:
                        raise ValueError('action_elements: placement and action map disagree')
                    if marker is not None and (placement_lookup[placement].get('marker_id')!=marker
                            or marker_lookup[marker]['properties'].get('asset_placement_id')!=placement):
                        raise ValueError('action_elements: marker and placement bindings disagree')
    return used
