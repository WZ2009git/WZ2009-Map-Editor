"""Local sixteen-map catalog and bounded offline conversion jobs, without bpy.

Discovery reads known editor export reports and original FF filenames only.
Existing hash-bound bundles are reused. No navigation scale or collision is
inferred from a visual export, and cancellation never removes conversion files.
"""
import copy
from pathlib import Path
from queue import Empty, Queue
from threading import Event, Lock, Thread

try:
    from .layout_geometry import REGIONS
    from .pipeline_workflow import _root, _json, run_native_export
    from .source_bundle import capture_gltf_bundle, verify_preview_bundle
except ImportError:
    from layout_geometry import REGIONS
    from pipeline_workflow import _root, _json, run_native_export
    from source_bundle import capture_gltf_bundle, verify_preview_bundle


MAP_NAMES = (
    ('mp_afghan', 'Afghan'), ('mp_derail', 'Derail'), ('mp_estate', 'Estate'),
    ('mp_favela', 'Favela'), ('mp_highrise', 'Highrise'), ('mp_invasion', 'Invasion'),
    ('mp_checkpoint', 'Karachi'), ('mp_quarry', 'Quarry'), ('mp_rundown', 'Rundown'),
    ('mp_rust', 'Rust'), ('mp_boneyard', 'Scrapyard'), ('mp_nightshift', 'Skidrow'),
    ('mp_subbase', 'Sub Base'), ('mp_terminal', 'Terminal'), ('mp_underpass', 'Underpass'),
    ('mp_brecourt', 'Wasteland'),
)
MAP_CODES = frozenset(code for code, _ in MAP_NAMES)
MAP_REGIONS = {code: region for region, maps, _ in REGIONS for code in maps}


def map_items():
    """Stable enum storage with readable retail names and design regions."""
    return [(code, name, f'{code} | {MAP_REGIONS[code]}') for code, name in MAP_NAMES]


def _code(value):
    if not isinstance(value, str) or value not in MAP_CODES:
        raise ValueError('Choose one of the sixteen original MW2009 maps')
    return value


def _native_files(asset_root):
    if asset_root is None or not str(asset_root).strip():
        return {}
    root = Path(asset_root).expanduser().resolve()
    if not root.is_dir():
        return {}
    folders = [root, root/'zone']
    zone = root/'zone'
    if zone.is_dir():
        folders.extend(sorted((p for p in zone.iterdir() if p.is_dir()), key=lambda p: p.name))
    result = {}
    for code in MAP_CODES:
        paths = [folder/f'{code}.ff' for folder in folders if (folder/f'{code}.ff').is_file()]
        if paths:
            result[code] = [str(path.resolve()) for path in paths]
    return result


def inspect_maps(root, asset_root=None, imported=()):
    """Verify known export reports and all declared buffer/image hashes.

    This can hash large local bundles: call on a worker for interactive refresh.
    Imported records are a caller snapshot containing source_map/preview_loaded.
    A missing FF does not invalidate an already captured local visual bundle.
    """
    root = _root(root)
    original = root/'worklog/KRV-02_NATIVE_EXPORT.json'
    if not asset_root and original.is_file():
        try:
            asset_root = _json(original).get('readonly_asset_root')
        except (OSError, ValueError):
            pass
    native = _native_files(asset_root)
    counts = {}
    for record in imported:
        code = record.get('source_map')
        if code in MAP_CODES:
            item = counts.setdefault(code, {'instances': 0, 'loaded': 0})
            item['instances'] += 1
            item['loaded'] += int(record.get('preview_loaded') is True)
    candidates = []
    if original.is_file():
        candidates.append(original)
    # Explicit adapter output locations, never a source-repository scan.
    exports = root/'exports'
    if exports.is_dir():
        candidates.extend(sorted(exports.glob('native-*/native-export.json')))
    bundles, failures = {}, {}
    for report_path in candidates:
        code = ('mp_rust' if report_path == original else next(
            (candidate for candidate in MAP_CODES if report_path.parent.name.startswith(f'native-{candidate}-')), None))
        try:
            if not report_path.resolve().is_relative_to(root):
                raise ValueError('Export report resolves outside the editor project')
            report = _json(report_path)
            code = report.get('map')
            if code not in MAP_CODES:
                continue
            if report.get('kind') != 'offline_native_visual_asset_export':
                continue
            if report.get('status') == 'PASS':
                bundle = report.get('preview_bundle')
                if not isinstance(bundle, dict):
                    raise ValueError('Successful native report is missing its captured preview bundle')
                verify_preview_bundle(bundle)
            elif report.get('result') == 'exported_visual_assets':
                # The original Rust proof predates preview_bundle. Bind its
                # already recorded reference/resource hashes before recapture.
                source = Path(report['scene']).resolve()
                if not source.is_relative_to(root):
                    raise ValueError('Original visual export is outside the editor project')
                bundle = capture_gltf_bundle(source, include_images=True)
                if bundle['reference_sha256'] != report.get('scene_sha256'):
                    raise ValueError('Original visual export reference hash changed')
                declared = {str((source.parent/item['uri']).resolve()): item['sha256']
                            for item in report.get('resources', [])}
                captured = {item['path']: item['sha256'] for item in bundle['resources']}
                if declared != captured:
                    raise ValueError('Original visual export resource hashes changed')
            else:
                if report.get('status') == 'FAIL':
                    failures[code] = str(report.get('error', 'Offline conversion failed'))[:300]
                continue
            reference = Path(bundle['reference_path']).resolve()
            if not reference.is_relative_to(root):
                raise ValueError('Reusable native bundle must belong to the editor project')
            bundles[code] = {'bundle': bundle, 'report_path': str(report_path.resolve()),
                             'nodes': report.get('nodes'), 'meshes': report.get('meshes')}
        except (OSError, ValueError, KeyError, TypeError) as exc:
            # Retain the individual report error; one broken job cannot hide
            # other valid maps or provoke a silent automatic re-export.
            if code in MAP_CODES:
                failures[code] = f'{report_path.name}: {str(exc)[:250]}'
    result = []
    for code, name in MAP_NAMES:
        count = counts.get(code, {})
        entry = {'code': code, 'name': name, 'region': MAP_REGIONS[code],
                 'native_files': native.get(code, []), 'native_available': code in native,
                 'bundle_available': code in bundles, 'instances': count.get('instances', 0),
                 'imported_instances': count.get('loaded', 0), 'physical_scale_verified': False,
                 'collision_validated': False}
        if code in bundles:
            entry.update(bundles[code])
        if code in failures:
            entry['last_error'] = failures[code]
        entry['status'] = ('imported' if entry['imported_instances'] else 'ready' if code in bundles
                           else 'source_available' if code in native else 'source_missing')
        result.append(entry)
    return result


class OfflineMapQueue:
    """Sequential non-bpy worker around the existing reviewed converter.

    start([]) performs a read-only catalog refresh. cancel() stops before the
    next conversion; the current existing adapter is bounded to sixty seconds.
    poll() returns plain records for Blender's main-thread timer to display.
    """
    def __init__(self):
        self._events = Queue()
        self._stop = Event()
        self._lock = Lock()
        self._thread = None

    @property
    def running(self):
        return self._thread is not None and self._thread.is_alive()

    def start(self, root, codes=(), asset_root=None, imported=()):
        root = _root(root)
        codes = tuple(_code(code) for code in codes)
        if len(set(codes)) != len(codes):
            raise ValueError('Each map may occur only once in an offline batch')
        snapshot = copy.deepcopy(tuple(imported))
        with self._lock:
            if self.running:
                raise ValueError('An offline library job is already running')
            self.poll()
            self._stop.clear()
            self._thread = Thread(target=self._work, args=(root, codes, asset_root, snapshot),
                                  name='WZ2009 offline map conversion', daemon=True)
            self._thread.start()

    def cancel(self):
        self._stop.set()
        self._events.put({'kind': 'cancel_requested', 'message': 'Stop after the current conversion'})

    def poll(self):
        records = []
        while True:
            try:
                records.append(self._events.get_nowait())
            except Empty:
                return records

    def _work(self, root, codes, asset_root, imported):
        completed, failed, reused = [], [], []
        try:
            catalog = inspect_maps(root, asset_root, imported)
            indexed = {entry['code']: entry for entry in catalog}
            self._events.put({'kind': 'catalog', 'maps': catalog})
            for number, code in enumerate(codes, 1):
                if self._stop.is_set():
                    break
                entry = indexed[code]
                self._events.put({'kind': 'progress', 'code': code, 'number': number,
                                  'total': len(codes), 'message': f'{number}/{len(codes)} {entry["name"]}'})
                if entry['bundle_available']:
                    reused.append(code)
                    self._events.put({'kind': 'reused', 'code': code, 'bundle': entry['bundle']})
                    continue
                if not entry['native_available']:
                    failed.append(code)
                    self._events.put({'kind': 'failed', 'code': code, 'message': 'Original local FF is missing'})
                    continue
                try:
                    report = run_native_export(root, code, asset_root)
                    completed.append(code)
                    self._events.put({'kind': 'exported', 'code': code, 'bundle': report['preview_bundle'],
                                      'report_path': report['report_path']})
                except (OSError, ValueError, RuntimeError) as exc:
                    failed.append(code)
                    self._events.put({'kind': 'failed', 'code': code, 'message': str(exc)[:400]})
            if completed:
                self._events.put({'kind': 'catalog', 'maps': inspect_maps(root, asset_root, imported)})
            self._events.put({'kind': 'finished', 'completed': completed, 'failed': failed,
                              'reused': reused, 'cancelled': self._stop.is_set()})
        except (OSError, ValueError, RuntimeError) as exc:
            self._events.put({'kind': 'error', 'message': str(exc)[:400]})

