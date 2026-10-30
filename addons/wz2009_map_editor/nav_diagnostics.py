"""Offline authoring diagnostics. Graph routes never establish walkability.

Only explicit portal/connector identifiers are associated; distances are warning
thresholds selected by the author, not automatic connection or collision tests.
"""
import math
import importlib.util
from pathlib import Path

try:
    from .nav_format import reachable
except ImportError:
    _spec = importlib.util.spec_from_file_location('_wz09_diagnostic_nav_format',
                                                 Path(__file__).with_name('nav_format.py'))
    _nav_format = importlib.util.module_from_spec(_spec)
    _spec.loader.exec_module(_nav_format)
    reachable = _nav_format.reachable

MAX_ITEMS = 50
LIMIT_KEYS = ('max_edge_distance_m', 'max_edge_delta_z_m')


def _records(value):
    return [row for row in value if isinstance(row, dict)] if isinstance(value, list) else []


def _id(value):
    return isinstance(value, str) and bool(value.strip())


def _position(value):
    if not isinstance(value, (list, tuple)) or len(value) != 3:
        return False
    try:
        return all(isinstance(n, (float, int)) and not isinstance(n, bool) and
                   math.isfinite(n) for n in value)
    except (ValueError, OverflowError):
        return False


def _commits(record):
    """Report declared provenance only, without guessing commits from filenames."""
    provenance = record.get('provenance')
    result = set()
    if isinstance(provenance, dict):
        for key in ('source_commit', 'commit', 'reference_commit'):
            value = provenance.get(key)
            if isinstance(value, str) and value.strip():
                result.add(value.strip())
        for row in _records(provenance.get('sources')):
            for key in ('source_commit', 'commit'):
                value = row.get(key)
                if isinstance(value, str) and value.strip():
                    result.add(value.strip())
    return result


def validate_limits(limits=None):
    if limits is None:
        return {key: None for key in LIMIT_KEYS}
    if not isinstance(limits, dict) or set(limits) - set(LIMIT_KEYS):
        raise ValueError('diagnostic_limits: expected max_edge_distance_m/max_edge_delta_z_m')
    normalized = {}
    for key in LIMIT_KEYS:
        value = limits.get(key)
        try:
            invalid = value is not None and (not isinstance(value, (int, float)) or isinstance(value, bool)
                                             or not math.isfinite(value) or value <= 0)
        except (ValueError, OverflowError):
            invalid = True
        if invalid:
            raise ValueError(f'diagnostic_limits.{key}: positive finite number or null required')
        normalized[key] = float(value) if value is not None else None
    return normalized


def diagnose(world, limits=None):
    """Return bounded JSON diagnostics from a world manifest; do not mutate it.

    ``enabled: false`` edges do not participate in topology. A nonempty condition
    is declarative: routes using it are conditional, never runtime-evaluated.
    ``properties.is_portal: true`` is the only automatic portal identification.
    """
    limits = validate_limits(limits)
    world = world if isinstance(world, dict) else {}
    maps = _records(world.get('maps'))
    nav = world.get('nav') if isinstance(world.get('nav'), dict) else {}
    nodes, edges = _records(nav.get('nodes')), _records(nav.get('edges'))
    lookup = {row['id']: row for row in nodes if _id(row.get('id'))}
    map_lookup = {row['id']: row for row in maps if _id(row.get('id'))}
    enabled = [row for row in edges if row.get('enabled', True) is not False]
    active = [row for row in enabled
              if _id(row.get('from')) and _id(row.get('to')) and
              row['from'] in lookup and row['to'] in lookup]
    unconditional = [row for row in active if not str(row.get('condition', '')).strip()]
    warnings, warning_count = [], 0

    def warn(message):
        nonlocal warning_count
        warning_count += 1
        if len(warnings) < MAX_ITEMS:
            warnings.append(message[:320])

    by_status, by_source = {}, {}
    for row in edges:
        status = str(row.get('status', 'unknown'))
        by_status[status] = by_status.get(status, 0) + 1
    commits = set()
    per_map = []
    for owner in sorted(map_lookup):
        record = map_lookup[owner]
        owned = [node for node in nodes if node.get('owner_map') == owner]
        incident = [edge for edge in active if lookup[edge['from']].get('owner_map') == owner
                    or lookup[edge['to']].get('owner_map') == owner]
        proof = record.get('calibration')
        proof = proof if isinstance(proof, dict) else {}
        declared = _commits(record)
        local_status, local_source = {}, {}
        for edge in incident:
            key = str(edge.get('status', 'unknown'))
            local_status[key] = local_status.get(key, 0) + 1
        for node in owned:
            declared.update(_commits(node))
            key = str(node.get('source', 'unknown'))
            local_source[key] = local_source.get(key, 0) + 1
        commits.update(declared)
        per_map.append({'map_id': owner, 'source_map': record.get('source_map'),
                        'nodes': len(owned), 'enabled_incident_edges': len(incident),
                        'enabled_edges_by_status': dict(sorted(local_status.items())),
                        'nodes_by_source': dict(sorted(local_source.items())),
                        'source_commits': sorted(declared),
                        'source_commit_status': 'declared' if declared else 'not_recorded',
                        'landmarks': len(_records(proof.get('landmarks'))),
                        'calibration_basis': proof.get('basis', 'measured_landmarks') if proof else None,
                        'calibration_status': record.get('calibration_status', 'unverified'),
                        'physical_scale_verified': record.get('calibration_status') == 'calibrated'
                            and proof.get('synthetic') is False
                            and proof.get('basis', 'measured_landmarks') != 'export_registration'})
    for node in nodes:
        source = str(node.get('source', 'unknown'))
        by_source[source] = by_source.get(source, 0) + 1
        commits.update(_commits(node))

    portals, disconnected, no_cross_map = [], [], []
    for uid, node in sorted(lookup.items()):
        properties = node.get('properties')
        if not isinstance(properties, dict) or properties.get('is_portal') is not True:
            continue
        incident = [edge for edge in active if uid in (edge['from'], edge['to'])]
        cross_map = [edge for edge in incident
                     if lookup[edge['from']].get('owner_map') != lookup[edge['to']].get('owner_map')]
        entry = {'id': uid, 'owner_map': node.get('owner_map'),
                 'enabled_incident_edges': len(incident),
                 'enabled_cross_map_edges': len(cross_map)}
        portals.append(entry)
        if not incident:
            disconnected.append(uid)
            warn(f'nav portal {uid}: no enabled incident link; portal is disconnected')
        elif not cross_map:
            no_cross_map.append(uid)
            warn(f'nav portal {uid}: no enabled cross-map link; inspect intended connector access')

    exceedances, exceedance_count = [], 0
    max_distance = max_delta_z = 0.0
    measured_edges = unmeasurable_edges = 0
    for edge in active:
        a, b = lookup[edge['from']].get('position_m'), lookup[edge['to']].get('position_m')
        if not _position(a) or not _position(b):
            continue
        distance, delta_z = math.dist(a, b), abs(a[2] - b[2])
        if not math.isfinite(distance) or not math.isfinite(delta_z):
            unmeasurable_edges += 1
            warn(f'nav edge {edge.get("id", "?")}: coordinate difference exceeds finite diagnostic range')
            continue
        max_distance, max_delta_z = max(max_distance, distance), max(max_delta_z, delta_z)
        measured_edges += 1
        checks = []
        if limits['max_edge_distance_m'] is not None and distance > limits['max_edge_distance_m']:
            checks.append('distance')
        if limits['max_edge_delta_z_m'] is not None and delta_z > limits['max_edge_delta_z_m']:
            checks.append('delta_z')
        if checks:
            exceedance_count += 1
            entry = {'id': edge.get('id'), 'from': edge['from'], 'to': edge['to'],
                     'distance_m': distance, 'delta_z_m': delta_z, 'exceeds': checks}
            if len(exceedances) < MAX_ITEMS:
                exceedances.append(entry)
            warn(f'nav edge {edge.get("id", "?")}: distance {distance:.3f} m / |delta Z| {delta_z:.3f} m '
                 f'exceeds configured {"/".join(checks)} warning threshold; no collision test')

    bridge_access = []
    for geometry in _records(world.get('geometry')):
        connector = geometry.get('connector')
        if not isinstance(connector, dict):
            continue
        for segment in _records(connector.get('segments')):
            a, b = segment.get('from_portal'), segment.get('to_portal')
            entry = {'segment_id': segment.get('id'), 'geometry_id': geometry.get('id'),
                     'from_map': segment.get('from_map'), 'to_map': segment.get('to_map'),
                     'from_portal': a, 'to_portal': b, 'topology_only': True,
                     'route_scope': 'global_enabled_graph'}
            if a is None and b is None:
                entry['access_status'] = 'not_declared'
            else:
                valid = (_id(a) and _id(b) and a != b and a in lookup and b in lookup
                         and isinstance(lookup[a].get('properties'), dict)
                         and lookup[a]['properties'].get('is_portal') is True
                         and isinstance(lookup[b].get('properties'), dict)
                         and lookup[b]['properties'].get('is_portal') is True
                         and lookup[a].get('owner_map') == segment.get('from_map')
                         and lookup[b].get('owner_map') == segment.get('to_map'))
                entry['access_status'] = 'declared' if valid else 'invalid'
                if valid:
                    entry.update(forward_route=reachable(a, b, active),
                                 return_route=reachable(b, a, active),
                                 forward_unconditional=reachable(a, b, unconditional),
                                 return_unconditional=reachable(b, a, unconditional))
                    if segment.get('kind') == 'bridge':
                        approaches = {}
                        for portal in (a, b):
                            owner = lookup[portal].get('owner_map')
                            neighbors = set()
                            for edge in active:
                                if edge['from'] == portal and edge['to'] != portal:
                                    neighbor = edge['to']
                                elif edge['to'] == portal and edge['from'] != portal:
                                    neighbor = edge['from']
                                else:
                                    continue
                                if lookup[neighbor].get('owner_map') == owner:
                                    neighbors.add(neighbor)
                            approaches[portal] = len(neighbors)
                        entry['declared_intra_map_approaches'] = approaches
                        entry['few_declared_approaches'] = any(count < 2 for count in approaches.values())
                        if entry['few_declared_approaches']:
                            warn(f'bridge {segment.get("id", "?")}: fewer than two declared enabled '
                                 'intra-map neighbors at an endpoint portal; topology only, no camping/clearance assessment')
                    if not entry['forward_route'] or not entry['return_route']:
                        warn(f'connector {segment.get("id", "?")}: explicit portals lack a directed '
                             'forward or return graph route; no bridge traversal/collision proof')
                else:
                    warn(f'connector {segment.get("id", "?")}: declared portal IDs/flags/map owners are inconsistent')
            if len(bridge_access) < MAX_ITEMS:
                bridge_access.append(entry)
    conditional = len(active) - len(unconditional)
    if conditional:
        warn(f'{conditional} enabled nav edges carry unevaluated conditions; graph routes may be conditional')
    return {'topology_only': True, 'collision_verified': False, 'limits': limits,
            'edges_by_status': dict(sorted(by_status.items())),
            'nodes_by_source': dict(sorted(by_source.items())),
            'source_commits': sorted(commits), 'per_map': per_map,
            'enabled_edges': len(enabled), 'disabled_edges': len(edges) - len(enabled),
            'conditional_enabled_edges': conditional,
            'portals': portals[:MAX_ITEMS], 'portal_count': len(portals),
            'unconnected_portals': disconnected[:MAX_ITEMS], 'unconnected_portal_count': len(disconnected),
            'portals_without_cross_map_links': no_cross_map[:MAX_ITEMS],
            'bridge_access': bridge_access,
            'edge_measurements': {'count': measured_edges, 'unmeasurable_edges': unmeasurable_edges, 'max_distance_m': max_distance,
                                  'max_delta_z_m': max_delta_z},
            'threshold_exceedances': exceedances, 'threshold_exceedance_count': exceedance_count,
            'warnings': warnings, 'warning_count': warning_count,
            'warnings_truncated': max(0, warning_count - len(warnings))}
