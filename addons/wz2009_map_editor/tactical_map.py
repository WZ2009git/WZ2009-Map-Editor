"""Deterministic XY schematic and georeferencing; no terrain/collision inference.

Pixel coordinates describe the continuous SVG viewport [0,width] x [0,height].
Affine arrays use X=a*u+b*v+c, Y=d*u+e*v+f (the inverse swaps X/Y
for u/v). Bounds describe provided editor data, never native map dimensions.
"""
import hashlib
import json
import math
import os
from pathlib import Path
import tempfile
from urllib.parse import quote as uri_quote
from xml.sax.saxutils import escape, quoteattr

from .scene_io import safe_output_path
from .source_bundle import validate_orthographic_image, verify_orthographic_image


def _numbers(values, count, label):
    if not isinstance(values, (list, tuple)) or len(values) != count:
        raise ValueError(f'{label}: expected {count} finite numbers')
    if any(isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) for v in values):
        raise ValueError(f'{label}: expected {count} finite numbers')
    return tuple(float(v) for v in values)


def _bounds(bounds):
    x0, y0, x1, y1 = _numbers(bounds, 4, 'bounds_m')
    if x1 <= x0 or y1 <= y0:
        raise ValueError('bounds_m: both horizontal extents must be positive')
    if not math.isfinite(x1-x0) or not math.isfinite(y1-y0):
        raise ValueError('bounds_m: extent overflow')
    return x0, y0, x1, y1


def _size(size):
    if not isinstance(size, (list, tuple)) or len(size) != 2 or any(
            isinstance(v, bool) or not isinstance(v, int) or not 1 <= v <= 32768 for v in size):
        raise ValueError('size: width and height must be integers in 1..32768')
    return tuple(size)


def affine_transforms(bounds, size=(1024, 1024)):
    """Return (world_to_pixel, pixel_to_world), with positive world Y upward."""
    x0, y0, x1, y1 = _bounds(bounds)
    width, height = _size(size)
    sx, sy = width/(x1-x0), height/(y1-y0)
    forward = [sx, 0.0, -x0*sx, 0.0, -sy, y1*sy]
    inverse = [1/sx, 0.0, x0, 0.0, -1/sy, y1]
    if not all(math.isfinite(v) for v in forward+inverse) or sx == 0 or sy == 0:
        raise ValueError('bounds_m: cannot represent invertible pixel transform')
    return forward, inverse


def apply_affine(transform, point):
    a, b, c, d, e, f = _numbers(transform, 6, 'affine')
    x, y = _numbers(point, 2, 'point')
    result = (a*x+b*y+c, d*x+e*y+f)
    if not all(math.isfinite(v) for v in result):
        raise ValueError('affine result overflow')
    return result


def world_to_pixel(point, bounds, size=(1024, 1024)):
    return apply_affine(affine_transforms(bounds, size)[0], point)


def pixel_to_world(point, bounds, size=(1024, 1024)):
    return apply_affine(affine_transforms(bounds, size)[1], point)


def _geometry_bounds(record):
    values = record.get('bounds_m')
    if values is None:
        return None
    if isinstance(values, (list, tuple)) and len(values) == 6:
        x0, y0, z0, x1, y1, z1 = _numbers(values, 6, 'geometry.bounds_m')
        if x1 < x0 or y1 < y0 or z1 < z0:
            raise ValueError('geometry.bounds_m: reversed extent')
        return x0, y0, x1, y1
    x0, y0, x1, y1 = _numbers(values, 4, 'geometry.bounds_m')
    if x1 < x0 or y1 < y0:
        raise ValueError('geometry.bounds_m: reversed extent')
    return x0, y0, x1, y1


def bounds_for_world(world):
    """Bounds of actual supplied positions and explicit geometry bounds only."""
    points = []
    for records in (world.get('maps', []), world.get('markers', []), world.get('nav', {}).get('nodes', [])):
        for record in records:
            x, y, _ = _numbers(record.get('position_m'), 3, 'position_m')
            points.append((x, y))
    for record in world.get('geometry', []):
        box = _geometry_bounds(record)
        if box is not None:
            points.extend(((box[0], box[1]), (box[2], box[3])))
    if not points:
        raise ValueError('No world positions or geometry bounds; provide explicit bounds')
    return _bounds((min(p[0] for p in points), min(p[1] for p in points),
                    max(p[0] for p in points), max(p[1] for p in points)))


def _n(number):
    return format(number, '.12g')


def render_svg(world, bounds=None, size=(1024, 1024), background=None):
    """Render the schematic overlay; export_tactical verifies optional PNG bytes."""
    return _render_svg(world, bounds, size, background)


def _render_svg(world, bounds=None, size=(1024, 1024), background=None, image_href=None):
    """Return UTF-8 SVG bytes plus bounds. Layer IDs are stable and separate."""
    bounds = bounds_for_world(world) if bounds is None else _bounds(bounds)
    width, height = _size(size)
    transform = affine_transforms(bounds, size)[0]
    def point(record):
        xyz = _numbers(record.get('position_m'), 3, 'position_m')
        return apply_affine(transform, xyz[:2])
    def title(record):
        return escape(str(record.get('id', '')))
    out = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}">',
           '<title>WZ2009 editor schematic — supplied positions only</title>',
           '<desc>XY top-down schematic. Bounds are editor data extents, not native map outlines. '
           'Geometry rectangles are supplied bounds. Navigation links retain their recorded status; '
           'this image provides no collision or navigability evidence. World +Y points up. '
           'No terrain imagery is generated.</desc>',
           '<defs><marker id="nav-arrow" viewBox="0 0 6 6" refX="6" refY="3" markerWidth="5" markerHeight="5" orient="auto"><path d="M 0 0 L 6 3 L 0 6" fill="#75b4bc"/></marker></defs>',
           f'<rect width="{width}" height="{height}" fill="#142029"/>',
           '<g id="geometry_bounds" fill="#25424b" fill-opacity="0.55" stroke="#6d9d9c" stroke-width="1">']
    if background is not None:
        validate_orthographic_image(background, bounds, size)
        href = background['path'] if image_href is None else image_href
        if not isinstance(href, str) or ':' in href or '\\' in href or href.startswith('/'):
            raise ValueError('tactical_map.image: local relative SVG image href required')
        out[1] = '<title>WZ2009 source preview image with editor schematic overlay</title>'
        out[2] = ('<desc>Orthographic XY source-geometry preview image with a schematic editor overlay. '
                  'The preview depicts imported visual geometry; it provides no collision or navigability evidence. '
                  'Physical metre scale remains unverified. Navigation links retain their recorded status. '
                  'World +Y points up.</desc>')
        out.insert(-1, f'<g id="preview_image"><image href={quoteattr(uri_quote(href, safe="/"))} x="0" y="0" '
                   f'width="{width}" height="{height}" preserveAspectRatio="none"/></g>')
    for record in sorted(world.get('geometry', []), key=lambda r: str(r.get('id', ''))):
        box = _geometry_bounds(record)
        if box is not None:
            left, top = apply_affine(transform, (box[0], box[3]))
            right, bottom = apply_affine(transform, (box[2], box[1]))
            out.append(f'<rect x="{_n(left)}" y="{_n(top)}" width="{_n(right-left)}" height="{_n(bottom-top)}"><title>{title(record)}: supplied geometry bounds</title></rect>')
    out.append('</g><g id="nav" stroke="#75b4bc" stroke-width="1" fill="#75b4bc">')
    nodes = world.get('nav', {}).get('nodes', [])
    by_id = {}
    for record in nodes:
        if record['id'] in by_id:
            raise ValueError(f'duplicate nav node ID: {record["id"]}')
        by_id[record['id']] = record
    for edge in sorted(world.get('nav', {}).get('edges', []), key=lambda r: str(r.get('id', ''))):
        if edge.get('from') not in by_id or edge.get('to') not in by_id:
            raise ValueError(f'nav edge {edge.get("id")}: missing endpoint')
        ax, ay = point(by_id[edge['from']]); bx, by = point(by_id[edge['to']])
        dash = ' stroke-dasharray="5 4"' if edge.get('status') == 'proposed' else ''
        status = quoteattr(str(edge.get('status', 'unknown')))
        state=''
        note=''
        if edge.get('enabled') is False:
            state=' data-enabled="false" stroke="#888888" opacity="0.35"'
            note='; disabled'
        if edge.get('condition'):note+='; condition: '+str(edge['condition'])
        out.append(f'<path d="M {_n(ax)} {_n(ay)} L {_n(bx)} {_n(by)}" marker-end="url(#nav-arrow)" data-status={status}{dash}{state}><title>{title(edge)}: {escape(str(edge.get("status", "unknown"))+note)}</title></path>')
    for record in sorted(nodes, key=lambda r: str(r.get('id', ''))):
        x, y = point(record)
        out.append(f'<circle cx="{_n(x)}" cy="{_n(y)}" r="2"><title>{title(record)}</title></circle>')
    out.append('</g><g id="map_origins" stroke="#a7bdc2" stroke-width="1.5" fill="none">')
    for record in sorted(world.get('maps', []), key=lambda r: str(r.get('id', ''))):
        x, y = point(record)
        out.append(f'<path d="M {_n(x-5)} {_n(y)} H {_n(x+5)} M {_n(x)} {_n(y-5)} V {_n(y+5)}"><title>{title(record)}: map origin only</title></path>')
    out.append('</g><g id="markers" fill="#f0bd69" font-family="sans-serif" font-size="12">')
    for record in sorted(world.get('markers', []), key=lambda r: str(r.get('id', ''))):
        x, y = point(record)
        label = record.get('properties', {}).get('name', record.get('kind', 'marker'))
        out.append(f'<circle cx="{_n(x)}" cy="{_n(y)}" r="4"><title>{title(record)}: {escape(str(label))}</title></circle>')
        out.append(f'<text x="{_n(x+7)}" y="{_n(y-7)}">{escape(str(label))}</text>')
    out.append('</g></svg>\n')
    return '\n'.join(out).encode('utf-8'), list(bounds)


def _write_new(path, data):
    """Publish complete bytes atomically without replacing any existing target."""
    if path.exists():
        if path.is_file() and path.read_bytes() == data:
            return
        raise FileExistsError(f'Refusing to overwrite: {path}')
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix='.wz09-', suffix='.tmp', dir=path.parent)
    tmp = Path(name)
    try:
        with os.fdopen(fd, 'wb') as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.link(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def export_tactical(world, output_path, bounds=None, size=(1024, 1024), root=None, background=None):
    """Write a new SVG and sidecar inside root; return world.tactical_map data.

    Paths in the returned block are relative to root. A caller writing its world
    JSON in another directory must rebase them relative to that JSON's parent.
    An explicit trusted editor repository root is required.
    """
    if root is None:
        raise ValueError('Explicit editor repository root required')
    root = Path(root).expanduser().resolve()
    path = safe_output_path(output_path, root=root)
    if path.suffix.lower() != '.svg':
        raise ValueError('Tactical output must use .svg')
    sidecar = safe_output_path(path.with_suffix('.tactical.json'), root=root)
    if background is None:
        data, actual_bounds = render_svg(world, bounds, size)
    else:
        actual_bounds = bounds_for_world(world) if bounds is None else _bounds(bounds)
        image = verify_orthographic_image(background, root, actual_bounds, size)
        href = Path(os.path.relpath(image, path.parent)).as_posix()
        data, actual_bounds = _render_svg(world, actual_bounds, size, background, href)
    forward, inverse = affine_transforms(actual_bounds, size)
    manifest = {'status': 'generated', 'path': path.relative_to(root).as_posix(),
                'sha256': hashlib.sha256(data).hexdigest(), 'width_px': size[0], 'height_px': size[1],
                'bounds_m': actual_bounds, 'pixel_to_world': inverse, 'world_to_pixel': forward,
                'layers': ['geometry_bounds', 'nav', 'map_origins', 'markers'], 'depiction': 'schematic'}
    if background is not None:
        manifest['image'] = dict(background)
        manifest['layers'].insert(0, 'preview_image')
    manifest_bytes = (json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False)+'\n').encode('utf-8')
    for target, payload in ((path, data), (sidecar, manifest_bytes)):
        if target.exists() and (not target.is_file() or target.read_bytes() != payload):
            raise FileExistsError(f'Refusing to overwrite: {target}')
    _write_new(path, data)
    _write_new(sidecar, manifest_bytes)
    return manifest
