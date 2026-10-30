"""Blender glue: write collision extensions (schema v2) next to a runtime export.

Kleiner Huen 2026-10-19. Reads the v1 runtime world + terrain_collision of an
export folder, gathers per-city native-collision flags, authored collision
proxies and open-mesh warnings from the scene, samples 8 m detail heights by
raycasting the generated terrain meshes, and publishes:
  products/<sha>.json            kind wz09_collision_extensions (schema 2)
  collision_ext/<sha>.bin        hole masks, detail heights, object meshes
  wz09_runtime_extensions.json   manifest (kind wz09_runtime_extensions)
The v1 files are never modified.
"""
import json
from pathlib import Path

try:
    from . import collision_extensions as ext
except ImportError:
    import collision_extensions as ext

DEFAULT_NATIVE = {'mp_invasion'}


def _runtime_world(export_dir):
    files = sorted(Path(export_dir).glob('wz09_runtime_world-*.json'))
    if not files:
        raise ValueError('No wz09_runtime_world-*.json in export')
    return files[0], json.loads(files[0].read_text(encoding='utf-8'))


def native_map_ids(scene, maps):
    """City collection flag > map root flag > default (Invasion)."""
    import bpy
    flags = {}
    for c in bpy.data.collections:
        if c.get('wz09_city') and 'wz09_native_collision_loaded' in c:
            flags[c['wz09_source_map']] = bool(c['wz09_native_collision_loaded'])
    for o in bpy.data.objects:
        if o.type == 'EMPTY' and o.get('wz09_source_map') and 'wz09_native_collision_loaded' in o:
            flags.setdefault(o['wz09_source_map'], bool(o['wz09_native_collision_loaded']))
    out = []
    for m in maps:
        if flags.get(m['source_map'], m['source_map'] in DEFAULT_NATIVE):
            out.append(m['id'])
    return out


def terrain_sampler(scene):
    import bpy
    from mathutils import Vector
    from mathutils.bvhtree import BVHTree
    trees = []
    for o in scene.objects:
        if o.type == 'MESH' and o.get('wz09_terrain_tile_json') and not o.get('wz09_archived_generation'):
            me = o.data
            mw = o.matrix_world
            verts = [mw @ v.co for v in me.vertices]
            polys = [tuple(p.vertices) for p in me.polygons]
            if polys:
                xs = [v.x for v in verts]; ys = [v.y for v in verts]
                trees.append(((min(xs), min(ys), max(xs), max(ys)), BVHTree.FromPolygons(verts, polys)))
    down = Vector((0, 0, -1))
    def sample(x, y):
        best = None
        for (x0, y0, x1, y1), tree in trees:
            if x0 <= x <= x1 and y0 <= y <= y1:
                hit = tree.ray_cast(Vector((x, y, 5000.0)), down, 20000.0)
                if hit[0] is not None and (best is None or hit[0].z > best):
                    best = hit[0].z
        return best
    return sample, len(trees)


def gather_objects(scene, maps):
    import bpy
    by_name = {m['source_map']: m['id'] for m in maps}
    objs, warnings = [], []
    for o in scene.objects:
        if o.get('wz09_open_edges'):
            warnings.append((o.name, o['wz09_open_edges']))
        if o.type != 'MESH' or not o.get('wz09_collision_proxy'):
            continue
        me = o.data; mw = o.matrix_world
        me.calc_loop_triangles()
        root = bpy.data.objects.get(o.get('wz09_collision_map', ''))
        objs.append(dict(name=o.get('wz09_collision_of', o.name), kind=o.get('wz09_collision_kind', 'box'),
                         map_instance_id=by_name.get(root.get('wz09_source_map')) if root else None,
                         vertices=[list(mw @ v.co) for v in me.vertices],
                         triangles=[list(t.vertices) for t in me.loop_triangles]))
    return objs, warnings


def publish(scene, export_dir, *, native_ids=None, use_mesh_sampler=True):
    export_dir = Path(export_dir)
    world_path, world = _runtime_world(export_dir)
    tref = world['products']['terrain_collision']
    terrain = json.loads((export_dir / tref['path']).read_text(encoding='utf-8'))
    royale = json.loads((export_dir / world['products']['royale_layout']['path']).read_text(encoding='utf-8'))
    xs = [p[0] for p in royale['play_bounds']]; ys = [p[1] for p in royale['play_bounds']]
    bounds = [min(xs), min(ys), max(xs), max(ys)]
    if native_ids is None:
        native_ids = native_map_ids(scene, world['maps'])
    sampler, trees = (terrain_sampler(scene) if use_mesh_sampler and scene is not None else (None, 0))
    objs, warnings = gather_objects(scene, world['maps']) if scene is not None else ([], [])
    doc, payloads = ext.build(terrain, lambda p: (export_dir / p).read_bytes(), maps=world['maps'],
                              native_map_ids=native_ids, play_bounds=bounds, world_id=world['world_id'],
                              detail_sampler=sampler, object_collision=objs, open_mesh_warnings=warnings,
                              base_ref=dict(tref))
    for rel, data in payloads.items():
        p = export_dir / rel; p.parent.mkdir(parents=True, exist_ok=True); p.write_bytes(data)
    raw = ext.encode(doc)
    pref = ext.resource(raw, folder='products', suffix='.json')
    (export_dir / pref['path']).write_bytes(raw)
    manifest = dict(kind='wz09_runtime_extensions', version=1, schema_version=ext.SCHEMA_VERSION,
                    world_id=world['world_id'], runtime_world=world_path.name,
                    products=dict(collision_extensions=pref),
                    docs='docs/COLLISION_EXTENSIONS_V2.md',
                    summary=dict(native_collision_maps=[m['source_map'] for m in world['maps'] if m['id'] in native_ids],
                                 filled_hole_cells=doc['hole_fill']['filled_cells'],
                                 remaining_hole_cells=doc['hole_fill']['remaining_hole_cells'],
                                 detail_patches=len(doc['detail_patches']),
                                 detail_samples_from_mesh=sum(p['sampled_from_mesh'] for p in doc['detail_patches']),
                                 detail_samples_total=sum(p['sampled_total'] for p in doc['detail_patches']),
                                 terrain_meshes_sampled=trees, boundary_walls=len(doc['boundary_walls']),
                                 catch_volumes=len(doc['catch_volumes']), object_collision=len(doc['object_collision']),
                                 open_mesh_warnings=len(doc['open_mesh_warnings'])))
    (export_dir / 'wz09_runtime_extensions.json').write_text(json.dumps(manifest, indent=1) + '\n', encoding='utf-8')
    return manifest
