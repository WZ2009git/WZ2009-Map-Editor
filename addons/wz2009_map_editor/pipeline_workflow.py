"""Project-local adapters for the existing offline exporter and Recast probe.

No gameplay command, build, dependency download or native-collision promotion is
provided. The installed add-on always receives an explicit editor project root.
"""
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import shutil
import subprocess
import uuid

try:
    from .scene_io import MAPS, atomic_write, safe_output_path
    from .source_bundle import capture_gltf_bundle, sha256_file, verify_file
except ImportError:
    from scene_io import MAPS, atomic_write, safe_output_path
    from source_bundle import capture_gltf_bundle, sha256_file, verify_file


NATIVE_SHA256 = 'adcca0aba74bd8c472941e7c04f306e8c6daf0fd219256d66e558324cc223f1b'
RECAST_SHA256 = 'cf903e07194dd74648f696ff0311c9146ed50e8da7bb9f3baaa18f7ab0aaef08'
RECAST_COMMIT = '9f4ce64458dfae86e1239c525ddc219c4e9e06f1'
NATIVE_SOURCES = (
    'crates/launcher/src/main.rs', 'crates/launcher/src/first_run.rs',
    'crates/bootstrap/src/args.rs', 'crates/bootstrap/src/launch.rs',
    'crates/asset_transport/src/discover.rs', 'crates/assets/src/gltf_export.rs')
RECAST_PARAMETERS = {
    'cell_size_m': .25, 'cell_height_m': .1, 'agent_height_m': 1.8,
    'agent_radius_m': .4, 'max_climb_m': .35, 'max_slope_deg': 45,
    'walkable_height_voxels': 18, 'walkable_climb_voxels': 3,
    'walkable_radius_voxels': 2, 'max_edge_length_voxels': 48,
    'max_simplification_error_voxels': 1.3, 'min_region_area_voxels': 64,
    'merge_region_area_voxels': 400, 'max_vertices_per_polygon': 6}


def _root(root):
    path = Path(root).expanduser().resolve()
    if not (path/'AGENTS.md').is_file() or not (path/'schema/world-v1.schema.json').is_file():
        raise ValueError('Choose the standalone editor project containing AGENTS.md and schema/world-v1.schema.json')
    return path


def _json(path, limit=32*1024*1024):
    path = Path(path)
    if path.stat().st_size > limit:
        raise ValueError(f'JSON exceeds offline adapter limit: {path}')
    def unique(pairs):
        out = {}
        for key, value in pairs:
            if key in out:
                raise ValueError(f'Duplicate JSON key: {key}')
            out[key] = value
        return out
    def invalid(value):
        raise ValueError(f'Nonfinite JSON constant: {value}')
    result = json.loads(path.read_text(encoding='utf-8-sig'), object_pairs_hook=unique,
                        parse_constant=invalid)
    if not isinstance(result, dict):
        raise ValueError(f'JSON object required: {path}')
    return result


def _relative_file(root, path, digest, label):
    target = safe_output_path(path, root)
    return verify_file(str(target), digest, label)


def _now():
    return datetime.now(timezone.utc).isoformat()


def inspect_native_source(root, asset_root=None):
    """Read and verify the reviewed project copy and original source bindings."""
    root = _root(root)
    if (root/'project/manifest.json').is_file():
        from .installation_setup import native_source
        return native_source(root,asset_root)
    evidence_path = root/'worklog/KRV-02_NATIVE_EXPORT.json'
    proof = _json(evidence_path)
    if (proof.get('kind') != 'offline_native_visual_asset_export'
            or proof.get('result') != 'exported_visual_assets'
            or proof.get('native_sha256') != NATIVE_SHA256
            or set(proof.get('audited_source_sha256', {})) != set(NATIVE_SOURCES)):
        raise ValueError('Native exporter evidence is incomplete or differs from the reviewed executable')
    executable = _relative_file(root, proof['native_copy'], NATIVE_SHA256, 'native exporter copy')
    game_root = Path(proof['source_game_root']).expanduser()
    if not game_root.is_absolute():
        raise ValueError('Native source proof requires an absolute read-only game root')
    game_root = game_root.resolve()
    if game_root == root or game_root.is_relative_to(root):
        raise ValueError('The read-only game source must be separate from the standalone editor')
    original = Path(proof['native_source']).expanduser().resolve()
    if not original.is_relative_to(game_root):
        raise ValueError('Original exporter does not belong to the read-only game source')
    verify_file(str(original), NATIVE_SHA256, 'native exporter original')
    sources = {}
    for relative in NATIVE_SOURCES:
        target = (game_root/relative).resolve()
        if not target.is_relative_to(game_root):
            raise ValueError('Audited source resolves outside the read-only game source')
        verify_file(str(target), proof['audited_source_sha256'][relative], relative)
        sources[relative] = proof['audited_source_sha256'][relative]
    assets = Path(asset_root or proof['readonly_asset_root']).expanduser().resolve()
    if not assets.is_dir() or any(c in assets.as_posix() for c in ('"', '\r', '\n')):
        raise ValueError('A valid local MW2 asset directory without quotes/newlines is required')
    return {'executable': str(executable), 'sha256': NATIVE_SHA256,
            'source_game_root': str(game_root), 'original_executable': str(original),
            'source_sha256': sources, 'asset_root': str(assets),
            'evidence_path': str(evidence_path), 'evidence_sha256': sha256_file(evidence_path)}


def _environment(temporary, assets=None):
    windows = os.environ.get('SystemRoot', r'C:\Windows')
    environment = {'SystemRoot': windows, 'WINDIR': windows,
                   'PATH': str(Path(windows)/'System32')+';'+windows,
                   'TEMP': str(temporary), 'TMP': str(temporary),
                   'IW4L_SKATE': 'off', 'IW4L_BENCH': 'off', 'IW4L_PERF': 'off'}
    if assets is not None:
        environment['IW4L_GAMES'] = str(assets)
    return environment


def _run(command, cwd, environment, log, timeout=60):
    """Exact argv, no shell, bounded wait, editor-local log and temporary files."""
    with Path(log).open('xb') as stream:
        result = subprocess.run(command, cwd=str(cwd), env=environment,
                                stdout=stream, stderr=subprocess.STDOUT,
                                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0),
                                timeout=timeout, check=False)
    if result.returncode:
        raise ValueError(f'Offline tool exited {result.returncode}; inspect {log}')


def run_native_export(root, map_code, asset_root=None):
    """Invoke only the reviewed native `export-gltf MAP` command in a fresh job."""
    root = _root(root)
    if not isinstance(map_code, str) or map_code not in MAPS:
        raise ValueError('Choose one of the 16 documented MW2 map codenames')
    if (root/'project/manifest.json').is_file():
        from . import installation_setup
        return installation_setup.export_map(root,map_code,asset_root)
    source = inspect_native_source(root, asset_root)
    job = safe_output_path(root/'exports'/f'native-{map_code}-{uuid.uuid4().hex}', root)
    assets = Path(source['asset_root'])
    if job.is_relative_to(assets):
        raise ValueError('Native output must be separate from the read-only asset directory')
    job.mkdir(parents=True, exist_ok=False)
    temporary = job/'tmp'
    temporary.mkdir()
    executable = job/'iw4l.exe'
    with Path(source['executable']).open('rb') as src, executable.open('xb') as dst:
        shutil.copyfileobj(src, dst, 1024*1024)
    verify_file(str(executable), NATIVE_SHA256, 'isolated exporter copy')
    config = f'IW4L_GAMES="{assets.as_posix()}"\nIW4L_SKATE=off\n'
    with (job/'.env').open('x', encoding='utf-8', newline='\n') as stream:
        stream.write(config)
    command = [str(executable), 'export-gltf', map_code]
    report_path = job/'native-export.json'
    report = {'kind': 'offline_native_visual_asset_export', 'map': map_code,
              'status': 'RUNNING', 'started_utc': _now(), 'source': source,
              'command': command, 'cwd': str(job), 'game_started': False,
              'runtime_integrated': False, 'collision_validated': False,
              'physical_scale_verified': False, 'timeout_seconds': 60,
              'scale_note': 'Existing exporter convention; not physical metre calibration',
              'coordinate_mapping': 'native [x,z,-y] * 0.0254 -> glTF Y-up'}
    failure = None
    try:
        _run(command, job, _environment(temporary, assets), job/'export.log')
        scenes = list((job/'iw4l-artifacts'/'exports'/map_code).glob('p1a-*/scene.gltf'))
        if len(scenes) != 1:
            raise ValueError(f'Native exporter must produce exactly one scene.gltf; found {len(scenes)}')
        scene = safe_output_path(scenes[0], root)
        bundle = capture_gltf_bundle(scene, include_images=True)
        document = _json(scene)
        report.update(status='PASS', reference_path=str(scene), preview_bundle=bundle,
                      nodes=len(document.get('nodes', [])), meshes=len(document.get('meshes', [])),
                      materials=len(document.get('materials', [])), images=len(document.get('images', [])))
    except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
        failure = exc
        report.update(status='FAIL', error=str(exc)[:500])
    try:
        after = inspect_native_source(root, assets)
        report['sources_unchanged'] = after == source
        report['job_executable_unchanged'] = sha256_file(executable) == NATIVE_SHA256
        if not report['sources_unchanged'] or not report['job_executable_unchanged']:
            raise ValueError('Native exporter or read-only source changed during conversion')
    except (OSError, ValueError) as exc:
        failure = exc
        report.update(status='FAIL', error=str(exc)[:500], sources_unchanged=False)
    report['finished_utc'] = _now()
    report['report_path'] = str(report_path)
    atomic_write(report_path, report, root)
    if failure:
        raise ValueError(f'{str(failure)[:350]}; evidence: {report_path}') from failure
    return report


def inspect_recast_source(root):
    """Verify existing probe/authoring source hashes; never build or download."""
    root = _root(root)
    path = root/'worklog/KRV-08_RECAST_REFERENCE.json'
    proof = _json(path)
    if (proof.get('reference_probe') != 'PASS'
            or proof.get('source', {}).get('commit') != RECAST_COMMIT
            or proof.get('build_products', {}).get('executable_sha256') != RECAST_SHA256):
        raise ValueError('Recast proof differs from the reviewed probe')
    executable = _relative_file(root, proof['build_products']['executable'], RECAST_SHA256, 'Recast executable')
    harness = proof.get('harness_files', {})
    if set(harness) != {'tools/recast_probe/CMakeLists.txt', 'tools/recast_probe/main.cpp'}:
        raise ValueError('Recast proof must bind the CMake and harness sources')
    for name, digest in harness.items():
        _relative_file(root, name, digest, 'Recast harness')
    license_info = proof['source']
    _relative_file(root, license_info['license_path'], license_info['license_sha256'], 'Recast license')
    return {'executable': str(executable), 'sha256': RECAST_SHA256,
            'source_commit': RECAST_COMMIT, 'harness_sha256': dict(harness),
            'evidence_path': str(path), 'evidence_sha256': sha256_file(path)}


def _finite3(value, label):
    if (not isinstance(value, (list, tuple)) or len(value) != 3
            or any(isinstance(x, bool) or not isinstance(x, (int, float)) or not math.isfinite(x) for x in value)):
        raise ValueError(f'{label}: three finite coordinates required')
    return tuple(float(x) for x in value)


def to_recast(point):
    """Blender Z-up world -> right-handed Recast Y-up, preserving winding."""
    x, y, z = _finite3(tuple(point), 'Blender coordinate')
    return (x, z, -y)


def from_recast(point):
    x, y, z = _finite3(point, 'Recast coordinate')
    return (x, -z, y)


def collect_triangles(ctx, objects):
    """Evaluate only explicitly marked, local authored geometry; clear all temps."""
    chosen = list(objects)
    if not chosen:
        raise ValueError('Select one or more marked custom geometry meshes/curves')
    units = getattr(ctx.scene, 'unit_settings', None)
    if units is None or not math.isclose(units.scale_length, 1.0, rel_tol=0, abs_tol=1e-12):
        raise ValueError('Recast authoring requires explicit scene units: one Blender unit equals one metre')
    vertices, triangles, records, ids = [], [], [], set()
    graph = ctx.evaluated_depsgraph_get()
    for obj in chosen:
        if (obj.type not in {'MESH', 'CURVE'} or not obj.get('wz09_custom_geometry')
                or obj.get('wz09_preview_only') or obj.library or getattr(obj, 'override_library', None)):
            raise ValueError('Recast input must contain only local marked custom geometry, never previews')
        if not any(item == obj for item in ctx.scene.objects):
            raise ValueError('Recast input must belong to the active scene')
        uid = obj.get('wz09_geometry_id')
        if not isinstance(uid, str) or not uid or uid in ids:
            raise ValueError('Every Recast input object requires a unique persistent geometry ID')
        ids.add(uid)
        evaluated = obj.evaluated_get(graph)
        mesh = None
        try:
            mesh = evaluated.to_mesh()
            if mesh is None:
                raise ValueError('Selected geometry could not produce an evaluated triangle mesh')
            mesh.calc_loop_triangles()
            offset = len(vertices)
            local = [to_recast(evaluated.matrix_world @ vertex.co) for vertex in mesh.vertices]
            faces = []
            for triangle in mesh.loop_triangles:
                indices = tuple(int(i) for i in triangle.vertices)
                if len(indices) != 3 or len(set(indices)) != 3 or any(i < 0 or i >= len(local) for i in indices):
                    raise ValueError('Invalid evaluated triangle indices')
                a, b, c = (local[index] for index in indices)
                cross = ((b[1]-a[1])*(c[2]-a[2])-(b[2]-a[2])*(c[1]-a[1]),
                         (b[2]-a[2])*(c[0]-a[0])-(b[0]-a[0])*(c[2]-a[2]),
                         (b[0]-a[0])*(c[1]-a[1])-(b[1]-a[1])*(c[0]-a[0]))
                if sum(value*value for value in cross) <= 1e-24:
                    raise ValueError('Degenerate evaluated triangle; repair custom geometry first')
                faces.append(tuple(index+offset for index in indices))
            if not faces:
                raise ValueError('Selected geometry contains no triangles')
            vertices.extend(local)
            triangles.extend(faces)
            if len(vertices) > 1000000 or len(triangles) > 2000000:
                raise ValueError('Selected geometry exceeds the existing Recast probe limits')
            records.append({'geometry_id': uid, 'vertices': len(local), 'triangles': len(faces)})
        finally:
            if mesh is not None:
                evaluated.to_mesh_clear()
    return vertices, triangles, records


def _obj_bytes(vertices, triangles):
    lines = ['# Editor authored geometry; explicit right-handed Y-up metres.',
             '# Experimental reference input, not a native game collider.']
    lines.extend('v '+ ' '.join(format(value, '.9g') for value in point) for point in vertices)
    lines.extend('f '+' '.join(str(index+1) for index in triangle) for triangle in triangles)
    return ('\n'.join(lines)+'\n').encode('ascii')


def validate_recast_result(document, input_path, root, vertices, triangles):
    """Check the actual probe contract, output topology and experiment bounds."""
    if (not isinstance(document, dict) or document.get('coordinate_system') != 'y_up_meters'
            or document.get('status') != 'proposed'
            or document.get('runtime_integrated') is not False
            or document.get('collision_validated') is not False
            or document.get('source_commit') != RECAST_COMMIT
            or document.get('recast_library_version') != '1.6.0'):
        raise ValueError('Recast result does not match the reviewed offline reference contract')
    if (not isinstance(document.get('input_mesh'), str)
            or safe_output_path(document['input_mesh'], root) != Path(input_path).resolve()):
        raise ValueError('Recast result belongs to a different input mesh')
    if (type(document.get('input_vertices')) is not int or type(document.get('input_triangles')) is not int
            or document['input_vertices'] != len(vertices) or document['input_triangles'] != len(triangles)):
        raise ValueError('Recast input counts differ from the authored input')
    parameters = document.get('parameters', {})
    if (not isinstance(parameters, dict)
            or any(isinstance(parameters.get(name), bool) or parameters.get(name) != value
                   for name, value in RECAST_PARAMETERS.items())):
        raise ValueError('Recast result uses unexpected experiment parameters')
    for name in ('raster_width', 'raster_height'):
        if type(parameters.get(name)) is not int or not 0 < parameters[name] <= 4096:
            raise ValueError('Recast result contains invalid raster dimensions')
    points, polygons = document.get('vertices_m'), document.get('polygons')
    if (not isinstance(points, list) or not isinstance(polygons, list)
            or not 3 <= len(points) <= 1000000 or not 1 <= len(polygons) <= 2000000
            or type(document.get('mesh_vertex_count')) is not int
            or type(document.get('mesh_polygon_count')) is not int
            or document['mesh_vertex_count'] != len(points)
            or document['mesh_polygon_count'] != len(polygons)):
        raise ValueError('Recast result mesh counts are invalid')
    lower = [min(v[axis] for v in vertices) for axis in range(3)]
    upper = [max(v[axis] for v in vertices) for axis in range(3)]
    for point in points:
        point = _finite3(point, 'Recast output')
        if (any(point[axis] < lower[axis]-.001 or point[axis] > upper[axis]+.001 for axis in (0, 2))
                or point[1] < lower[1]-.501 or point[1] > upper[1]+3.001):
            raise ValueError('Recast output escapes the input raster bounds')
    for polygon in polygons:
        if (not isinstance(polygon, list) or not 3 <= len(polygon) <= 6
                or any(type(i) is not int or not 0 <= i < len(points) for i in polygon)
                or len(set(polygon)) != len(polygon)):
            raise ValueError('Recast output polygon has invalid indices')
        area = abs(sum(points[polygon[i]][0]*points[polygon[(i+1)%len(polygon)]][2]
                       - points[polygon[(i+1)%len(polygon)]][0]*points[polygon[i]][2]
                       for i in range(len(polygon))))*.5
        if area <= 1e-10:
            raise ValueError('Recast output polygon has zero projected area')
    return points, polygons


def bake_reference(ctx, root, objects, output=None, *, bpy_module=None):
    """Bake selected authored geometry with the existing probe; add only overlay.

    Return manifest and the new object, never nav nodes or runtime proof. The
    optional output is a NEW .json report; adjacent OBJ/raw-result/log are also
    exclusive files. A failed result is retained for inspection.
    """
    root = _root(root)
    tool = inspect_recast_source(root)
    vertices, triangles, inputs = collect_triangles(ctx, objects)
    if output is None:
        output = root/'exports'/f'recast-{uuid.uuid4().hex}'/'reference.json'
    report_path = safe_output_path(output, root)
    if report_path.suffix.lower() != '.json':
        raise ValueError('Recast reference report requires a new .json filename')
    input_path = report_path.with_suffix('.obj')
    raw_path = report_path.with_name(report_path.stem+'.raw.json')
    log_path = report_path.with_suffix('.log')
    if any(path.exists() for path in (report_path, input_path, raw_path, log_path)):
        raise FileExistsError('Recast output already exists; choose a new report filename')
    report_path.parent.mkdir(parents=True, exist_ok=True)
    input_path = safe_output_path(input_path, root)
    with input_path.open('xb') as stream:
        stream.write(_obj_bytes(vertices, triangles))
    temporary = report_path.parent/('tmp-'+uuid.uuid4().hex)
    temporary.mkdir()
    report = {'kind': 'recast_authored_geometry_reference', 'status': 'RUNNING',
              'started_utc': _now(), 'tool': tool, 'input_path': str(input_path),
              'input_sha256': sha256_file(input_path), 'inputs': inputs,
              'input_basis': 'explicit authored Blender world geometry, metres',
              'coordinate_mapping': 'Blender [x,y,z] -> Recast [x,z,-y]',
              'navigation_status': 'proposed', 'runtime_integrated': False, 'collision_validated': False,
              'physical_scale_verified': False, 'game_started': False,
              'parameters': dict(RECAST_PARAMETERS), 'report_path': str(report_path),
              'raw_result_path': str(raw_path), 'timeout_seconds': 60}
    overlay = mesh = None
    bpy = None
    try:
        _run([tool['executable'], str(input_path), str(raw_path)], root,
             _environment(temporary), log_path)
        document = _json(raw_path)
        points, polygons = validate_recast_result(document, input_path, root, vertices, triangles)
        if sha256_file(input_path) != report['input_sha256'] or inspect_recast_source(root) != tool:
            raise ValueError('Recast executable, sources or authored input changed during bake')
        if bpy_module is None:
            import bpy as bpy_module
        bpy = bpy_module
        mesh = bpy.data.meshes.new('WZ09_RECAST_REFERENCE')
        mesh.from_pydata([from_recast(point) for point in points], [], polygons)
        mesh.update()
        overlay = bpy.data.objects.new('WZ09_RECAST_REFERENCE', mesh)
        overlay['wz09_preview_only'] = True
        overlay['wz09_preview_id'] = str(uuid.uuid4())
        overlay['wz09_recast_reference'] = True
        overlay['wz09_recast_status'] = 'proposed'
        overlay['wz09_recast_report'] = str(report_path)
        overlay['wz09_recast_input_sha256'] = report['input_sha256']
        overlay.display_type = 'WIRE'
        overlay.show_in_front = True
        overlay.hide_render = True
        overlay.color = (.05, .8, .4, 1)
        ctx.scene.collection.objects.link(overlay)
        report.update(status='PASS', raw_result_sha256=sha256_file(raw_path),
                      upstream_fixture_tag=document.get('fixture'),
                      fixture_tag_note='Fixed legacy harness tag; actual input is bound above, not a claim of synthetic fixture identity',
                      overlay_id=overlay['wz09_preview_id'],
                      mesh_vertex_count=len(points), mesh_polygon_count=len(polygons),
                      raster_width=document['parameters']['raster_width'],
                      raster_height=document['parameters']['raster_height'])
        report['finished_utc'] = _now()
        atomic_write(report_path, report, root)
    except (OSError, ValueError, TypeError, KeyError, RuntimeError, subprocess.TimeoutExpired) as exc:
        if bpy is not None:
            if overlay is not None:
                bpy.data.objects.remove(overlay, do_unlink=True)
            if mesh is not None and mesh.users == 0:
                bpy.data.meshes.remove(mesh)
        if not report_path.exists():
            report.update(status='FAIL', error=str(exc)[:500], finished_utc=_now())
            atomic_write(report_path, report, root)
        raise ValueError(f'{str(exc)[:350]}; evidence: {report_path}') from exc
    return {'manifest': report, 'overlay_object': overlay,
            'report_path': str(report_path), 'input_path': str(input_path)}
