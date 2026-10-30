"""Opt-in authoring extensions for the existing deterministic terrain engine.

No imported ground or native material is classified here. Coordinates retain
TerrainField's explicit editor-design units. All APIs are independent of bpy;
the caller persists records/UUIDs and performs the normal stale-bake checks.
"""
import copy
import math

try:
    from . import terrain_core as core
except ImportError:
    import terrain_core as core


MATERIALS = tuple(dict.fromkeys((*core.BIOMES, 'asphalt')))
_EPS = 1e-8


def _unit(value, name):
    value = core._number(value, name, nonnegative=True)
    if value > 1:
        raise ValueError(name + ': expected 0..1')
    return value


def _records(value, name, maximum=1024):
    if not isinstance(value, (list, tuple)) or len(value) > maximum:
        raise ValueError(f'{name}: at most {maximum} records required')
    if not all(isinstance(v, dict) for v in value):
        raise ValueError(name + ': object records required')
    ids = [core._identifier(v.get('id'), name + '.id') for v in value]
    if len(set(ids)) != len(ids):
        raise ValueError(name + '.id: duplicate IDs')
    return value


def _material(value):
    if value not in MATERIALS:
        raise ValueError('material: sand/soil/grass/snow/rock/asphalt required')
    return value


def _range(value, name, domain=None):
    if value is None:
        return None
    if not isinstance(value, (list, tuple)) or len(value) != 2:
        raise ValueError(name + ': [minimum, maximum] required')
    low, high = [core._number(v, name) for v in value]
    if high < low or domain and (low < domain[0] or high > domain[1]):
        raise ValueError(name + ': ordered values within the channel domain required')
    return [low, high]


def validate_material_rules(records):
    """Ordered paint layers; each range contributes a smooth multiplicative mask.

    ``{id, material, strength?:1, height_range_m?:[lo,hi],
    slope_range_deg?:[lo,hi], moisture_range?:[lo,hi], height_falloff_m?:0,
    slope_falloff_deg?:0, moisture_falloff?:0, enabled?:True}``.
    Ranges omitted by the author do not constrain that channel. Later layers
    blend over earlier layers, making order explicit and reproducible.
    """
    result = []
    for source in _records(records, 'material_rules', 256):
        enabled = source.get('enabled', True)
        if not isinstance(enabled, bool):
            raise ValueError('material_rules.enabled: boolean required')
        result.append(dict(id=source['id'], material=_material(source.get('material')),
            strength=_unit(source.get('strength', 1), 'strength'), enabled=enabled,
            height_range_m=_range(source.get('height_range_m'), 'height_range_m'),
            slope_range_deg=_range(source.get('slope_range_deg'), 'slope_range_deg', (0, 90)),
            moisture_range=_range(source.get('moisture_range'), 'moisture_range', (0, 1)),
            height_falloff_m=core._number(source.get('height_falloff_m', 0), 'height_falloff_m', nonnegative=True),
            slope_falloff_deg=core._number(source.get('slope_falloff_deg', 0), 'slope_falloff_deg', nonnegative=True),
            moisture_falloff=_unit(source.get('moisture_falloff', 0), 'moisture_falloff')))
    return result


def validate_material_masks(records):
    """Validate explicit, editable polygon paint masks with persistent caller IDs.

    ``{id, polygon:[[x,y],...], material, strength?:1, falloff_m?:0,
    enabled?:True}``. The interior has full strength; falloff fades outward.
    Masks change only generated material weights, never imported mesh data.
    """
    result = []
    for source in _records(records, 'material_masks'):
        enabled = source.get('enabled', True)
        if not isinstance(enabled, bool):
            raise ValueError('material_masks.enabled: boolean required')
        result.append(dict(id=source['id'], polygon=[list(p) for p in core.validate_polygon(source.get('polygon'))],
            material=_material(source.get('material')), enabled=enabled,
            strength=_unit(source.get('strength', 1), 'strength'),
            falloff_m=core._number(source.get('falloff_m', 0), 'falloff_m', nonnegative=True)))
    return result


def upsert_material_mask(records, record):
    """Return independent validated records, retaining the edited layer's ID/order."""
    current = validate_material_masks(records)
    replacement = validate_material_masks([record])[0]
    for index, item in enumerate(current):
        if item['id'] == replacement['id']:
            current[index] = replacement
            break
    else:
        current.append(replacement)
    return validate_material_masks(current)


def remove_material_mask(records, mask_id):
    """Remove only the explicit authoring layer, leaving source ground untouched."""
    core._identifier(mask_id, 'mask_id')
    return [r for r in validate_material_masks(records) if r['id'] != mask_id]


def _segment_distance(point, a, b):
    dx, dy = b[0] - a[0], b[1] - a[1]
    length = dx*dx + dy*dy
    t = max(0., min(1., ((point[0]-a[0])*dx+(point[1]-a[1])*dy)/length)) if length else 0.
    return math.hypot(point[0]-a[0]-t*dx, point[1]-a[1]-t*dy)


def _range_weight(value, interval, falloff):
    if interval is None or interval[0] <= value <= interval[1]:
        return 1.
    if not falloff:
        return 0.
    distance = interval[0]-value if value < interval[0] else value-interval[1]
    return 1-core._smooth(distance/falloff)


def _paint(weights, material, alpha):
    return {key: value*(1-alpha)+(alpha if key == material else 0.)
            for key, value in weights.items()}


class MaterialField(core.TerrainField):
    """TerrainField-compatible material decorator; height/water/normal are reused.

    Base fields retain the existing five-biome contract. Only this opt-in field
    exposes six channels. Asphalt appears through explicit authoring rules or
    polygon masks. It is never guessed from a native model/material name.
    ``generate_tile(field, ...)`` and ``scatter_tile`` accept this subclass.
    """
    def __init__(self, base_field, *, rules=(), masks=()):
        if not isinstance(base_field, core.TerrainField):
            raise ValueError('base_field: existing TerrainField required')
        self.base_field = base_field
        self.rules = validate_material_rules(rules)
        self.masks = validate_material_masks(masks)
        self._mask_bounds = [(min(p[0] for p in m['polygon'])-m['falloff_m'],
            min(p[1] for p in m['polygon'])-m['falloff_m'],
            max(p[0] for p in m['polygon'])+m['falloff_m'],
            max(p[1] for p in m['polygon'])+m['falloff_m']) for m in self.masks]
        self._needs_slope = any(r['enabled'] and r['slope_range_deg'] is not None for r in self.rules)

    def __getattr__(self, name):
        return getattr(self.base_field, name)

    def excluded(self, x, y):
        return self.base_field.excluded(x, y)

    def settings_record(self):
        return dict(kind='wz09_terrain_material_layers', version=1,
                    channels=list(MATERIALS), rules=copy.deepcopy(self.rules), masks=copy.deepcopy(self.masks),
                    coordinate_space='world_editor_design_metres', physical_scale_verified=False)

    def _decorate(self, sample, x, y):
        result = dict(sample)
        weights = {key: max(0., sample['biome_weights'].get(key, 0.)) for key in MATERIALS}
        total = sum(weights.values())
        if not total or not math.isfinite(total):
            raise ValueError('base field: finite nonempty biome weights required')
        weights = {key: value/total for key, value in weights.items()}
        for rule in self.rules:
            if rule['enabled']:
                alpha = rule['strength']
                for channel, interval, falloff in (
                        ('height_m', 'height_range_m', 'height_falloff_m'),
                        ('slope_deg', 'slope_range_deg', 'slope_falloff_deg'),
                        ('wetness', 'moisture_range', 'moisture_falloff')):
                    if rule[interval] is not None:
                        alpha *= _range_weight(result[channel], rule[interval], rule[falloff])
                weights = _paint(weights, rule['material'], alpha)
        for mask,bounds in zip(self.masks,self._mask_bounds):
            if not mask['enabled']:
                continue
            if not bounds[0] <= x <= bounds[2] or not bounds[1] <= y <= bounds[3]:
                continue
            polygon = mask['polygon']
            inside = core._point_in_polygon((x,y), polygon)
            if inside:
                alpha = mask['strength']
            elif mask['falloff_m']:
                distance = min(_segment_distance((x,y), a, b) for a,b in zip(polygon, polygon[1:]+polygon[:1]))
                alpha = mask['strength']*(1-core._smooth(distance/mask['falloff_m']))
            else:
                alpha = 0.
            weights = _paint(weights, mask['material'], alpha)
        result['biome_weights'] = weights
        result['material_channels'] = MATERIALS
        return result

    def sample_surface(self, x, y):
        x,y = core._xy((x,y))
        sample = self.base_field.sample(x,y) if self._needs_slope else self.base_field.sample_surface(x,y)
        return self._decorate(sample,x,y)

    def sample(self, x, y):
        x,y = core._xy((x,y))
        return self._decorate(self.base_field.sample(x,y),x,y)


def water_footprint_distance_m(field, x, y):
    """Horizontal distance to *explicit* sea rectangles/river widths, or None.

    This is an editor placement clearance. It does not infer coasts from source
    meshes or use the legacy unbounded sea-level mask as a water polygon.
    """
    if not isinstance(field, core.TerrainField):
        raise ValueError('field: TerrainField required')
    point = core._xy((x,y))
    distances = []
    for sea in field.seas:
        x0,y0,x1,y1 = sea['bounds']
        distances.append(math.hypot(max(x0-point[0],0.,point[0]-x1), max(y0-point[1],0.,point[1]-y1)))
    for river in field.rivers:
        distances.append(max(0., min(_segment_distance(point,a,b)
            for a,b in zip(river['points'],river['points'][1:]))-river['width_m']/2))
    return min(distances) if distances else None


def validate_clearances(records):
    """Explicit authored obstacle/navigation/action point or polyline envelopes.

    ``{id, kind:'collision'|'navigation'|'interaction', points:[[x,y],...],
    radius_m?:0}``. These inputs reference known geometry/authoring records.
    Proximity tests do not prove native collision or navigable routes.
    """
    result = []
    total_points = 0
    for source in _records(records, 'clearances', 4096):
        kind = source.get('kind')
        points = source.get('points')
        if kind not in ('collision', 'navigation', 'interaction'):
            raise ValueError('clearances.kind: collision/navigation/interaction required')
        if not isinstance(points,(list,tuple)) or not 1 <= len(points) <= 2048:
            raise ValueError('clearances.points: 1..2048 explicit XY points required')
        total_points += len(points)
        if total_points > 100000:
            raise ValueError('clearances: point budget exceeded')
        result.append(dict(id=source['id'],kind=kind,points=[core._xy(p) for p in points],
            radius_m=core._number(source.get('radius_m',0),'clearances.radius_m',nonnegative=True)))
    return result


class _ScatterField(core.TerrainField):
    def __init__(self, base, clearances, distances, exclusions, water_range, material, weight):
        self.base_field = base
        self.clearances, self.distances, self.extra_exclusions = clearances, distances, exclusions
        self.water_range, self.material, self.weight = water_range, material, weight

    def __getattr__(self, name):
        return getattr(self.base_field, name)

    def sample(self, x, y):
        return self.base_field.sample(x,y)

    def excluded(self, x, y):
        if self.base_field.excluded(x,y) or any(core._point_in_polygon((x,y), p) for p in self.extra_exclusions):
            return True
        for obstacle in self.clearances:
            required = self.distances[obstacle['kind']]+obstacle['radius_m']
            points = obstacle['points']
            distance = math.dist((x,y),points[0]) if len(points) == 1 else min(
                _segment_distance((x,y),a,b) for a,b in zip(points,points[1:]))
            if distance < required:
                return True
        low,high = self.water_range
        if low or high is not None:
            distance = water_footprint_distance_m(self.base_field,x,y)
            if (distance is None and high is not None or distance is not None
                    and (distance < low or high is not None and distance > high)):
                return True
        if self.material is not None:
            return self.base_field.sample_surface(x,y)['biome_weights'].get(self.material,0.) < self.weight
        return False


def scatter_filtered_tile(field, tile_x, tile_y, *, clearances=(),
        navigation_clearance_m=0, interaction_clearance_m=0, collision_clearance_m=0,
        exclusions=(), min_water_distance_m=0, max_water_distance_m=None,
        material=None, material_min_weight=.5, **scatter_settings):
    """Reuse existing seeded Matérn scatter, testing new filters before spacing.

    The original scatter options (seed, height range, slope, density, spacing,
    tile extent, underwater permission) are forwarded unchanged. Additional
    material supports asphalt. Exclusions and clearance records are author
    inputs; manually baked assets remain editable through the existing browser.
    """
    if not isinstance(field, core.TerrainField):
        raise ValueError('field: TerrainField required')
    if material is not None:
        _material(material)
    if 'biome' in scatter_settings or 'biome_min_weight' in scatter_settings:
        raise ValueError('use material/material_min_weight for the six-channel filter')
    obstacles = validate_clearances(clearances)
    distances = {key:core._number(value,key+'_clearance_m',nonnegative=True) for key,value in (
        ('navigation',navigation_clearance_m),('interaction',interaction_clearance_m),('collision',collision_clearance_m))}
    polygons = [core.validate_polygon(r.get('polygon')) for r in _records(exclusions,'scatter_exclusions')]
    low = core._number(min_water_distance_m,'min_water_distance_m',nonnegative=True)
    high = None if max_water_distance_m is None else core._number(max_water_distance_m,'max_water_distance_m',nonnegative=True)
    if high is not None and high < low:
        raise ValueError('min_water_distance_m exceeds max_water_distance_m')
    filtered = _ScatterField(field,obstacles,distances,polygons,(low,high),material,
                             _unit(material_min_weight,'material_min_weight'))
    return core.scatter_tile(filtered,tile_x,tile_y,**scatter_settings)


def _triangle_height(point, triangle, tolerance=1e-8):
    a,b,c = triangle
    area = core._cross(a,b,c)
    if abs(area) <= _EPS:
        return None
    u = core._cross(b,c,point)/area
    v = core._cross(c,a,point)/area
    w = 1-u-v
    return u*a[2]+v*b[2]+w*c[2] if min(u,v,w) >= -tolerance else None


def water_level_conflicts(field, *, tolerance_m=.01, max_checks=200000):
    """Compare explicit river centreline crossings and bounded sea intersections.

    Wide banks meeting without intersecting centrelines need separate review;
    no mesh/material name is interpreted as a native water surface.
    """
    if not isinstance(field,core.TerrainField):
        raise ValueError('field: TerrainField required')
    tolerance = core._number(tolerance_m,'tolerance_m',nonnegative=True)
    maximum = core._integer(max_checks,'max_checks',1,10000000)
    checks = 0
    issues = []
    for river in field.rivers:
        for segment,(a,b) in enumerate(zip(river['points'],river['points'][1:])):
            for sea in field.seas:
                checks += 1
                if checks > maximum:
                    return issues+[dict(kind='diagnostic_budget_exhausted',checks=maximum,remaining_water_checks=True)]
                start,end = 0.,1.
                for axis in (0,1):
                    delta = b[axis]-a[axis]
                    low,high = sea['bounds'][axis],sea['bounds'][axis+2]
                    if abs(delta) <= _EPS:
                        if not low <= a[axis] <= high:
                            end = -1.
                            break
                    else:
                        t0,t1 = sorted(((low-a[axis])/delta,(high-a[axis])/delta))
                        start,end = max(start,t0),min(end,t1)
                if start <= end:
                    heights = [a[2]+t*(b[2]-a[2]) for t in (start,end)]
                    gap = max(abs(h-sea['level_m']) for h in heights)
                    if gap > tolerance:
                        issues.append(dict(kind='water_level_conflict',river_id=river['id'],
                            sea_id=sea['id'],segment=segment,difference_m=gap))
    for ri,river in enumerate(field.rivers):
        for other in field.rivers[ri+1:]:
            for ai,(a,b) in enumerate(zip(river['points'],river['points'][1:])):
                for bi,(c,d) in enumerate(zip(other['points'],other['points'][1:])):
                    checks += 1
                    if checks > maximum:
                        return issues+[dict(kind='diagnostic_budget_exhausted',checks=maximum,remaining_water_checks=True)]
                    ab = (b[0]-a[0],b[1]-a[1])
                    cd = (d[0]-c[0],d[1]-c[1])
                    denominator = ab[0]*cd[1]-ab[1]*cd[0]
                    if abs(denominator) <= _EPS:
                        continue
                    ca = (c[0]-a[0],c[1]-a[1])
                    t = (ca[0]*cd[1]-ca[1]*cd[0])/denominator
                    u = (ca[0]*ab[1]-ca[1]*ab[0])/denominator
                    if 0 <= t <= 1 and 0 <= u <= 1:
                        gap = abs(a[2]+t*(b[2]-a[2])-c[2]-u*(d[2]-c[2]))
                        if gap > tolerance:
                            issues.append(dict(kind='water_level_conflict',river_id=river['id'],
                                other_river_id=other['id'],segments=[ai,bi],difference_m=gap))
    return issues


def diagnose_tiles(plans, *, field=None, support_points=(), expected_tile_indices=(), height_tolerance_m=.01,
        slope_limit_deg=45, max_issues=1000, max_vertices=1000000,
        max_faces=2000000, max_geometry_checks=200000):
    """Bounded geometry diagnostics for generated terrain, never a runtime gate.

    Reports invalid vertices/faces/UVs, upwardness/steepness, exact duplicate
    triangles, seam height differences, nonmanifold/open internal edges, and
    explicit support contact points. Protected core holes and exterior working
    boundaries are expected. Support points are ``{id,position_m,max_gap_m?}``
    at the author's chosen contact positions; arbitrary asset origins are not
    classified as ground contacts. Optional expected tile indices identify
    wholly missing tiles. Physics, traversability and imported source ground
    remain separate checks. The geometry budget is shared by overlap/support
    tests; exhausting it is explicit and cannot produce a clean result.
    """
    if not isinstance(plans,(list,tuple)) or not 1 <= len(plans) <= 256:
        raise ValueError('plans: 1..256 explicit terrain tiles required')
    if field is not None and not isinstance(field,core.TerrainField):
        raise ValueError('field: TerrainField required')
    tolerance = core._number(height_tolerance_m,'height_tolerance_m',positive=True)
    slope_limit = core._number(slope_limit_deg,'slope_limit_deg',nonnegative=True)
    if slope_limit > 90:
        raise ValueError('slope_limit_deg: expected <=90')
    maximum = core._integer(max_issues,'max_issues',1,100000)
    vertex_budget = core._integer(max_vertices,'max_vertices',3,5000000)
    face_budget = core._integer(max_faces,'max_faces',1,5000000)
    check_budget = core._integer(max_geometry_checks,'max_geometry_checks',1,10000000)
    contacts = _records(support_points,'support_points',10000)
    if not isinstance(expected_tile_indices,(list,tuple)) or len(expected_tile_indices) > 4096:
        raise ValueError('expected_tile_indices: at most 4096 XY tile indices required')
    expected = set()
    for value in expected_tile_indices:
        if not isinstance(value,(list,tuple)) or len(value) != 2:
            raise ValueError('expected_tile_indices: integer XY pairs required')
        expected.add(tuple(core._integer(i,'tile index',-100000,100000) for i in value))
    issues, counts = [], {}

    def report(kind, **details):
        counts[kind] = counts.get(kind,0)+1
        if len(issues) < maximum:
            issues.append(dict(kind=kind,**details))

    for plan in plans:
        if (not isinstance(plan,dict) or not isinstance(plan.get('vertices'),(list,tuple))
                or not isinstance(plan.get('faces'),(list,tuple))
                or not isinstance(plan.get('uvs',()),(list,tuple))):
            raise ValueError('plan: vertices/faces/uvs arrays required')
    vertices_total = sum(len(p['vertices']) for p in plans)
    if vertices_total > vertex_budget:
        raise ValueError('diagnostics vertex budget exceeded; inspect selected tiles')
    if sum(len(p['faces']) for p in plans) > face_budget:
        raise ValueError('diagnostics face budget exceeded; inspect selected tiles')
    edges, columns, signatures, triangles, bounds, found_tiles = {}, {}, {}, [], [], set()
    quant = lambda value: round(value,8)
    xykey = lambda v: (quant(v[0]),quant(v[1]))
    for tile_index, plan in enumerate(plans):
        if not isinstance(plan,dict):
            raise ValueError('plan: object required')
        tile = plan.get('tile')
        if isinstance(tile,(list,tuple)) and len(tile) == 2:
            found_tiles.add(tuple(core._integer(i,'tile index',-100000,100000) for i in tile))
        raw = plan.get('vertices',())
        uvs = plan.get('uvs',())
        if not isinstance(raw,(list,tuple)) or not isinstance(plan.get('faces'),(list,tuple)):
            raise ValueError('plan: vertices/faces arrays required')
        valid = {}
        for index, vertex in enumerate(raw):
            try:
                value = core._xyz(vertex,'vertex')
                valid[index] = value
                columns.setdefault(xykey(value),[]).append((value[2],tile_index,index))
            except ValueError:
                report('invalid_vertex',tile=tile_index,vertex=index)
        if len(uvs) != len(raw):
            report('missing_uvs',tile=tile_index,vertices=len(raw),uvs=len(uvs))
        valid_uv = {}
        for index, uv in enumerate(uvs):
            try:
                valid_uv[index] = core._xy(uv,'uv')
            except ValueError:
                report('invalid_uv',tile=tile_index,vertex=index)
        b = plan.get('bounds_m')
        if not isinstance(b,(list,tuple)) or len(b) != 4:
            raise ValueError('plan.bounds_m: explicit tile rectangle required')
        b = tuple(core._number(v,'bounds_m') for v in b)
        if b[2] <= b[0] or b[3] <= b[1]:
            raise ValueError('plan.bounds_m: positive rectangle required')
        bounds.append(b)
        for face_index, face in enumerate(plan['faces']):
            if (not isinstance(face,(list,tuple)) or len(face) != 3
                    or any(isinstance(i,bool) or not isinstance(i,int) or i not in valid for i in face)
                    or len(set(face)) != 3):
                report('invalid_face',tile=tile_index,face=face_index)
                continue
            tri = [valid[i] for i in face]
            a,bp,c = tri
            ux,uy,uz = [bp[i]-a[i] for i in range(3)]
            vx,vy,vz = [c[i]-a[i] for i in range(3)]
            normal = (uy*vz-uz*vy,uz*vx-ux*vz,ux*vy-uy*vx)
            if math.sqrt(sum(v*v for v in normal)) <= _EPS or abs(normal[2]) <= _EPS:
                report('degenerate_heightfield_face',tile=tile_index,face=face_index)
                continue
            if normal[2] < 0:
                report('inverted_face',tile=tile_index,face=face_index)
            slope = math.degrees(math.atan2(math.hypot(*normal[:2]),abs(normal[2])))
            if slope > slope_limit:
                report('steep_face',tile=tile_index,face=face_index,slope_deg=slope)
            if all(i in valid_uv for i in face) and abs(core._cross(*(valid_uv[i] for i in face))) <= _EPS:
                report('collapsed_uv_face',tile=tile_index,face=face_index)
            signature = tuple(sorted(tuple(quant(v) for v in p) for p in tri))
            if signature in signatures:
                report('duplicate_face',tile=tile_index,face=face_index,other=signatures[signature])
            signatures[signature] = [tile_index,face_index]
            triangles.append((tile_index,face_index,tri))
            for p,q in zip(tri,tri[1:]+tri[:1]):
                key = tuple(sorted((xykey(p),xykey(q))))
                edges.setdefault(key,[]).append((tile_index,face_index))
    for missing in sorted(expected-found_tiles):
        report('missing_tile',tile=list(missing))
    for key, entries in columns.items():
        low,high = min(v[0] for v in entries),max(v[0] for v in entries)
        if high-low > tolerance:
            report('height_gap',xy_m=list(key),difference_m=high-low,tiles=sorted(set(v[1] for v in entries)))
    neighbors = [[j for j,b in enumerate(bounds) if j != i and
        min(a[2],b[2]) >= max(a[0],b[0])-_EPS and min(a[3],b[3]) >= max(a[1],b[1])-_EPS]
        for i,a in enumerate(bounds)]
    for key, owners in edges.items():
        if len(owners) > 2:
            report('nonmanifold_edge',edge_xy_m=key,faces=owners)
        elif len(owners) == 1:
            point = tuple((a+b)/2 for a,b in zip(*key))
            if field is not None and any(r['bounds'][0]-_EPS <= point[0] <= r['bounds'][2]+_EPS
                and r['bounds'][1]-_EPS <= point[1] <= r['bounds'][3]+_EPS
                and core._point_in_polygon(point,r['polygon']) for r in field.regions):
                continue
            tile_index = owners[0][0]
            x0,y0,x1,y1 = bounds[tile_index]
            own_boundary = (abs(key[0][0]-key[1][0]) <= _EPS and min(abs(point[0]-x0),abs(point[0]-x1)) <= _EPS
                or abs(key[0][1]-key[1][1]) <= _EPS and min(abs(point[1]-y0),abs(point[1]-y1)) <= _EPS)
            neighbor = any(bounds[i][0]-_EPS <= point[0] <= bounds[i][2]+_EPS
                and bounds[i][1]-_EPS <= point[1] <= bounds[i][3]+_EPS for i in neighbors[tile_index])
            if not own_boundary or neighbor:
                report('open_transition',tile=tile_index,edge_xy_m=key,evidence='unmatched_xy_edge_or_t_junction')
    checks = 0
    # Buckets restrict positive-area overlap checks to nearby triangles. Reuse
    # terrain_core's exact convex clipping instead of inventing a second clipper.
    extent = max(max(b[2] for b in bounds)-min(b[0] for b in bounds),
                 max(b[3] for b in bounds)-min(b[1] for b in bounds))
    bucket_size = max(.001,extent/max(1.,math.sqrt(len(triangles))))
    buckets, seen_pairs = {}, set()
    exhausted = False
    for ti,(tile,face,tri) in enumerate(triangles):
        xs,ys = [p[0] for p in tri],[p[1] for p in tri]
        ix0,ix1 = math.floor(min(xs)/bucket_size),math.floor(max(xs)/bucket_size)
        iy0,iy1 = math.floor(min(ys)/bucket_size),math.floor(max(ys)/bucket_size)
        if (ix1-ix0+1)*(iy1-iy0+1) > check_budget:
            exhausted = True
            break
        for iy in range(iy0,iy1+1):
            for ix in range(ix0,ix1+1):
                checks += 1
                if checks > check_budget:
                    exhausted = True
                    break
                bucket = buckets.setdefault((ix,iy),[])
                for oi in bucket:
                    pair = (oi,ti)
                    if pair in seen_pairs:
                        continue
                    seen_pairs.add(pair)
                    checks += 1
                    if checks > check_budget:
                        exhausted = True
                        break
                    ot,of,other = triangles[oi]
                    ox,oy = [p[0] for p in other],[p[1] for p in other]
                    if min(max(xs),max(ox))-max(min(xs),min(ox)) <= _EPS or min(max(ys),max(oy))-max(min(ys),min(oy)) <= _EPS:
                        continue
                    p,q = [v[:2] for v in tri],[v[:2] for v in other]
                    if core._area(p) < 0:
                        p.reverse()
                    if core._area(q) < 0:
                        q.reverse()
                    area = core._overlap_area(p,q)
                    if area > _EPS:
                        report('terrain_overlap',tile=tile,face=face,other=[ot,of],projected_area_m2=area)
                bucket.append(ti)
                if exhausted:
                    break
            if exhausted:
                break
        if exhausted:
            break
    if exhausted:
        report('diagnostic_budget_exhausted',checks=check_budget,remaining_overlap_checks=True)
    overlap_checks = min(checks,check_budget)
    support_checks = 0
    for contact in contacts:
        point = core._xyz(contact.get('position_m'),'support.position_m')
        gap = core._number(contact.get('max_gap_m',.1),'support.max_gap_m',nonnegative=True)
        hits = []
        exhausted = checks >= check_budget
        if exhausted:
            report('diagnostic_budget_exhausted',checks=check_budget,remaining_support_points=True)
            break
        for tile,face,tri in triangles:
            bx0,by0,bx1,by1 = bounds[tile]
            if not bx0-_EPS <= point[0] <= bx1+_EPS or not by0-_EPS <= point[1] <= by1+_EPS:
                continue
            checks += 1
            support_checks += 1
            if checks > check_budget:
                exhausted = True
                break
            z = _triangle_height(point,tri)
            if z is not None:
                hits.append(z)
        if exhausted:
            report('diagnostic_budget_exhausted',checks=check_budget,remaining_support_points=True)
            break
        if not hits:
            report('missing_support',id=contact['id'],position_m=point)
        else:
            nearest = min(hits,key=lambda z:abs(z-point[2]))
            if abs(nearest-point[2]) > gap:
                report('support_height_gap',id=contact['id'],difference_m=point[2]-nearest,max_gap_m=gap)
    if field is not None:
        for issue in water_level_conflicts(field,tolerance_m=tolerance):
            report(issue['kind'],**{key:value for key,value in issue.items() if key != 'kind'})
    return dict(kind='wz09_terrain_geometry_diagnostics',version=1,issues=issues,counts=counts,
        issue_count=sum(counts.values()),truncated=sum(counts.values()) > len(issues),
        checked_tiles=len(plans),checked_vertices=vertices_total,geometry_checks=min(checks,check_budget),
        overlap_geometry_checks=overlap_checks,support_triangle_checks=support_checks,
        scope='authored_heightfield_geometry_only',
        unchecked=['water_bank_only_or_collinear_contacts','structural_support','native_source_ground','runtime_collision','runtime_navigation'],
        physical_scale_verified=False,collision_verified=False,navigation_verified=False)
