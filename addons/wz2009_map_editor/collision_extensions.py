"""WZ2009 collision extensions (schema v2), pure Python, no bpy.

Added by Kleiner Huen 2026-10-19. Builds the product kind
'wz09_collision_extensions' on top of an existing v1 'wz09_terrain_collision'
product without changing it:
  * effective hole masks: ground holes under cities whose original MW2
    collision is NOT loaded in the game are filled (base heights exist there)
  * detail height patches (default 8 m) over play areas (city cores + margin)
  * boundary walls along the play edge and catch volumes below/outside
  * per-object collision meshes (box / convex hull) authored in the editor
See docs/COLLISION_EXTENSIONS_V2.md.
"""
import hashlib
import json
import math
import struct
import uuid

KIND = 'wz09_collision_extensions'
SCHEMA_VERSION = 2
TILE_CELLS = 32
NAMESPACE = uuid.UUID('6f1c1f0e-8a59-4c8e-9d2e-2b5f0c7a9e11')


def resource(payload, folder='collision_ext', suffix='.bin'):
    digest = hashlib.sha256(payload).hexdigest()
    return dict(path=folder + '/' + digest + suffix, sha256=digest, byte_length=len(payload))


def stable_id(*parts):
    return str(uuid.uuid5(NAMESPACE, '|'.join(str(p) for p in parts)))


def core_world_polygon(core):
    x, y = core['position_m'][0], core['position_m'][1]
    r = math.radians(core.get('yaw_deg', 0))
    c, s = math.cos(r), math.sin(r)
    return [[x + c * px - s * py, y + s * px + c * py] for px, py in core['polygon_local_m']]


def point_in_polygon(x, y, poly):
    inside = False
    j = len(poly) - 1
    for i in range(len(poly)):
        xi, yi = poly[i]; xj, yj = poly[j]
        if (yi > y) != (yj > y) and x < (xj - xi) * (y - yi) / ((yj - yi) or 1e-12) + xi:
            inside = not inside
        j = i
    return inside


def _bbox(poly):
    xs = [p[0] for p in poly]; ys = [p[1] for p in poly]
    return min(xs), min(ys), max(xs), max(ys)


class BaseGrid:
    """Bilinear sampler over v1 33x33 tiles (absolute world z)."""

    def __init__(self, terrain, read_bytes):
        self.tiles = {}
        self.read = read_bytes
        for t in terrain['tiles']:
            n = terrain['grid']['columns']
            raw = read_bytes(t['height_samples']['path'])
            heights = struct.unpack('<%df' % (n * n), raw)
            self.tiles[tuple(t['tile'])] = (t, heights, n)
        first = terrain['tiles'][0]
        self.cell = first['cell_size_m']
        self.span = self.cell * TILE_CELLS
        self.min_z = min(min(h) for _, h, _ in self.tiles.values())
        self.max_z = max(max(h) for _, h, _ in self.tiles.values())

    def sample(self, x, y):
        tx, ty = math.floor(x / self.span), math.floor(y / self.span)
        entry = self.tiles.get((tx, ty))
        if entry is None:
            return None
        t, h, n = entry
        ox, oy = t['origin_m'][0], t['origin_m'][1]
        fx = min(max((x - ox) / self.cell, 0), n - 1.000001)
        fy = min(max((y - oy) / self.cell, 0), n - 1.000001)
        c, r = int(fx), int(fy); dx, dy = fx - c, fy - r
        z00 = h[r * n + c]; z10 = h[r * n + c + 1]; z01 = h[(r + 1) * n + c]; z11 = h[(r + 1) * n + c + 1]
        return (z00 * (1 - dx) + z10 * dx) * (1 - dy) + (z01 * (1 - dx) + z11 * dx) * dy


def build(terrain, read_bytes, *, maps, native_map_ids, play_bounds, world_id='wz09_karvorsk',
          detail_cell_m=8.0, detail_margin_m=64.0, detail_sampler=None,
          wall_thickness_m=16.0, wall_top_m=1500.0, catch_depth_m=50.0,
          object_collision=(), open_mesh_warnings=(), base_ref=None):
    """Return (document, {relative_path: bytes}).

    maps: [{id, source_map}] ; native_map_ids: map instance ids whose original
    collision the game loads (their holes stay open). detail_sampler(x, y) ->
    z or None (e.g. Blender BVH raycast); falls back to bilinear base grid.
    object_collision: [{name, map_instance_id, kind, vertices:[[x,y,z]..], triangles:[[i,j,k]..]}]
    """
    native = set(native_map_ids)
    known = {m['id'] for m in maps}
    unknown = native - known
    if unknown:
        raise ValueError('native_map_ids contains unknown map ids: %s' % sorted(unknown))
    grid = BaseGrid(terrain, read_bytes)
    payloads = {}

    def store(payload, suffix='.bin'):
        ref = resource(payload, suffix=suffix)
        payloads[ref['path']] = payload
        return ref

    cores = [(c, core_world_polygon(c)) for c in terrain.get('collision_cores', [])]
    fill_cores = [(c, p, _bbox(p)) for c, p in cores if c['map_instance_id'] not in native]
    # 1) effective hole masks
    tiles_out, filled_total, remaining = [], 0, 0
    for t in terrain['tiles']:
        mask = bytearray(read_bytes(t['hole_mask']['path']))
        ox, oy = t['origin_m'][0], t['origin_m'][1]; step = t['cell_size_m']
        filled = 0
        for idx, v in enumerate(mask):
            if not v:
                continue
            row, col = divmod(idx, TILE_CELLS)
            cx, cy = ox + (col + .5) * step, oy + (row + .5) * step
            for _, poly, (x0, y0, x1, y1) in fill_cores:
                if x0 - step <= cx <= x1 + step and y0 - step <= cy <= y1 + step:
                    # cell center inside, or any cell corner inside the core
                    pts = ((cx, cy), (cx - step / 2, cy - step / 2), (cx + step / 2, cy - step / 2),
                           (cx + step / 2, cy + step / 2), (cx - step / 2, cy + step / 2))
                    if any(point_in_polygon(px, py, poly) for px, py in pts):
                        mask[idx] = 0; filled += 1
                        break
        remaining += sum(1 for v in mask if v)
        filled_total += filled
        tiles_out.append(dict(tile=list(t['tile']), base_tile_id=t['id'], filled_cells=filled,
                              hole_mask=store(bytes(mask))))
    # 2) detail patches
    patches = []
    lo_x, lo_y, hi_x, hi_y = play_bounds
    for c, poly in cores:
        x0, y0, x1, y1 = _bbox(poly)
        x0 = max(lo_x, math.floor((x0 - detail_margin_m) / detail_cell_m) * detail_cell_m)
        y0 = max(lo_y, math.floor((y0 - detail_margin_m) / detail_cell_m) * detail_cell_m)
        x1 = min(hi_x, math.ceil((x1 + detail_margin_m) / detail_cell_m) * detail_cell_m)
        y1 = min(hi_y, math.ceil((y1 + detail_margin_m) / detail_cell_m) * detail_cell_m)
        cols = int(round((x1 - x0) / detail_cell_m)) + 1
        rows = int(round((y1 - y0) / detail_cell_m)) + 1
        heights, measured = [], 0
        for r in range(rows):
            for k in range(cols):
                x, y = x0 + k * detail_cell_m, y0 + r * detail_cell_m
                z = detail_sampler(x, y) if detail_sampler else None
                if z is None:
                    z = grid.sample(x, y)
                    if z is None:
                        z = grid.min_z
                else:
                    measured += 1
                heights.append(float(z))
        raw = struct.pack('<%df' % len(heights), *heights)
        patches.append(dict(id=stable_id('detail', c['id']), map_instance_id=c['map_instance_id'],
                            native_collision_loaded=c['map_instance_id'] in native,
                            origin_m=[x0, y0], cell_size_m=detail_cell_m, columns=cols, rows=rows,
                            encoding='f32_le', row_axis='+Y', column_axis='+X', heights='absolute_world_z_m',
                            sampled_from_mesh=measured, sampled_total=len(heights), height_samples=store(raw)))
    # 3) walls + catch volumes (axis aligned boxes, world meters)
    bottom = grid.min_z - catch_depth_m
    T = wall_thickness_m
    walls = []
    for side, mn, mx in (('west', [lo_x - T, lo_y - T, bottom], [lo_x, hi_y + T, wall_top_m]),
                         ('east', [hi_x, lo_y - T, bottom], [hi_x + T, hi_y + T, wall_top_m]),
                         ('south', [lo_x, lo_y - T, bottom], [hi_x, lo_y, wall_top_m]),
                         ('north', [lo_x, hi_y, bottom], [hi_x, hi_y + T, wall_top_m])):
        walls.append(dict(id=stable_id('wall', side), side=side, solid=True, blocks=['player', 'bullet_optional'],
                          box_min_m=mn, box_max_m=mx))
    far = 4000.0
    catch = [dict(id=stable_id('catch', 'below'), kind='below_world', box_min_m=[lo_x - far, lo_y - far, bottom - 2000.0],
                  box_max_m=[hi_x + far, hi_y + far, bottom], action='teleport_to_safe_ground',
                  safe_ground='nearest_terrain_collision_sample')]
    for side, mn, mx in (('west', [lo_x - far, lo_y - far, bottom - 2000], [lo_x - T, hi_y + far, wall_top_m + 2000]),
                         ('east', [hi_x + T, lo_y - far, bottom - 2000], [hi_x + far, hi_y + far, wall_top_m + 2000]),
                         ('south', [lo_x - T, lo_y - far, bottom - 2000], [hi_x + T, lo_y - T, wall_top_m + 2000]),
                         ('north', [lo_x - T, hi_y + T, bottom - 2000], [hi_x + T, hi_y + far, wall_top_m + 2000])):
        catch.append(dict(id=stable_id('catch', side), kind='outside_edge', box_min_m=mn, box_max_m=mx,
                          action='teleport_to_safe_ground', safe_ground='nearest_point_inside_play_bounds'))
    # 4) object collision
    objects = []
    for o in object_collision:
        verts = [float(v) for p in o['vertices'] for v in p]
        tris = [int(i) for tri in o['triangles'] for i in tri]
        if not tris or max(tris) >= len(o['vertices']):
            raise ValueError('object collision %s: bad triangles' % o.get('name'))
        objects.append(dict(id=stable_id('obj', o['name']), object_name=o['name'],
                            map_instance_id=o.get('map_instance_id'), kind=o['kind'], solid=True,
                            vertex_count=len(o['vertices']), triangle_count=len(tris) // 3,
                            vertex_encoding='f32_le_xyz', index_encoding='u32_le', winding='ccw',
                            vertices=store(struct.pack('<%df' % len(verts), *verts)),
                            indices=store(struct.pack('<%dI' % len(tris), *tris))))
    doc = dict(kind=KIND, version=1, schema_version=SCHEMA_VERSION, world_id=world_id,
               id=stable_id(KIND, world_id, json.dumps(sorted(native))),
               units='meters', coordinate_system='world_z_up',
               base_terrain_collision=base_ref,
               native_collision_map_ids=sorted(native),
               play_bounds_m=list(play_bounds),
               hole_fill=dict(policy='fill_cores_without_native_collision', filled_cells=filled_total,
                              remaining_hole_cells=remaining, tiles=tiles_out),
               detail_patches=patches, boundary_walls=walls, catch_volumes=catch,
               object_collision=objects,
               open_mesh_warnings=[dict(object_name=n, non_manifold_edges=int(e)) for n, e in open_mesh_warnings],
               collision_verified=False, runtime_integrated=False)
    return doc, payloads


def encode(doc):
    return (json.dumps(doc, indent=1, sort_keys=True, allow_nan=False) + '\n').encode()
