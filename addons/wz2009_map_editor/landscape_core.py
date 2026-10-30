"""KRV-26 authored landscape plan, without Blender or game dependencies.

All distances are explicit editor design units. Relief is an additive height
signal; the existing TerrainField host protects real core boundaries and applies
its water carving afterwards. No native navigation, collision or metre scale is
inferred here. Original map records are never mutated.
"""
from __future__ import annotations

import copy
import hashlib
import json
import math
import uuid

try:
    from .layout_geometry import REGIONS
    from .map_library import MAP_CODES, MAP_NAMES
except ImportError:  # Existing direct-module offline test convention.
    from layout_geometry import REGIONS
    from map_library import MAP_CODES, MAP_NAMES

NAMES = {code: name.upper() for code, name in MAP_NAMES}


def canonical_hash(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'),
                                    allow_nan=False).encode()).hexdigest()


def stable_id(role, source=''):
    return str(uuid.uuid5(uuid.NAMESPACE_URL, 'wz09/karvorsk-intro-v1/'+role+'/'+source))

KIND = 'wz09_karvorsk_landscape_plan'
VERSION = 1
CENTRAL = frozenset(('mp_highrise', 'mp_nightshift', 'mp_invasion', 'mp_checkpoint'))
CHANNELS = ('sand', 'soil', 'grass', 'snow', 'rock', 'asphalt')
RUNWAY_MATERIALS = frozenset(('wc/asphalt_grooved', 'wc/asphalt_traffic_wear_stripe_dec',
                              'wc/kh_tarmac_trim1'))
HIGHRISE_FACADE_MATERIALS = frozenset(('wc/city_building_exterior02',
                                      'wc/city_window02', 'wc/city_window03'))
HIGHRISE_CLOUD_MATERIALS = frozenset('wc/atmos_cloud_below_highrise01'+suffix
                                   for suffix in ('', 'high', 'medium', 'low', 'lowest'))


def _number(value, field):
    if type(value) not in (int, float) or not math.isfinite(value) or abs(value) > 1e7:
        raise ValueError(field+': finite bounded number required')
    return float(value)


def _xyz(value, field):
    if not isinstance(value, (list, tuple)) or len(value) != 3:
        raise ValueError(field+': three coordinates required')
    return [_number(v, field) for v in value]


def smooth(value):
    value = max(0., min(1., value))
    return value*value*(3.-2.*value)


def _segment_distance(x, y, a, b):
    dx, dy = b[0]-a[0], b[1]-a[1]
    t = max(0., min(1., ((x-a[0])*dx+(y-a[1])*dy)/(dx*dx+dy*dy)))
    return math.hypot(x-a[0]-t*dx, y-a[1]-t*dy)


def make_plan(world, *, inward_design_units=450.):
    """Translate only outer groups from their accepted current coordinates.

    This deliberately does not call the old grid layout builder. Central map
    matrices and each group's relative arrangement are therefore independent of
    future layout defaults, record order, or native source topology.
    """
    inward = _number(inward_design_units, 'inward_design_units')
    if not 0 <= inward <= 500:
        raise ValueError('Outer group inward move must be within 0..500 design units')
    records = world.get('maps')
    if (not isinstance(records, list) or len(records) != 16
            or {r.get('source_map') for r in records} != MAP_CODES
            or len({r.get('id') for r in records}) != 16):
        raise ValueError('Exactly the existing sixteen maps with persistent IDs are required')
    source = {r['source_map']: r for r in records}
    maps = []
    for name, codes, center in REGIONS:
        delta = [-math.copysign(inward, center[0]) if center[0] else 0.,
                 -math.copysign(inward, center[1]) if center[1] else 0., 0.]
        for code in codes:
            old = source[code]
            if not isinstance(old['id'], str) or not old['id'].strip():
                raise ValueError('Persistent map identity required')
            position = _xyz(old['position_m'], 'map.position_m')
            rotation = _xyz(old.get('rotation_deg', [0., 0., 0.]), 'map.rotation_deg')
            target_rotation = list(rotation)
            if code == 'mp_terminal':
                target_rotation[2] += 90.
            maps.append(dict(id=old['id'], source_map=code, name=NAMES[code], region=name,
                source_position_m=position, position_m=[a+b for a, b in zip(position, delta)],
                source_rotation_deg=rotation, rotation_deg=target_rotation,
                delta_m=list(delta), central_transform_locked=code in CENTRAL))
    features = [
        ('north-spine', 'ridge', [(-2250, 2250), (-1650, 2430), (-900, 2320),
            (-160, 2460), (600, 2300), (1450, 2390), (2180, 2200)], 850., 360.),
        ('north-peaks', 'ridge', [(-1520, 2250), (-1090, 2390), (-400, 2270),
            (200, 2400), (970, 2260)], 650., 120.),
        ('northwest-shoulder', 'ridge', [(-2350, 1160), (-2500, 1600), (-2230, 2070)], 800., 180.),
        ('northeast-shoulder', 'ridge', [(1650, 1450), (2160, 1880), (2270, 2330)], 850., 230.),
        ('eastern-canyon-rim', 'ridge', [(2440, -1900), (2290, -1050), (2520, -170),
            (2310, 600), (2430, 1320)], 750., 180.),
        ('afghan-mesa', 'hill', [(1570, -850)], 600., 95.),
        ('estate-forested-foothills', 'ridge', [(-1200, 1500), (-800, 1200),
            (-980, 630)], 650., 65.),
        ('southern-forested-hills', 'ridge', [(-1900, -2050), (-1180, -2420),
            (-200, -2500), (600, -2240), (1260, -2410)], 750., 140.),
        ('western-low-ridge', 'ridge', [(-2620, -1570), (-2320, -1040), (-2460, -370)], 650., 80.),
        ('western-rolling-plain', 'hill', [(-1450, -500)], 850., 35.),
        ('southern-rolling-plain', 'hill', [(-500, -1700)], 900., 45.),
        ('eastern-rolling-plain', 'hill', [(1200, 700)], 1000., 40.),
        ('city-basin', 'basin', [(-500, -600), (200, 0), (540, 530)], 780., -18.),
    ]
    relief = [dict(id=stable_id('landscape-relief', name), name=name, kind=kind,
        points_m=[list(p) for p in points], width_m=width, height_m=height)
        for name, kind, points, width, height in features]
    plan = dict(kind=KIND, version=VERSION, units='editor_design_meters', maps=maps,
        bounds_m=[-3072., -3072., 3072., 3072.], inward_design_units=inward,
        source_maps_sha256=canonical_hash(records),
        relief=dict(features=relief, maximum_additive_height_m=550.,
            minimum_additive_height_m=-75., semantics='additive_before_core_protection_and_water_carve',
            organic_coast_radius_m=2680., coast_transition_m=210.,river_valley_width_m=700.),
        biomes=dict(northern_snow_start_y_m=1750., northern_snow_full_y_m=2380.,
            snow_height_start_m=100., snow_height_full_m=260.,
            eastern_sand_start_x_m=750., eastern_sand_full_x_m=1650.,
            city_center_m=[0., 0.], city_radius_m=740., channels=list(CHANNELS)),
        terrain=dict(seed=2009, cells=32, cell_size_m=24., texture_size=512,
            noise_amplitude_m=5., noise_scale_m=220.),
        elevated_map_ids=[source['mp_highrise']['id']], elevated_ground_m=0.,
        river=dict(id=stable_id('river', 'mainland-draft'), width_m=32., bank_width_m=36., depth_m=16.,
            points_m=[[0., 2180., 18.], [-25., 1800., 18.], [-104., 1500., 15.],
                [-104., 1020., 8.], [-104., 900., 6.],
                [-180., 760., 2.], [-650., 650., -10.], [-820., 400., -19.],
                [-818., -900., -22.], [-820., -1540., -25.], [-1000., -1900., -28.],
                [-1250., -2100., -31.], [-1450., -2250., -36.], [-1600., -2350., -40.]],
            status='proposed_editor_watercourse'),
        physical_scale_verified=False, collision_verified=False, runtime_integrated=False,
        available_map_count=16, unavailable_maps=['Summit: no locally evidenced native geometry'],
        native_edits=dict(terminal='restore_connected_original_runway_material_faces',
            highrise='exclude_audited_original_cloud_materials_and_repeat_native_facade_strips'))
    plan['sha256'] = canonical_hash(plan)
    return plan


def validate_plan(plan):
    if (not isinstance(plan, dict) or plan.get('kind') != KIND or plan.get('version') != VERSION
            or plan.get('units') != 'editor_design_meters'):
        raise ValueError('Versioned KARVORSK landscape design plan required')
    unsigned = {k: v for k, v in plan.items() if k != 'sha256'}
    if plan.get('sha256') != canonical_hash(unsigned):
        raise ValueError('Landscape plan content hash differs')
    if {r['source_map'] for r in plan['maps']} != MAP_CODES or len(plan['maps']) != 16:
        raise ValueError('Landscape plan must retain all sixteen source maps')
    map_ids = set()
    for row in plan['maps']:
        if not isinstance(row.get('id'), str) or not row['id'].strip() or row['id'] in map_ids:
            raise ValueError('Unique persistent landscape map identities required')
        map_ids.add(row['id'])
        old = _xyz(row['source_position_m'], 'source_position_m')
        new = _xyz(row['position_m'], 'position_m')
        delta = _xyz(row['delta_m'], 'delta_m')
        if any(abs(a+b-c) > 1e-8 for a, b, c in zip(old, delta, new)):
            raise ValueError('Map translation differs from declared delta')
        if row['source_map'] in CENTRAL and (old != new or delta != [0., 0., 0.]
                or row['source_rotation_deg'] != row['rotation_deg']):
            raise ValueError('Central map transforms are locked')
        rotation = _xyz(row['rotation_deg'], 'rotation_deg')
        old_rotation = _xyz(row['source_rotation_deg'], 'source_rotation_deg')
        expected_yaw = 90. if row['source_map'] == 'mp_terminal' else 0.
        if rotation[:2] != old_rotation[:2] or abs(rotation[2]-old_rotation[2]-expected_yaw) > 1e-8:
            raise ValueError('Only the audited Terminal 90-degree yaw is authorized')
    if plan.get('bounds_m') != [-3072., -3072., 3072., 3072.]:
        raise ValueError('Landscape must retain the existing bounded64-tile authoring extent')
    if any(plan.get(k) is not False for k in ('physical_scale_verified', 'collision_verified', 'runtime_integrated')):
        raise ValueError('Landscape plan cannot assert unverified physical/runtime capabilities')
    relief = plan.get('relief', {})
    if (relief.get('semantics') != 'additive_before_core_protection_and_water_carve'
            or not 100. <= _number(relief.get('maximum_additive_height_m'),'maximum_additive_height_m') <= 1150.
            or relief.get('minimum_additive_height_m') != -75.):
        raise ValueError('Explicit bounded additive relief semantics required')
    features = relief.get('features')
    if not isinstance(features, list) or not 1 <= len(features) <= 32:
        raise ValueError('Landscape supports1..32explicit authored relief features')
    ids = set()
    for feature in features:
        if (feature.get('kind') not in ('ridge', 'hill', 'basin')
                or not isinstance(feature.get('id'), str) or not feature['id'].strip() or feature['id'] in ids):
            raise ValueError('Supported relief kind and unique feature identity required')
        ids.add(feature['id'])
        width = _number(feature.get('width_m'), 'feature.width_m')
        height = _number(feature.get('height_m'), 'feature.height_m')
        if not 16. <= width <= 2000. or not -75. <= height <= relief['maximum_additive_height_m']:
            raise ValueError('Relief feature dimensions exceed bounded authoring range')
        points = feature.get('points_m')
        if not isinstance(points, list) or not 1 <= len(points) <= 128:
            raise ValueError('Relief feature requires1..128explicit XYpoints')
        last = None
        for point in points:
            if not isinstance(point, (list, tuple)) or len(point) != 2:
                raise ValueError('Relief points require two finite coordinates')
            xy = tuple(_number(v, 'feature.point') for v in point)
            if last is not None and math.dist(xy, last) < .001:
                raise ValueError('Relief ridge segments must be nondegenerate')
            last = xy
    for key in ('organic_coast_radius_m', 'coast_transition_m'):
        if not 1. <= _number(relief.get(key), key) <= 5000.:
            raise ValueError('Bounded positive coast dimensions required')
    if 'river_valley_width_m' in relief and not 0. <= _number(
            relief['river_valley_width_m'],'river_valley_width_m') <= 2000.:
        raise ValueError('Authored river valley width must be within 0..2000 design metres')
    biomes = plan.get('biomes', {})
    if biomes.get('channels') != list(CHANNELS):
        raise ValueError('Explicit six-channel landscape material contract required')
    for lo, hi in (('northern_snow_start_y_m', 'northern_snow_full_y_m'),
                   ('snow_height_start_m', 'snow_height_full_m'),
                   ('eastern_sand_start_x_m', 'eastern_sand_full_x_m')):
        if _number(biomes.get(hi), hi) <= _number(biomes.get(lo), lo):
            raise ValueError('Landscape biome transition ranges must increase')
    if _number(biomes.get('city_radius_m'), 'city_radius_m') <= 0:
        raise ValueError('Positive landscape city radius required')
    highrise = next(row['id'] for row in plan['maps'] if row['source_map'] == 'mp_highrise')
    if plan.get('elevated_map_ids') != [highrise]:
        raise ValueError('Only the explicitly authored Highrise rooftop core may be elevated')
    river = plan.get('river', {})
    if river.get('id') != stable_id('river', 'mainland-draft'):
        raise ValueError('Existing persistent river identity required')
    points = river.get('points_m')
    if not isinstance(points, list) or not 2 <= len(points) <= 128:
        raise ValueError('Bounded explicit authored river controls required')
    previous = None
    for point in points:
        point = _xyz(point, 'river.point')
        if previous is not None and (point[2] > previous[2] or math.dist(point[:2], previous[:2]) < .001):
            raise ValueError('River controls must descend with positive horizontal segment lengths')
        previous = point
    for key in ('width_m', 'depth_m', 'bank_width_m'):
        if not 1. <= _number(river.get(key), 'river.'+key) <= 500.:
            raise ValueError('Bounded positive authored river dimensions required')
    return plan


def relief_height(x, y, plan):
    """Additive authored relief; caller must protect cores and carve water."""
    x, y = _number(x, 'x'), _number(y, 'y')
    total = 0.
    for feature in plan['relief']['features']:
        points = feature['points_m']
        distance = (math.hypot(x-points[0][0], y-points[0][1]) if len(points) == 1 else
                    min(_segment_distance(x, y, a, b) for a, b in zip(points, points[1:])))
        profile = math.exp(-2.5*(distance/feature['width_m'])**2)
        total += feature['height_m']*profile
    # A restrained folded rock signal breaks the uniform mound silhouette. Its
    # support is bounded to the mountains; open city and runway remain quiet.
    north = smooth((y-1550.)/850.)
    detail = 32.*(math.sin(x/103.+math.sin(y/179.))*math.cos(y/89.))
    total += north*detail
    angle = math.atan2(y, x)
    coast = (plan['relief']['organic_coast_radius_m'] + 140.*math.sin(angle*3.+.8)
             + 72.*math.sin(angle*7.-.4) + 35.*math.cos(angle*11.))
    radius=math.hypot(x,y)
    # Keep the declared ocean footprint while easing positive mountain relief
    # before the shoreline. Cutting a 500m ridge inside the old 210m sea blend
    # otherwise recreates a near-vertical wall despite wider ridge profiles.
    if plan['relief']['maximum_additive_height_m']<=550. and total>0:
        total*=smooth((coast-radius)/700.)
    coastal = smooth((radius-coast)/plan['relief']['coast_transition_m'])
    total = total*(1.-coastal)-75.*coastal
    # Ease the authored valley before the existing protected-core water carve.
    # This broad design corridor is independent of the actual water/bank width
    # used for road approaches and carving. It only lowers our additive relief.
    # Missing optional widths retain the previous saved-plan interpretation.
    river=plan['river']
    valley_width=plan['relief'].get('river_valley_width_m',
        river['bank_width_m']*5. if river['bank_width_m']>=100. else 0.)
    if valley_width>0.:
        sections=[]
        for a,b in zip(river['points_m'],river['points_m'][1:]):
            dx,dy=b[0]-a[0],b[1]-a[1]
            t=max(0.,min(1.,((x-a[0])*dx+(y-a[1])*dy)/(dx*dx+dy*dy)))
            distance=math.hypot(x-a[0]-t*dx,y-a[1]-t*dy)
            sections.append((distance,a[2]+t*(b[2]-a[2])))
        distance=min(row[0] for row in sections)
        # A hard nearest-section level switches abruptly in a meander's broad
        # valley. Relative distance weights keep the designed floor continuous.
        minimum=max(1.,distance)
        weights=[(minimum/max(1.,d))**4 for d,_ in sections]
        surface=sum(level*weight for (_,level),weight in zip(sections,weights))/sum(weights)
        blend=smooth((distance-river['width_m']/2.)/valley_width)
        valley_ceiling=surface+2.*river['depth_m']
        if total>valley_ceiling:total=valley_ceiling+(total-valley_ceiling)*blend
    return max(plan['relief']['minimum_additive_height_m'],
               min(plan['relief']['maximum_additive_height_m'], total))


def material_weights(x, y, z, plan):
    """Normalized regional authoring weights; native materials remain separate."""
    x, y, z = _number(x, 'x'), _number(y, 'y'), _number(z, 'z')
    biome = plan['biomes']
    north = smooth((y-biome['northern_snow_start_y_m'])/
                   (biome['northern_snow_full_y_m']-biome['northern_snow_start_y_m']))
    snow = north*(.8+.2*smooth((z-biome['snow_height_start_m'])/
        (biome['snow_height_full_m']-biome['snow_height_start_m'])))
    # A cold western valley connects the two native winter maps. The Gaussian
    # declines enough across the 660-unit group spacing that Brecourt/Estate
    # keep their grass dominance on the eastern side of the northern group.
    winter_valley = (.94*math.exp(-.5*((x+330.)/500.)**2)
        *smooth((y-750.)/350.)*(1.-smooth((y-2150.)/350.)))
    snow = max(snow, winter_valley)
    sand = smooth((x-biome['eastern_sand_start_x_m'])/
                  (biome['eastern_sand_full_x_m']-biome['eastern_sand_start_x_m']))*(1.-snow)
    city = (1.-smooth((math.hypot(x, y)-500.)/biome['city_radius_m']))*(1.-snow)*(1.-sand)
    rock = .18*smooth((z-140.)/550.)*(1.-snow)
    grass = (1.-sand)*(1.-snow)*(1.-city)*.78
    weights = dict(sand=.04+sand, soil=.10+.25*(1.-snow), grass=.04+grass,
                   snow=snow, rock=.03+rock, asphalt=.02+.6*city)
    total = sum(weights.values())
    return {key: value/total for key, value in weights.items()}


def transform_position(position, row):
    """Apply declared map-origin yaw and translation to a world position."""
    yaw = math.radians(row['rotation_deg'][2]-row['source_rotation_deg'][2])
    c, s = math.cos(yaw), math.sin(yaw)
    x, y, z = [a-b for a, b in zip(position, row['source_position_m'])]
    target = row['position_m']
    return [target[0]+c*x-s*y, target[1]+s*x+c*y, target[2]+z]


def translated_world(world, plan):
    """Expected semantic translation, useful for native save/export checks.

    Native records use world coordinates even where their Blender objects are
    parented. This function translates by explicit owner IDs and preserves every
    non-position field. Aircraft animation/placement stays fixed in world space.
    """
    result = copy.deepcopy(world)
    transforms = {r['id']: r for r in plan['maps']}
    for key in ('maps', 'markers', 'asset_placements', 'audio_zones', 'action_elements'):
        for row in result.get(key, []):
            owner = (row['id'] if key == 'maps' else
                row.get('owner_map') or row.get('map_instance_id') or row.get('properties', {}).get('editor_owner_map'))
            # The intro aircraft is owner-bound to Rust for inventory only.
            # It has a cinematic transform and must retain its compiled route.
            if key == 'asset_placements' and row.get('placement_kind') == 'cinematic':
                continue
            transform = transforms.get(owner)
            if transform is not None:
                row['position_m'] = transform_position(row['position_m'], transform)
                if key == 'maps':
                    row['rotation_deg'] = list(transform['rotation_deg'])
                elif 'rotation_deg' in row:
                    row['rotation_deg'][2] += transform['rotation_deg'][2]-transform['source_rotation_deg'][2]
                    row['rotation_deg'][2] = (row['rotation_deg'][2]+180.)%360.-180.
    for row in result.get('nav', {}).get('nodes', []):
        transform = transforms.get(row.get('owner_map'))
        if transform is not None:
            row['position_m'] = transform_position(row['position_m'], transform)
    return result


def rotation_disagreement_degrees(a,b):
    """Compare XYZ Euler orientations through their rotations, including gimbals."""
    def matrix(values):
        x,y,z=map(math.radians,values);cx,sx=math.cos(x),math.sin(x)
        cy,sy=math.cos(y),math.sin(y);cz,sz=math.cos(z),math.sin(z)
        return [cz*cy,cz*sy*sx-sz*cx,cz*sy*cx+sz*sx,
            sz*cy,sz*sy*sx+cz*cx,sz*sy*cx-cz*sx,-sy,cy*sx,cy*cx]
    first,second=matrix(a),matrix(b)
    return math.degrees(math.acos(max(-1.,min(1.,(sum(x*y for x,y in zip(first,second))-1.)/2.))))


def check_translated_records(before, after, plan, *, fixed_placement_ids=(), tolerance=.002):
    """Require persistent native identities and exact authored group deltas."""
    expected = translated_world(before, plan)
    for row in expected.get('asset_placements', []):
        if row['id'] in fixed_placement_ids:
            row['position_m'] = next(r['position_m'] for r in before['asset_placements'] if r['id'] == row['id'])
    counts = {};maximum_scale_disagreement=0.;maximum_size_relative_disagreement=0.
    for key in ('maps', 'markers', 'asset_placements', 'audio_zones', 'action_elements'):
        old = {r['id']: r for r in expected.get(key, [])}
        new = {r['id']: r for r in after.get(key, [])}
        if old.keys() != new.keys():
            raise ValueError('Persistent native identities changed: '+key)
        for uid, row in old.items():
            actual = new[uid]
            if any(abs(a-b) > tolerance for a, b in zip(row['position_m'], actual['position_m'])):
                raise ValueError('Native owner translation differs: '+key+'/'+uid)
            for field in ('rotation_deg',):
                if field in row and rotation_disagreement_degrees(row[field],actual[field])>.002:
                    raise ValueError('Native owner rotation differs: '+key+'/'+uid)
            transform_fields={'position_m','rotation_deg'}
            if key=='asset_placements' and 'scale' in row:
                expected_scale=row['scale'];actual_scale=actual.get('scale')
                if not isinstance(actual_scale,(list,tuple)) or len(expected_scale)!=3 or len(actual_scale)!=3:
                    raise ValueError('Native owner scale missing: '+key+'/'+uid)
                differences=[abs(a-b) for a,b in zip(expected_scale,actual_scale)]
                if any(not math.isfinite(v) or v>1e-6 for v in differences):
                    raise ValueError('Native owner scale differs beyond Float32 readback: '+key+'/'+uid)
                maximum_scale_disagreement=max(maximum_scale_disagreement,*differences)
                transform_fields.add('scale')
            # Zone dimensions/radii are serialized from their world matrices.
            # A rigid Float32 parent yaw causes the same tiny scale readback as
            # a placement; authored behavior and source evidence stay exact.
            size_fields=({'dimensions_m','radius_m','inner_radius_m','fade_m','height_m'} if key=='audio_zones'
                         else {'radius_m','explosion_radius_m'} if key=='action_elements' else set())
            for field in size_fields & set(row):
                original=row[field];observed=actual.get(field)
                first=original if isinstance(original,(list,tuple)) else [original]
                second=observed if isinstance(observed,(list,tuple)) else [observed]
                if len(first)!=len(second):raise ValueError('Native world-size shape differs: '+key+'/'+uid+'/'+field)
                for a,b in zip(first,second):
                    if type(b) not in (int,float) or not math.isfinite(b):
                        raise ValueError('Native world-size readback is invalid: '+key+'/'+uid+'/'+field)
                    relative=abs(a-b)/max(1.,abs(a))
                    if relative>1e-6:raise ValueError('Native world-size differs: '+key+'/'+uid+'/'+field)
                    maximum_size_relative_disagreement=max(maximum_size_relative_disagreement,relative)
                transform_fields.add(field)
            old_meta={k:v for k,v in row.items() if k not in transform_fields}
            new_meta={k:v for k,v in actual.items() if k not in transform_fields}
            if old_meta!=new_meta:
                fields=[k for k in sorted(set(old_meta)|set(new_meta)) if old_meta.get(k)!=new_meta.get(k)]
                raise ValueError('Native metadata changed: '+key+'/'+uid+' fields='+','.join(fields))
        counts[key] = len(old)
    old_nav = expected.get('nav', dict(nodes=[], edges=[]))
    new_nav = after.get('nav', dict(nodes=[], edges=[]))
    if old_nav.get('edges', []) != new_nav.get('edges', []):
        raise ValueError('Persistent native navigation edges changed')
    old_nodes = {r['id']: r for r in old_nav.get('nodes', [])}
    new_nodes = {r['id']: r for r in new_nav.get('nodes', [])}
    if old_nodes.keys() != new_nodes.keys():
        raise ValueError('Persistent native navigation node identities changed')
    for uid, row in old_nodes.items():
        actual = new_nodes[uid]
        if (any(abs(a-b) > tolerance for a, b in zip(row['position_m'], actual['position_m']))
                or {k: v for k, v in row.items() if k != 'position_m'} != {
                    k: v for k, v in actual.items() if k != 'position_m'}):
            raise ValueError('Native navigation node transform or metadata differs: '+uid)
    counts['nav_nodes'] = len(old_nodes)
    counts['nav_edges'] = len(old_nav.get('edges', []))
    return dict(counts=counts, central_map_transforms_exact=True,
        outer_group_internal_offsets_preserved=True, position_tolerance_design_units=tolerance,
        scale_readback_tolerance=1e-6,maximum_scale_disagreement=maximum_scale_disagreement,
        world_size_relative_tolerance=1e-6,maximum_world_size_relative_disagreement=maximum_size_relative_disagreement)


def connected_face_components(faces, positions, *, weld_tolerance=.002):
    """Face components through actual shared welded vertices, never AABB overlap."""
    if weld_tolerance <= 0:
        raise ValueError('Positive vertex weld tolerance required')
    parent = list(range(len(faces)))
    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i
    vertices = {}
    for index, face in enumerate(faces):
        for vi in face:
            p = positions[vi]
            key = tuple(round(v/weld_tolerance) for v in p)
            if key in vertices:
                a, b = find(index), find(vertices[key])
                if a != b:
                    parent[max(a, b)] = min(a, b)
            else:
                vertices[key] = index
    groups = {}
    for i in range(len(faces)):
        groups.setdefault(find(i), []).append(i)
    return sorted(groups.values(), key=lambda group: (-len(group), group[0]))


SHOWCASE_VIEWS = (
    dict(name='showcase_01_north_ridges', position_m=[-2770., -2640., 1860.], target_m=[-150., 1500., 290.], lens_mm=38.),
    dict(name='showcase_02_desert_city', position_m=[2900., -2350., 1550.], target_m=[650., 100., 110.], lens_mm=42.),
    dict(name='showcase_03_terminal_highrise', position_m=[-2500., -1400., 1060.], target_m=[-980., 520., 85.], lens_mm=47.),
)
