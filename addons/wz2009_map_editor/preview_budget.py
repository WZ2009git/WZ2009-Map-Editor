"""Pure lazy-load decisions over the existing source-bound map inventory.

This module neither imports nor unloads anything and does not own a second asset
database. Counts/byte estimates must come from the existing verified bundles or
measured residents. A hidden or bounding-box-displayed mesh remains resident.
"""
import re


def _id(value, name='map_instance_id'):
    if not isinstance(value,str) or not value.strip() or len(value) > 256:
        raise ValueError(name + ': stable nonempty ID required')
    return value


def _cost(value, name, optional=False):
    if optional and value is None:
        return None
    if isinstance(value,bool) or not isinstance(value,int) or not 0 <= value <= 2**63-1:
        raise ValueError(name + ': nonnegative integer required')
    return value


def _ids(values, name):
    if not isinstance(values,(list,tuple)) or len(values) > 4096:
        raise ValueError(name + ': bounded list of stable IDs required')
    return list(dict.fromkeys(_id(value,name) for value in values))


def plan_preview_budget(inventory, requested_ids=(), *, selected_ids=(),
        triangle_budget=3000000, memory_budget_bytes=1073741824, reserve_bytes=0):
    """Plan sequential calls to the existing importer without implicit eviction.

    Inventory records: ``{map_instance_id,source_sha256,loaded:bool,
    triangles:int|None,estimated_bytes:int|None}``. ``estimated_bytes`` is the
    caller's explicit geometry+texture estimate, never inferred from file size.
    Unknown source/costs defer a requested import. Unknown resident costs block
    further loads because a bounded total cannot be established. Selected maps
    have priority, then caller request order. All residents count against both
    budgets, including those displayed as bounds. The caller applies the plan
    through existing preview queues/visibility controls, after rechecking costs.
    """
    if not isinstance(inventory,(list,tuple)) or len(inventory) > 4096:
        raise ValueError('inventory: at most 4096 existing map records required')
    triangles_limit = _cost(triangle_budget,'triangle_budget')
    bytes_limit = _cost(memory_budget_bytes,'memory_budget_bytes')
    reserve = _cost(reserve_bytes,'reserve_bytes')
    if reserve > bytes_limit:
        raise ValueError('reserve_bytes exceeds memory_budget_bytes')
    requested = _ids(requested_ids,'requested_ids')
    selected = _ids(selected_ids,'selected_ids')
    records = {}
    for source in inventory:
        if not isinstance(source,dict):
            raise ValueError('inventory: object records required')
        mid = _id(source.get('map_instance_id'))
        if mid in records:
            raise ValueError('map_instance_id: duplicate ID')
        loaded = source.get('loaded',False)
        if not isinstance(loaded,bool):
            raise ValueError('loaded: boolean required')
        sha = source.get('source_sha256')
        if sha is not None and (not isinstance(sha,str) or re.fullmatch('[0-9a-fA-F]{64}',sha) is None):
            raise ValueError('source_sha256: SHA-256 hex required')
        records[mid] = dict(map_instance_id=mid,source_sha256=sha,loaded=loaded,
            triangles=_cost(source.get('triangles'),'triangles',True),
            estimated_bytes=_cost(source.get('estimated_bytes'),'estimated_bytes',True))
    if set(requested+selected)-set(records):
        raise ValueError('requested/selected IDs must exist in the source inventory')
    resident = [r for r in records.values() if r['loaded']]
    unknown_resident = [r['map_instance_id'] for r in resident
        if r['triangles'] is None or r['estimated_bytes'] is None]
    used_triangles = sum(r['triangles'] or 0 for r in resident)
    used_bytes = reserve+sum(r['estimated_bytes'] or 0 for r in resident)
    load_ids, deferred = [], []
    reasons = {}
    priority = [mid for mid in selected if mid in requested]+[mid for mid in requested if mid not in selected]
    for mid in priority:
        source = records[mid]
        if source['loaded']:
            continue
        reason = None
        if source['source_sha256'] is None:
            reason = 'unverified_source'
        elif source['triangles'] is None or source['estimated_bytes'] is None:
            reason = 'unknown_source_cost'
        elif unknown_resident:
            reason = 'unknown_resident_cost'
        elif used_triangles+source['triangles'] > triangles_limit:
            reason = 'triangle_budget'
        elif used_bytes+source['estimated_bytes'] > bytes_limit:
            reason = 'memory_budget'
        if reason:
            deferred.append(mid)
            reasons[mid] = reason
        else:
            load_ids.append(mid)
            used_triangles += source['triangles']
            used_bytes += source['estimated_bytes']
    desired = set(requested+selected)
    entries = [dict(record,
        display='FULL' if (record['loaded'] or mid in load_ids) and mid in desired
            else 'BOUNDS' if record['loaded'] else 'PROXY',
        decision='resident' if record['loaded'] else 'load' if mid in load_ids
            else 'deferred' if mid in reasons else 'proxy',
        reason=reasons.get(mid)) for mid,record in records.items()]
    return dict(kind='wz09_lazy_preview_budget',version=1,entries=entries,
        load_ids=load_ids,deferred_ids=deferred,reasons=reasons,
        resident_ids=[r['map_instance_id'] for r in resident],unknown_resident_ids=unknown_resident,
        planned_triangles=used_triangles,planned_bytes=used_bytes,
        triangle_budget=triangles_limit,memory_budget_bytes=bytes_limit,reserve_bytes=reserve,
        resident_over_budget=used_triangles > triangles_limit or used_bytes > bytes_limit,
        costs_complete=not unknown_resident,
        budget_kind='explicit_source_estimate_not_process_memory_guarantee',
        unload_automatic=False)
