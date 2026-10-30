"""Explicit collision interchange from existing generated terrain plans.

No visual GLB is used. Native collision and navigation remain unverified. Cells
intersecting native cores are conservatively omitted; their border gaps require
the later native trace seam gate and are never called collision-ready here.
"""
import copy
import hashlib
import math
from pathlib import Path
import struct

try:
    from . import runtime_export as runtime, terrain_core as core
except ImportError:
    import runtime_export as runtime
    import terrain_core as core


def _world_polygon(record):
    polygon = core.validate_polygon(record['polygon_local_m'], 'collision_core.polygon_local_m')
    yaw = runtime._number(record['yaw_deg'], 'collision_core.yaw_deg')
    if yaw not in (0, 90, 180, 270):
        raise ValueError('collision_core: quarter-turn yaw required')
    x, y, _ = runtime._vector(record['position_m'], 'collision_core.position_m')
    radians = math.radians(yaw)
    c, s = round(math.cos(radians)), round(math.sin(radians))
    return [[x+c*px-s*py, y+s*px+c*py] for px, py in polygon]


def _height(point, triangle):
    a, b, c = triangle
    den = (b[1]-c[1])*(a[0]-c[0])+(c[0]-b[0])*(a[1]-c[1])
    if abs(den) < 1e-12:
        return None
    u = ((b[1]-c[1])*(point[0]-c[0])+(c[0]-b[0])*(point[1]-c[1]))/den
    v = ((c[1]-a[1])*(point[0]-c[0])+(a[0]-c[0])*(point[1]-c[1]))/den
    if min(u, v, 1-u-v) < -1e-7:
        return None
    return u*a[2]+v*b[2]+(1-u-v)*c[2]


def _mesh_sampler(plan):
    vertices = plan['vertices']
    points = {(round(v[0], 8), round(v[1], 8)): v[2] for v in vertices}
    minx, miny, _, _ = plan['bounds_m']
    step = plan['cell_size_m']
    bins = {}
    for face in plan['faces']:
        tri = [vertices[i] for i in face]
        ix0 = max(0, math.floor((min(v[0] for v in tri)-minx)/step-1e-7))
        iy0 = max(0, math.floor((min(v[1] for v in tri)-miny)/step-1e-7))
        ix1 = min(31, math.floor((max(v[0] for v in tri)-minx)/step+1e-7))
        iy1 = min(31, math.floor((max(v[1] for v in tri)-miny)/step+1e-7))
        for iy in range(iy0, iy1+1):
            for ix in range(ix0, ix1+1):
                bins.setdefault((ix, iy), []).append(tri)
    def sample(x, y):
        value = points.get((round(x, 8), round(y, 8)))
        if value is not None:
            return value
        ix, iy = min(31, max(0, int((x-minx)/step))), min(31, max(0, int((y-miny)/step)))
        for triangle in bins.get((ix, iy), []):
            value = _height((x, y), triangle)
            if value is not None:
                return value
        return None
    return sample


def _triangle_area2(a, b, c):
    return math.hypot((b[1]-a[1])*(c[2]-a[2])-(b[2]-a[2])*(c[1]-a[1]),
                      (b[2]-a[2])*(c[0]-a[0])-(b[0]-a[0])*(c[2]-a[2]),
                      (b[0]-a[0])*(c[1]-a[1])-(b[1]-a[1])*(c[0]-a[0]))


def build_product(world_id, plans, *, sample_surface, collision_cores,
                  water_volumes=(), connector_meshes=()):
    """Return (document, relative-path->bytes) for 32-cell generated mesh plans.

    sample_surface is the SAME field/decorator that generated the meshes. Its
    heights only fill native hole samples; unmasked heights come from the actual
    triangulated plans. Surface labels are explicitly authored field biomes.
    Connector inputs are owned world-space meshes, never imported visual GLBs.
    """
    if not plans or len(plans) > 4096:
        raise ValueError('terrain collision requires 1..4096 generated plans')
    cores = copy.deepcopy(list(collision_cores))
    polygons = [(_world_polygon(r), r['position_m'][2]+r['z_range_m'][0],
                 r['position_m'][2]+r['z_range_m'][1]) for r in cores]
    result = runtime._header('wz09_terrain_collision', world_id)
    result.update(source_status='editor_geometry_unverified', collision_verified=False,
                  grid=dict(columns=33, rows=33, encoding='f32_le', row_axis='+Y', column_axis='+X', heights='absolute_world_z_m'),
                  surface_table=list(runtime.SURFACES), hole_policy='conservative_cell_core_intersection',
                  native_boundary_verified=False, collision_cores=cores, tiles=[],
                  water_volumes=copy.deepcopy(list(water_volumes)), connectors=[])
    payloads, edge_samples = {}, {}
    def store(payload):
        ref = runtime.resource(payload, folder='collision', suffix='.bin')
        payloads[ref['path']] = payload
        return ref
    for plan in plans:
        if plan.get('cells') != 32:
            raise ValueError('terrain collision requires existing 32x32-cell plans (33x33 samples)')
        x0, y0, x1, y1 = plan['bounds_m']
        if max(abs(v) for v in plan['bounds_m']) > 3200:
            raise ValueError('terrain collision bounds exceed +/-3200m')
        step = runtime._number(plan['cell_size_m'], 'cell_size_m', .001)
        if abs(x1-x0-32*step) > 1e-6 or abs(y1-y0-32*step) > 1e-6:
            raise ValueError('terrain collision plan bounds differ from sample lattice')
        sampler = _mesh_sampler(plan)
        holes, surfaces = [], []
        for row in range(32):
            for col in range(32):
                x, y = x0+col*step, y0+row*step
                square = [[x, y], [x+step, y], [x+step, y+step], [x, y+step]]
                center = (x+step/2, y+step/2)
                surface = sample_surface(*center)
                actual_height = sampler(*center)
                ground_z = surface['height_m'] if actual_height is None else actual_height
                # A roof core is a bounded native volume, not a hole through
                # the entire world. Preserve independently authored ground
                # below elevated Highrise and other raised native structures.
                intersects = any(z0-.1 <= ground_z <= z1+.1 and core._overlap_area(square, polygon) > 1e-8
                                 for polygon, z0, z1 in polygons)
                # Clipped source geometry can also leave explicit unsupported
                # cells; omit those rather than invent a collision surface.
                missing = actual_height is None
                holes.append(int(intersects or missing))
                material = surface.get('material')
                if material is None:
                    weights = surface.get('biome_weights', {})
                    if not weights:
                        raise ValueError('terrain collision: explicit authored surface required')
                    material = max(weights, key=lambda name: weights[name])
                material = 'dirt' if material == 'soil' else material
                if material not in runtime.SURFACES:
                    raise ValueError('terrain collision: unsupported authored surface '+str(material))
                surfaces.append(runtime.SURFACES.index(material))
        heights = []
        for row in range(33):
            for col in range(33):
                x, y = x0+col*step, y0+row*step
                height = sampler(x, y)
                if height is None:
                    adjoining = [holes[r*32+c] for r in (row-1, row) for c in (col-1, col) if 0 <= r < 32 and 0 <= c < 32]
                    if not adjoining or not all(adjoining):
                        raise ValueError('terrain collision: missing mesh sample beside unmasked cell')
                    height = sample_surface(x, y)['height_m']
                height = runtime._number(height, 'terrain.height_m', -3200, 3200)
                encoded = struct.pack('<f', height)
                if row in (0, 32) or col in (0, 32):
                    key = (round(x, 7), round(y, 7))
                    if key in edge_samples and edge_samples[key] != encoded:
                        raise ValueError('terrain collision: adjacent tile edge heights differ at '+str(key))
                    edge_samples[key] = encoded
                heights.append(encoded)
        tile = list(plan['tile'])
        result['tiles'].append(dict(id=plan.get('id') or runtime.identity(world_id, 'collision-tile:'+str(tile[0])+':'+str(tile[1])),
                                    tile=tile, origin_m=[x0, y0, 0.], cell_size_m=step,
                                    height_samples=store(b''.join(heights)),
                                    hole_mask=store(bytes(holes)), surface_mask=store(bytes(surfaces))))
    for mesh in connector_meshes:
        uid = runtime._identifier(mesh['id'], 'connector.id')
        vertices, faces = mesh['vertices'], mesh['faces']
        if not 3 <= len(vertices) <= 65535 or not faces:
            raise ValueError('connector: explicit u16-compatible mesh required; split large parts')
        positions = [runtime._vector(v, 'connector.vertex') for v in vertices]
        if any(abs(c) > 3200 for p in positions for c in p):
            raise ValueError('connector vertices exceed +/-3200m')
        # Validate the exact coordinate bytes the consumer will read. A valid
        # double-precision triangle can collapse near a large world origin when
        # encoded as Float32. Never remove an authored face to hide that loss.
        vertex_bytes = b''.join(struct.pack('<fff', *p) for p in positions)
        packed_positions = list(struct.iter_unpack('<fff', vertex_bytes))
        indices = []
        for face_index, face in enumerate(faces):
            if not isinstance(face, (tuple, list)) or len(face) != 3 or any(type(i) is not int or not 0 <= i < len(vertices) for i in face):
                raise ValueError('connector: triangle u16 indices required')
            if len(set(face)) != 3:
                raise ValueError('connector: degenerate triangle indices')
            a, b, c = [positions[i] for i in face]
            if _triangle_area2(a, b, c) <= 1e-10:
                raise ValueError('connector: zero-area triangle')
            if _triangle_area2(*(packed_positions[i] for i in face)) <= 1e-10:
                raise ValueError(f'connector {uid}: triangle {face_index} collapses after f32 quantization; source unchanged')
            indices.extend(face)
        if mesh.get('winding') != 'ccw':
            raise ValueError('connector: explicit CCW winding required')
        surfaces = mesh['surfaces']
        if len(surfaces) != len(faces) or any(s not in runtime.SURFACES for s in surfaces):
            raise ValueError('connector: supported surface per triangle required')
        result['connectors'].append(dict(id=uid, vertices=store(vertex_bytes),
            indices=store(struct.pack('<'+'H'*len(indices), *indices)),
            surface_mask=store(bytes(runtime.SURFACES.index(s) for s in surfaces)), vertex_count=len(positions), triangle_count=len(faces),
            vertex_encoding='f32_le_xyz', index_encoding='u16_le', winding='ccw', collision_verified=False))
    runtime.validate_document(result)
    return result, payloads


def validate_document(document, base_dir=None):
    """Semantic checks after runtime_schema's exact structure gate."""
    seen, tiles = {document['id']}, set()
    for record in document['collision_cores']:
        runtime._identifier(record['id'], 'collision_core.id')
        if record['id'] in seen:
            raise ValueError('terrain collision: duplicate core ID')
        seen.add(record['id'])
        polygon = _world_polygon(record)
        if record['z_range_m'][1] <= record['z_range_m'][0]:
            raise ValueError('collision_core: nonempty local z range required')
        if len(record['boundary_heights_m']) != len(polygon):
            raise ValueError('collision_core: one explicit height per boundary vertex required')
        # Convex only: native trace clipping has no concave decomposition contract.
        if any(core._cross(polygon[i-1], polygon[i], polygon[(i+1) % len(polygon)]) < -1e-8 for i in range(len(polygon))):
            raise ValueError('collision_core: convex polygon required')
    edges = {}
    for tile in document['tiles']:
        key = tuple(tile['tile'])
        if tile['id'] in seen or key in tiles:
            raise ValueError('terrain collision: duplicate tile ID/index')
        seen.add(tile['id'])
        tiles.add(key)
        for name, size in (('height_samples', 33*33*4), ('hole_mask', 32*32), ('surface_mask', 32*32)):
            runtime._check_resource(tile[name], base_dir)
            if tile[name]['byte_length'] != size:
                raise ValueError('terrain collision: wrong '+name+' binary size')
        if base_dir is not None:
            data = (Path(base_dir)/tile['height_samples']['path']).read_bytes()
            heights = struct.unpack('<1089f', data)
            if any(not math.isfinite(h) or abs(h) > 3200 for h in heights):
                raise ValueError('terrain collision: nonfinite/out-of-bounds height')
            for row in range(33):
                for col in range(33):
                    if row not in (0, 32) and col not in (0, 32):
                        continue
                    x = round(tile['origin_m'][0]+col*tile['cell_size_m'], 7)
                    y = round(tile['origin_m'][1]+row*tile['cell_size_m'], 7)
                    chunk = data[(row*33+col)*4:(row*33+col+1)*4]
                    if (x, y) in edges and edges[(x, y)] != chunk:
                        raise ValueError('terrain collision: shared edge not bit-identical')
                    edges[(x, y)] = chunk
            for name, maximum in (('hole_mask', 1), ('surface_mask', len(runtime.SURFACES)-1)):
                if any(v > maximum for v in (Path(base_dir)/tile[name]['path']).read_bytes()):
                    raise ValueError('terrain collision: invalid '+name+' value')
    for water in document['water_volumes']:
        if water['id'] in seen:
            raise ValueError('terrain collision: duplicate water ID')
        seen.add(water['id'])
        core.validate_polygon(water['polygon_m'], 'water.polygon_m')
        if water['depth_m'] > water['walkable_depth_m'] and water['out_of_bounds_id'] is None:
            raise ValueError('deep water requires an explicit out_of_bounds reference')
    for connector in document['connectors']:
        if connector['id'] in seen:
            raise ValueError('terrain collision: duplicate connector ID')
        seen.add(connector['id'])
        for name, size in (('vertices', connector['vertex_count']*12), ('indices', connector['triangle_count']*6),
                           ('surface_mask', connector['triangle_count'])):
            runtime._check_resource(connector[name], base_dir)
            if connector[name]['byte_length'] != size:
                raise ValueError('connector: binary size differs from declared counts')
        if base_dir is not None:
            positions = list(struct.iter_unpack('<fff', (Path(base_dir)/connector['vertices']['path']).read_bytes()))
            indices = struct.unpack('<'+'H'*(connector['triangle_count']*3), (Path(base_dir)/connector['indices']['path']).read_bytes())
            if any(not math.isfinite(v) or abs(v) > 3200 for p in positions for v in p) or max(indices) >= connector['vertex_count']:
                raise ValueError('connector: invalid world vertex/index')
            for index in range(0, len(indices), 3):
                face = indices[index:index+3]
                if len(set(face)) != 3 or _triangle_area2(*(positions[i] for i in face)) <= 1e-10:
                    raise ValueError('connector: degenerate published f32 triangle')
    return document
