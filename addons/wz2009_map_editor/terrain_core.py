"""Deterministic terrain, water and scatter plans without a Blender dependency.

Coordinates are explicit *editor design metres*. Nothing in this module infers
MW2 source scale, classifies imported triangles, or proves runtime collision.

``TerrainField(regions, ...)`` accepts confirmed, non-overlapping simple polygon
records: ``{id, polygon:[[x,y],...], height_m, biome, confirmed:True,
transition_m?:50, boundary_heights_m?:[z,...]}``. Optional per-boundary heights
are interpolated on polygon edges. They must come from ground selected by the
caller; a roof hit is not automatically ground. ``rivers`` are records with
``{id, points:[[x,y,z],...], width_m, depth_m, bank_width_m?}``; z is the chosen
water surface. Exclusions are confirmed polygon records ``{id, polygon}``.
Optional ``seas`` records are ``{id, bounds:[minX,minY,maxX,maxY], level_m,
depth_m, coast_width_m, carve_terrain:bool}``. Bounds/level describe actual
water coverage. Basin carving is opt-in, affects only generated ground, and
fades at both the rectangle coast and protected core edges. Without sea records
the existing global ``sea_level_m`` mask behavior is retained.

``generate_tile`` clips protected cores out of new geometry, including concave
cores, and samples one global integer lattice across all tiles. Arbitrary
boundary intersection vertices use the same deterministic field. Core interiors
are never modified. Mask weights are normalized and provided per vertex.

Scatter uses globally indexed, seeded candidates with local priority rejection,
so separate adjacent tile calls retain spacing, seed and exclusion behavior.
Candidate IDs are reproducible provenance keys, not persistent placement UUIDs.
The caller must keep stable authoring IDs when baking actual assets.
"""

import hashlib
import math
import struct


BIOMES = ('sand', 'soil', 'grass', 'snow', 'rock')
_EPS = 1e-8
_MAX_COORD = 1e8
_MAX_REGIONS = 256
_MAX_POLYGON_VERTICES = 256


def _number(value, name, *, positive=False, nonnegative=False):
    if (isinstance(value, bool) or not isinstance(value, (int, float))
            or abs(value) > _MAX_COORD or not math.isfinite(value)):
        raise ValueError(name + ': a finite bounded number is required')
    value = float(value)
    if positive and value < .001 or nonnegative and value < 0:
        raise ValueError(name + ': positive dimensions must be at least .001 design metres; negatives are invalid')
    return value


def _integer(value, name, lo, hi):
    if isinstance(value, bool) or not isinstance(value, int) or not lo <= value <= hi:
        raise ValueError(f'{name}: integer in [{lo}, {hi}] required')
    return value


def _xy(value, name='point'):
    if not isinstance(value, (tuple, list)) or len(value) != 2:
        raise ValueError(name + ': two finite coordinates required')
    return tuple(_number(v, name) for v in value)


def _xyz(value, name='point'):
    if not isinstance(value, (tuple, list)) or len(value) != 3:
        raise ValueError(name + ': three finite coordinates required')
    return tuple(_number(v, name) for v in value)


def _identifier(value, name='id'):
    if not isinstance(value, str) or not value.strip() or len(value) > 256:
        raise ValueError(name + ': nonempty text of at most 256 characters required')
    return value


def _cross(a, b, p):
    return (b[0]-a[0]) * (p[1]-a[1]) - (b[1]-a[1]) * (p[0]-a[0])


def _area(polygon):
    # Translating before summation avoids catastrophic loss for distant maps.
    ox, oy = polygon[0]
    return sum((a[0]-ox)*(b[1]-oy) - (b[0]-ox)*(a[1]-oy)
               for a, b in zip(polygon, polygon[1:]+polygon[:1])) / 2


def _on_segment(point, a, b):
    return (abs(_cross(a, b, point)) <= _EPS * max(1, math.dist(a, b))
            and min(a[0], b[0])-_EPS <= point[0] <= max(a[0], b[0])+_EPS
            and min(a[1], b[1])-_EPS <= point[1] <= max(a[1], b[1])+_EPS)


def _segments_intersect(a, b, c, d):
    ab_c, ab_d, cd_a, cd_b = _cross(a,b,c), _cross(a,b,d), _cross(c,d,a), _cross(c,d,b)
    if ab_c * ab_d < -_EPS and cd_a * cd_b < -_EPS:
        return True
    return any((_on_segment(p, u, v)) for p, u, v in
               ((c,a,b), (d,a,b), (a,c,d), (b,c,d)))


def _point_in_polygon(point, polygon, include_boundary=True):
    x, y = point
    inside = False
    for a, b in zip(polygon, polygon[1:]+polygon[:1]):
        if _on_segment(point, a, b):
            return include_boundary
        if (a[1] > y) != (b[1] > y):
            if x < a[0] + (y-a[1]) * (b[0]-a[0]) / (b[1]-a[1]):
                inside = not inside
    return inside


def point_in_polygon(point, polygon, *, include_boundary=True):
    """Validated even/odd containment, with explicit boundary inclusion."""
    if not isinstance(include_boundary,bool):
        raise ValueError('include_boundary: boolean required')
    return _point_in_polygon(_xy(point),validate_polygon(polygon),include_boundary)


def validate_polygon(value, name='polygon'):
    """Return a CCW simple polygon. Degenerate/self-crossing rings are rejected."""
    if (not isinstance(value, (list, tuple))
            or not 3 <= len(value) <= _MAX_POLYGON_VERTICES):
        raise ValueError(f'{name}: 3..{_MAX_POLYGON_VERTICES} vertices required')
    polygon = [_xy(p, name) for p in value]
    if len(set(polygon)) != len(polygon):
        raise ValueError(name + ': repeated vertices or repeated closing point')
    n = len(polygon)
    for i in range(n):
        if math.dist(polygon[i], polygon[(i+1) % n]) <= _EPS:
            raise ValueError(name + ': zero-length edge')
        for j in range(i+1, n):
            if j == i+1 or (i == 0 and j == n-1):
                continue
            if _segments_intersect(polygon[i], polygon[(i+1) % n],
                                   polygon[j], polygon[(j+1) % n]):
                raise ValueError(name + ': self-intersecting polygon')
    area = _area(polygon)
    if abs(area) <= _EPS:
        raise ValueError(name + ': zero-area polygon')
    return polygon if area > 0 else list(reversed(polygon))


def _nearest(point, polygon, heights):
    best = (float('inf'), 0.0)
    for i, a in enumerate(polygon):
        b = polygon[(i+1) % len(polygon)]
        dx, dy = b[0]-a[0], b[1]-a[1]
        t = max(0.0, min(1.0, ((point[0]-a[0])*dx+(point[1]-a[1])*dy)/(dx*dx+dy*dy)))
        d = math.hypot(point[0]-a[0]-t*dx, point[1]-a[1]-t*dy)
        if d < best[0]:
            best = (d, heights[i]*(1-t)+heights[(i+1) % len(heights)]*t)
    return best


def _smooth(value):
    value = min(1.0, max(0.0, value))
    return value * value * (3-2*value)


def _random(seed, x, y, channel):
    data = f'{seed}:{x}:{y}:{channel}'.encode('ascii')
    factory = hashlib.blake2b
    hash_object = factory(data, digest_size=8)
    if type(factory) is not type or type(hash_object) is not factory:
        raise TypeError('_random: unexpected blake2b factory/object types: '
                        +type(factory).__name__+'/'+type(hash_object).__name__)
    digest_bytes = hash_object.digest()
    if type(digest_bytes) is not bytes:
        raise TypeError('_random: blake2b digest must be bytes, got '+type(digest_bytes).__name__)
    if len(digest_bytes) != 8:
        raise ValueError('_random: blake2b digest must contain exactly 8 bytes')
    value = int.from_bytes(digest_bytes, 'big') / 2**64
    return value


def _noise(seed, x, y):
    ix, iy = math.floor(x), math.floor(y)
    tx, ty = _smooth(x-ix), _smooth(y-iy)
    a = _random(seed,ix,iy,0)*(1-tx) + _random(seed,ix+1,iy,0)*tx
    b = _random(seed,ix,iy+1,0)*(1-tx) + _random(seed,ix+1,iy+1,0)*tx
    return (a*(1-ty)+b*ty)*2-1


def _triangulate(polygon):
    """Ear clipping for a validated simple CCW ring; retains concave holes."""
    ring, triangles = list(polygon), []
    while len(ring) > 3:
        found = False
        for i in range(len(ring)):
            a, b, c = ring[i-1], ring[i], ring[(i+1) % len(ring)]
            if _cross(a,b,c) <= _EPS:
                continue
            others = [p for p in ring if p not in (a,b,c)]
            if any(_cross(a,b,p) >= -_EPS and _cross(b,c,p) >= -_EPS
                   and _cross(c,a,p) >= -_EPS for p in others):
                continue
            triangles.append([a,b,c])
            del ring[i]
            found = True
            break
        if not found:
            # Remove a redundant collinear vertex, then try again.
            for i in range(len(ring)):
                if abs(_cross(ring[i-1],ring[i],ring[(i+1) % len(ring)])) <= _EPS:
                    del ring[i]
                    found = True
                    break
            if not found:
                raise ValueError('polygon cannot be triangulated safely')
    if len(ring) == 3 and _area(ring) > _EPS:
        triangles.append(ring)
    return triangles


def _clip_halfplane(polygon, a, b, keep_inside=True):
    output = []
    for p, q in zip(polygon, polygon[1:]+polygon[:1]):
        dp, dq = _cross(a,b,p), _cross(a,b,q)
        ip = dp >= -_EPS if keep_inside else dp <= _EPS
        iq = dq >= -_EPS if keep_inside else dq <= _EPS
        if ip:
            output.append(p)
        if ip != iq and abs(dp-dq) > 1e-20:
            t = dp/(dp-dq)
            output.append((round(p[0]+t*(q[0]-p[0]), 10),
                           round(p[1]+t*(q[1]-p[1]), 10)))
    clean = []
    for p in output:
        if not clean or math.dist(clean[-1],p) > _EPS:
            clean.append(p)
    if len(clean) > 1 and math.dist(clean[0],clean[-1]) <= _EPS:
        clean.pop()
    return clean if len(clean) >= 3 and abs(_area(clean)) > _EPS else []


def _partition_convex(subject, clip):
    """Outside convex pieces plus the retained inside intersection."""
    remainder, pieces = subject, []
    for a, b in zip(clip,clip[1:]+clip[:1]):
        if not remainder:
            break
        outside = _clip_halfplane(remainder,a,b,False)
        if outside:
            pieces.append(outside)
        remainder = _clip_halfplane(remainder,a,b,True)
    return pieces, remainder


def _subtract_convex(subject, clip):
    """Subtract with cuts bounded to the clip AABB, including at tile seams.

    Unbounded half-plane partitions would add artificial cuts to neighboring
    cells that never touch the core. Bounding them makes both sides of a shared
    grid edge perform the same cuts whenever a clipping piece reaches the edge.
    """
    x0,y0,x1,y1 = _bounds(clip)
    box = [(x0,y0),(x1,y0),(x1,y1),(x0,y1)]
    outside, intersection = _partition_convex(subject,box)
    if intersection:
        pieces, remainder = _partition_convex(intersection,clip)
        outside.extend(pieces)
    return outside


def _overlap_area(a, b):
    total = 0.0
    for ta in _triangulate(a):
        for tb in _triangulate(b):
            piece = ta
            for x, y in zip(tb,tb[1:]+tb[:1]):
                piece = _clip_halfplane(piece,x,y) if piece else []
            if piece:
                total += abs(_area(piece))
    return total


def _bounds(polygon):
    return (min(p[0] for p in polygon), min(p[1] for p in polygon),
            max(p[0] for p in polygon), max(p[1] for p in polygon))


def _bounds_overlap(a, b):
    return not (a[2] < b[0]-_EPS or b[2] < a[0]-_EPS
                or a[3] < b[1]-_EPS or b[3] < a[1]-_EPS)


def validate_regions(regions):
    """Normalize confirmed source-ground cores without changing caller records."""
    if not isinstance(regions, (list, tuple)) or not 1 <= len(regions) <= _MAX_REGIONS:
        raise ValueError('regions: 1..256 confirmed regions required')
    normalized, ids = [], set()
    for source in regions:
        if not isinstance(source, dict) or source.get('confirmed') is not True:
            raise ValueError('regions: explicit confirmed=True is required')
        rid = _identifier(source.get('id'), 'region.id')
        if rid in ids:
            raise ValueError('region.id: duplicate ' + rid)
        ids.add(rid)
        polygon = validate_polygon(source.get('polygon'), 'region.polygon')
        height = _number(source.get('height_m'), 'region.height_m')
        biome = source.get('biome')
        if biome not in BIOMES:
            raise ValueError('region.biome: unsupported biome')
        heights = source.get('boundary_heights_m', [height]*len(polygon))
        if not isinstance(heights,(list,tuple)) or len(heights) != len(polygon):
            raise ValueError('boundary_heights_m must match polygon vertices')
        heights = [_number(z, 'boundary_height_m') for z in heights]
        # Boundary values refer to the caller's vertex order, including CW rings.
        if _area([tuple(p) for p in source['polygon']]) < 0:
            heights.reverse()
        normalized.append(dict(id=rid, polygon=polygon, heights=heights, biome=biome,
                               transition_m=_number(source.get('transition_m',50),
                                                    'transition_m',positive=True),
                               bounds=_bounds(polygon), triangles=_triangulate(polygon)))
    for i, a in enumerate(normalized):
        for b in normalized[i+1:]:
            if _bounds_overlap(a['bounds'],b['bounds']) and _overlap_area(a['polygon'],b['polygon']) > _EPS:
                raise ValueError('protected core overlap: '+a['id']+' / '+b['id'])
    return normalized


def validate_river(source):
    """Normalize a chosen water centerline; no inferred flow or source semantics."""
    if not isinstance(source, dict):
        raise ValueError('river: object required')
    points = source.get('points')
    if not isinstance(points,(list,tuple)) or not 2 <= len(points) <= 2048:
        raise ValueError('river.points: 2..2048 points required')
    points = [_xyz(p,'river.point') for p in points]
    for a, b in zip(points,points[1:]):
        if math.dist(a[:2],b[:2]) <= _EPS:
            raise ValueError('river centerline requires horizontal segment length')
        if b[2] > a[2]+_EPS:
            raise ValueError('river points must be ordered downstream (non-increasing water height)')
    return dict(id=_identifier(source.get('id'),'river.id'), points=points,
                width_m=_number(source.get('width_m'),'river.width_m',positive=True),
                depth_m=_number(source.get('depth_m'),'river.depth_m',positive=True),
                bank_width_m=_number(source.get('bank_width_m',5),'river.bank_width_m',positive=True))


def validate_sea(source):
    """Normalize an explicit bounded sea surface and opt-in generated basin."""
    if not isinstance(source,dict):
        raise ValueError('sea: object required')
    bounds=source.get('bounds')
    if not isinstance(bounds,(tuple,list)) or len(bounds)!=4:
        raise ValueError('sea.bounds: [minX,minY,maxX,maxY] required')
    x0,y0,x1,y1=[_number(v,'sea.bounds') for v in bounds]
    if x1-x0 < .001 or y1-y0 < .001:
        raise ValueError('sea.bounds: positive design extent of at least .001 required')
    if type(source.get('carve_terrain')) is not bool:
        raise ValueError('sea.carve_terrain: explicit boolean required')
    return dict(id=_identifier(source.get('id'),'sea.id'),bounds=(x0,y0,x1,y1),
                level_m=_number(source.get('level_m'),'sea.level_m'),
                depth_m=_number(source.get('depth_m'),'sea.depth_m',positive=True),
                coast_width_m=_number(source.get('coast_width_m'),'sea.coast_width_m',positive=True),
                carve_terrain=source['carve_terrain'])


class TerrainField:
    """Continuous, deterministic design field over explicitly confirmed cores."""

    def __init__(self, regions, *, seed=0, noise_amplitude_m=0, noise_scale_m=200,
                 sea_level_m=0, rivers=(), exclusions=(), seas=()):
        self.regions = validate_regions(regions)
        self._regions_by_id = {r['id']:r for r in self.regions}
        self.seed = _integer(seed,'seed',-2**63,2**63-1)
        self.noise_amplitude_m = _number(noise_amplitude_m,'noise_amplitude_m',nonnegative=True)
        self.noise_scale_m = _number(noise_scale_m,'noise_scale_m',positive=True)
        self.sea_level_m = _number(sea_level_m,'sea_level_m')
        if not isinstance(rivers,(tuple,list)) or len(rivers) > 256:
            raise ValueError('rivers: at most 256 records required')
        self.rivers = [validate_river(r) for r in rivers]
        if len({r['id'] for r in self.rivers}) != len(self.rivers):
            raise ValueError('river.id: duplicates')
        if not isinstance(seas,(tuple,list)) or len(seas)>256:
            raise ValueError('seas: at most 256 records required')
        self.seas=[validate_sea(s) for s in seas]
        water_ids=[r['id'] for r in self.rivers]+[s['id'] for s in self.seas]
        if len(set(water_ids))!=len(water_ids):
            raise ValueError('water.id: duplicate river or sea ID')
        for i,a in enumerate(self.seas):
            for b in self.seas[i+1:]:
                ab,bb=a['bounds'],b['bounds']
                if (min(ab[2],bb[2])-max(ab[0],bb[0])>_EPS
                        and min(ab[3],bb[3])-max(ab[1],bb[1])>_EPS
                        and abs(a['level_m']-b['level_m'])>_EPS):
                    raise ValueError('sea: overlapping bounds have conflicting water levels')
        if not isinstance(exclusions,(tuple,list)) or len(exclusions) > 1024:
            raise ValueError('exclusions: at most 1024 polygon records required')
        self.exclusions = []
        ids = set()
        for source in exclusions:
            if not isinstance(source,dict):
                raise ValueError('exclusion: polygon record required')
            eid = _identifier(source.get('id'),'exclusion.id')
            if eid in ids:
                raise ValueError('exclusion.id: duplicates')
            ids.add(eid)
            self.exclusions.append(validate_polygon(source.get('polygon'),'exclusion.polygon'))

    def _river_at(self, x, y):
        nearest = None
        for river in self.rivers:
            for a, b in zip(river['points'],river['points'][1:]):
                dx, dy = b[0]-a[0], b[1]-a[1]
                t = max(0.,min(1.,((x-a[0])*dx+(y-a[1])*dy)/(dx*dx+dy*dy)))
                distance = math.hypot(x-a[0]-t*dx,y-a[1]-t*dy)
                value = dict(distance_m=distance, surface_m=a[2]*(1-t)+b[2]*t, river=river)
                # Compare bank distance, so a broad river influences its full width.
                if nearest is None or distance-river['width_m']/2 < nearest['distance_m']-nearest['river']['width_m']/2:
                    nearest = value
        return nearest

    def _base(self, x, y):
        entries = []
        for region in self.regions:
            distance, height = _nearest((x,y),region['polygon'],region['heights'])
            bounds = region['bounds']
            if (bounds[0]-_EPS <= x <= bounds[2]+_EPS and bounds[1]-_EPS <= y <= bounds[3]+_EPS
                    and _point_in_polygon((x,y),region['polygon'])):
                return height, {b:float(b == region['biome']) for b in BIOMES}, True, region['id'], 0.
            entries.append((distance,height,region))
        closest = min(entries,key=lambda e:e[0])
        if closest[0] <= _EPS:
            return closest[1], {b:float(b == closest[2]['biome']) for b in BIOMES}, False, closest[2]['id'], 0.
        # Relative weights avoid division overflow near a confirmed boundary.
        minimum = min(d/r['transition_m'] for d,h,r in entries)
        weights = [(minimum/(d/r['transition_m']))**2 for d,h,r in entries]
        norm = sum(weights)
        height = sum(h*w for (d,h,r),w in zip(entries,weights))/norm
        biomes = {b:sum(w for (d,h,r),w in zip(entries,weights) if r['biome'] == b)/norm for b in BIOMES}
        fade = _smooth(closest[0]/closest[2]['transition_m'])
        height += self.noise_amplitude_m * fade * _noise(self.seed,x/self.noise_scale_m,y/self.noise_scale_m)
        return height, biomes, False, closest[2]['id'], closest[0]

    @staticmethod
    def _carve(height, protected, water, core_fade=1):
        if water and not protected:
            river = water['river']
            half = river['width_m']/2
            d = water['distance_m']
            if d < half + river['bank_width_m']:
                bowl = max(0.,1-(d/half)**2)
                bed = water['surface_m']-river['depth_m']*bowl
                weight = (1-_smooth((d-half)/river['bank_width_m'])) * core_fade
                height = min(height,height*(1-weight)+bed*weight)
        return height

    def _carve_seas(self,height,x,y,protected,core_fade):
        if protected:
            return height
        original=height
        for sea in self.seas:
            if not sea['carve_terrain']:
                continue
            x0,y0,x1,y1=sea['bounds']
            edge=min(x-x0,x1-x,y-y0,y1-y)
            if edge<=0:
                continue
            weight=_smooth(edge/sea['coast_width_m'])*core_fade
            target=sea['level_m']-sea['depth_m']
            height=min(height,original*(1-weight)+target*weight)
        return height

    def _wetness(self,height,x,y,river):
        """Bounded sea levels supersede global fallback when explicitly supplied."""
        wetness=0.
        underwater=False
        if self.seas:
            for sea in self.seas:
                x0,y0,x1,y1=sea['bounds']
                if x0<=x<=x1 and y0<=y<=y1:
                    wetness=max(wetness,1-_smooth((height-sea['level_m'])/5))
                    underwater=underwater or height<=sea['level_m']
        else:
            wetness=1-_smooth((height-self.sea_level_m)/5)
            underwater=height<=self.sea_level_m
        water_distance=None
        if river:
            water_distance=max(0.,river['distance_m']-river['river']['width_m']/2)
            wetness=max(wetness,1-_smooth(water_distance/river['river']['bank_width_m']))
            if river['distance_m']<=river['river']['width_m']/2:
                underwater=underwater or height<=river['surface_m']
        return wetness,underwater,water_distance

    def _height(self, x, y):
        height, biomes, protected, owner, distance = self._base(x,y)
        fade = _smooth(distance/self._regions_by_id[owner]['transition_m'])
        height=self._carve_seas(height,x,y,protected,fade)
        return self._carve(height,protected,self._river_at(x,y),fade)

    def sample_surface(self, x, y):
        """Fast height/material/water sample, without normal or slope queries.

        Use this for texture/mask pixels. ``sample`` additionally computes four
        neighboring height queries and is intended for geometry/scatter normals.
        """
        x, y = _number(x,'x'), _number(y,'y')
        raw, biomes, protected, owner, distance = self._base(x,y)
        water = self._river_at(x,y)
        fade = _smooth(distance/self._regions_by_id[owner]['transition_m'])
        raw=self._carve_seas(raw,x,y,protected,fade)
        height = self._carve(raw,protected,water,fade)
        wetness,underwater,water_distance=self._wetness(height,x,y,water)
        return dict(height_m=height,biome_weights=biomes,wetness=wetness,protected=protected,
                    region_id=owner,core_distance_m=distance,river_distance_m=water_distance,
                    underwater=underwater,
                    physical_scale_verified=False,collision_verified=False)

    def sample(self, x, y):
        """Height, finite normal, slope, normalized biome weights and masks."""
        result = self.sample_surface(x,y)
        step = .1
        dx = (self._height(x+step,y)-self._height(x-step,y))/(2*step)
        dy = (self._height(x,y+step)-self._height(x,y-step))/(2*step)
        length = math.sqrt(dx*dx+dy*dy+1)
        normal = (-dx/length,-dy/length,1/length)
        result.update(normal=normal,slope_deg=math.degrees(math.atan(math.hypot(dx,dy))))
        return result

    def excluded(self, x, y):
        """Protected source cores plus caller-authored no-scatter polygons."""
        point = (_number(x,'x'),_number(y,'y'))
        return (any(_point_in_polygon(point,r['polygon']) for r in self.regions)
                or any(_point_in_polygon(point,p) for p in self.exclusions))


def tile_indices(bounds, *, cells=32, cell_size_m=10, origin=(0,0), max_tiles=256):
    """Explicit bounded tile plan. Never use enormous imported backdrop AABBs."""
    if not isinstance(bounds,(list,tuple)) or len(bounds) != 4:
        raise ValueError('bounds: [minX,minY,maxX,maxY] required')
    x0,y0,x1,y1 = [_number(v,'bounds') for v in bounds]
    if x1 <= x0 or y1 <= y0:
        raise ValueError('bounds: positive extent required')
    cells = _integer(cells,'cells',1,128)
    step = _number(cell_size_m,'cell_size_m',positive=True)
    ox,oy = _xy(origin,'origin')
    maximum = _integer(max_tiles,'max_tiles',1,4096)
    size = cells*step
    ix0,iy0 = math.floor((x0-ox)/size),math.floor((y0-oy)/size)
    ix1,iy1 = math.ceil((x1-ox)/size)-1,math.ceil((y1-oy)/size)-1
    if (ix1-ix0+1)*(iy1-iy0+1) > maximum:
        raise ValueError('terrain bounds exceed explicit tile budget; choose working bounds or coarser tiles')
    return [(ix,iy) for iy in range(iy0,iy1+1) for ix in range(ix0,ix1+1)]


def generate_tile(field, tile_x, tile_y, *, cells=32, cell_size_m=10, origin=(0,0),
                  max_vertices=300000):
    """Plan clipped terrain mesh; all vertices use global/world coordinates.

    ``faces`` are upward triangles. ``uvs``, ``biome_weights``, ``normals`` and
    ``wetness`` match vertices. Tile-edge coordinates/heights/masks are identical
    across adjacent calls with the same settings, even for negative indices.
    Core clipping removes covered areas instead of creating overlapping ground.
    Faces below the existing custom-geometry export guard's area threshold in
    Blender's Float32 coordinates are omitted; original vertex data is retained.
    """
    if not isinstance(field,TerrainField):
        raise ValueError('field: TerrainField required')
    tx,ty = _integer(tile_x,'tile_x',-100000,100000),_integer(tile_y,'tile_y',-100000,100000)
    count = _integer(cells,'cells',1,128)
    step = _number(cell_size_m,'cell_size_m',positive=True)
    ox,oy = _xy(origin,'origin')
    maximum = _integer(max_vertices,'max_vertices',3,2000000)
    minx,miny = ox+tx*count*step,oy+ty*count*step
    bounds = (minx,miny,ox+(tx+1)*count*step,oy+(ty+1)*count*step)
    if any(abs(v) > _MAX_COORD-1 for v in bounds):
        raise ValueError('tile coordinates exceed safe editor extent')
    clips = [tri for r in field.regions if _bounds_overlap(bounds,r['bounds']) for tri in r['triangles']]
    vertices,faces,normals,weights,wetness,uvs = [],[],[],[],[],[]
    float32_vertices = []
    dropped_float32_degenerate_faces = 0
    index = {}

    def vertex(point):
        key = (round(point[0],10),round(point[1],10))
        if key not in index:
            if len(vertices) >= maximum:
                raise ValueError('terrain vertex budget exceeded')
            sample = field.sample(*key)
            index[key] = len(vertices)
            vertices.append((key[0],key[1],sample['height_m']))
            float32_vertices.append(struct.unpack('<3f',struct.pack('<3f',*vertices[-1])))
            normals.append(sample['normal'])
            weights.append(sample['biome_weights'])
            wetness.append(sample['wetness'])
            uvs.append(((key[0]-bounds[0])/(count*step),(key[1]-bounds[1])/(count*step)))
        return index[key]

    for y in range(count):
        for x in range(count):
            # Integer grid indices first, so no cumulative per-tile floating drift.
            gx,gy = tx*count+x,ty*count+y
            a,b,c,d = ((ox+gx*step,oy+gy*step),(ox+(gx+1)*step,oy+gy*step),
                       (ox+(gx+1)*step,oy+(gy+1)*step),(ox+gx*step,oy+(gy+1)*step))
            pieces = [[a,b,c,d]]
            cb = (a[0],a[1],c[0],c[1])
            for clip in clips:
                if not _bounds_overlap(cb,_bounds(clip)):
                    continue
                pieces = [part for piece in pieces for part in _subtract_convex(piece,clip)]
                if not pieces:
                    break
                if len(pieces) > 10000:
                    raise ValueError('core clipping complexity exceeds safe cell budget')
            for piece in pieces:
                for i in range(1,len(piece)-1):
                    if _cross(piece[0],piece[i],piece[i+1]) > _EPS:
                        face = tuple(vertex(p) for p in (piece[0],piece[i],piece[i+1]))
                        # Clipping can leave distinct double-precision points
                        # at the same Float32 position. Keep the source data,
                        # but do not emit a face that Blender will collapse.
                        a,b,c = (float32_vertices[j] for j in face)
                        u = tuple(b[j]-a[j] for j in range(3))
                        v = tuple(c[j]-a[j] for j in range(3))
                        cross = (u[1]*v[2]-u[2]*v[1],u[2]*v[0]-u[0]*v[2],u[0]*v[1]-u[1]*v[0])
                        if math.hypot(*cross) < 1e-10:
                            dropped_float32_degenerate_faces += 1
                        else:
                            faces.append(face)
    return dict(vertices=vertices,faces=faces,normals=normals,biome_weights=weights,
                wetness=wetness,uvs=uvs,tile=[tx,ty],bounds_m=list(bounds),cells=count,
                cell_size_m=step,seed=field.seed,protected_core_ids=[r['id'] for r in field.regions],
                dropped_float32_degenerate_faces=dropped_float32_degenerate_faces,
                status='authored_terrain_preview',physical_scale_verified=False,
                collision_verified=False,navigation_verified=False)


def scatter_tile(field, tile_x, tile_y, *, cells=32, cell_size_m=10, origin=(0,0),
                 seed=0, spacing_m=5, density=1, slope_max_deg=35, biome=None,
                 biome_min_weight=.5, min_height_m=None, max_height_m=None,
                 river_clearance_m=1, allow_underwater=False, max_points=10000):
    """Deterministic candidates with cross-tile minimum spacing and exclusions.

    A local Matérn priority test avoids order-dependent tiling. ``density`` is
    candidate probability, not points per square metre. Exclusions, protected
    cores, slope, biome weight, height and water tests precede spacing rejection.
    ``candidate_id`` stays stable when viewing or independently rebuilding tiles.
    No imported mesh is classified or altered and no action behavior is inferred.
    """
    if not isinstance(field,TerrainField):
        raise ValueError('field: TerrainField required')
    tx,ty = _integer(tile_x,'tile_x',-100000,100000),_integer(tile_y,'tile_y',-100000,100000)
    cells = _integer(cells,'cells',1,128)
    cell = _number(cell_size_m,'cell_size_m',positive=True)
    ox,oy = _xy(origin,'origin')
    seed = _integer(seed,'seed',-2**63,2**63-1)
    spacing = _number(spacing_m,'spacing_m',positive=True)
    density = _number(density,'density',nonnegative=True)
    slope = _number(slope_max_deg,'slope_max_deg',nonnegative=True)
    weight = _number(biome_min_weight,'biome_min_weight',nonnegative=True)
    clearance = _number(river_clearance_m,'river_clearance_m',nonnegative=True)
    maximum = _integer(max_points,'max_points',1,1000000)
    if density > 1 or weight > 1 or slope > 90 or not isinstance(allow_underwater,bool):
        raise ValueError('density/biome weight must be <=1, slope <=90, allow_underwater boolean')
    if biome is not None and biome not in BIOMES:
        raise ValueError('scatter biome: unsupported biome')
    low = None if min_height_m is None else _number(min_height_m,'min_height_m')
    high = None if max_height_m is None else _number(max_height_m,'max_height_m')
    if low is not None and high is not None and high < low:
        raise ValueError('scatter min_height_m exceeds max_height_m')
    bounds = (ox+tx*cells*cell,oy+ty*cells*cell,ox+(tx+1)*cells*cell,oy+(ty+1)*cells*cell)
    if any(abs(v) > _MAX_COORD-1 for v in bounds):
        raise ValueError('tile coordinates exceed safe editor extent')
    grid = spacing/math.sqrt(2)
    ix0,iy0 = math.floor(bounds[0]/grid),math.floor(bounds[1]/grid)
    ix1,iy1 = math.ceil(bounds[2]/grid),math.ceil(bounds[3]/grid)
    if (ix1-ix0+6)*(iy1-iy0+6) > 1000000:
        raise ValueError('scatter candidate budget exceeded; increase spacing or reduce working bounds')
    cache = {}

    def candidate(ix, iy):
        key = (ix,iy)
        if key in cache:
            return cache[key]
        result = None
        if _random(seed,ix,iy,1) < density:
            x = (ix+_random(seed,ix,iy,2))*grid
            y = (iy+_random(seed,ix,iy,3))*grid
            if not field.excluded(x,y):
                s = field.sample(x,y)
                z = s['height_m']
                if (s['slope_deg'] <= slope and (low is None or z >= low)
                        and (high is None or z <= high)
                        and (biome is None or s['biome_weights'][biome] >= weight)
                        and (allow_underwater or not s['underwater'])
                        and (s['river_distance_m'] is None or s['river_distance_m'] >= clearance)):
                    result = dict(candidate_id=f'{seed}:{ix}:{iy}',position_m=(x,y,z),
                                  normal=s['normal'],biome_weights=s['biome_weights'],
                                  priority=_random(seed,ix,iy,4),seed=seed)
        cache[key] = result
        return result

    points = []
    for iy in range(iy0,iy1):
        for ix in range(ix0,ix1):
            value = candidate(ix,iy)
            if value is None:
                continue
            x,y,z = value['position_m']
            if not bounds[0] <= x < bounds[2] or not bounds[1] <= y < bounds[3]:
                continue
            accepted = True
            for ny in range(iy-2,iy+3):
                for nx in range(ix-2,ix+3):
                    if nx == ix and ny == iy:
                        continue
                    other = candidate(nx,ny)
                    if other is not None and (other['priority'],nx,ny) < (value['priority'],ix,iy):
                        if math.dist((x,y),other['position_m'][:2]) < spacing:
                            accepted = False
                            break
                if not accepted:
                    break
            if accepted:
                if len(points) >= maximum:
                    raise ValueError('scatter point budget exceeded; increase spacing or lower density')
                points.append({k:v for k,v in value.items() if k != 'priority'})
    return points


def river_mesh(source):
    """Continuous editable river ribbon, without water physics or core carving."""
    river = validate_river(source)
    points,vertices,faces = river['points'],[],[]
    half = river['width_m']/2
    for i,p in enumerate(points):
        directions = []
        for a,b in ((points[max(0,i-1)],p),(p,points[min(len(points)-1,i+1)])):
            length = math.dist(a[:2],b[:2])
            if length > _EPS:
                directions.append((-(b[1]-a[1])/length,(b[0]-a[0])/length))
        nx,ny = sum(n[0] for n in directions),sum(n[1] for n in directions)
        length = math.hypot(nx,ny)
        if length <= _EPS:
            raise ValueError('river centerline cannot turn back 180 degrees')
        nx,ny = nx/length,ny/length
        dot = max(.5,nx*directions[-1][0]+ny*directions[-1][1])
        width = half/dot
        vertices.extend(((p[0]-nx*width,p[1]-ny*width,p[2]),
                         (p[0]+nx*width,p[1]+ny*width,p[2])))
        if i:
            faces.append((2*i-2,2*i,2*i+1,2*i-1))
    return dict(id=river['id'],vertices=vertices,faces=faces,kind='river_surface',
                runtime_integrated=False,collision_verified=False)


def sea_mesh(bounds, level_m):
    """One explicit sea surface rectangle; not a gameplay water volume."""
    if not isinstance(bounds,(list,tuple)) or len(bounds) != 4:
        raise ValueError('sea bounds: [minX,minY,maxX,maxY] required')
    x0,y0,x1,y1 = [_number(v,'sea.bounds') for v in bounds]
    z = _number(level_m,'sea.level_m')
    if x1 <= x0 or y1 <= y0:
        raise ValueError('sea bounds must have positive extent')
    return dict(vertices=[(x0,y0,z),(x1,y0,z),(x1,y1,z),(x0,y1,z)],faces=[(0,1,2,3)],
                kind='sea_surface',runtime_integrated=False,collision_verified=False)
