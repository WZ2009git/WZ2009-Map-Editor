"""Pure Python, no bpy dependency. IW4 Bot Warfare CSV reader and graph helpers.

Source: _bot_utility.gsc parseTokensIntoWaypoint / wpsFromCSV.
The first row first column is the number of waypoints, subsequent rows 5 columns:
origin, child ids, type, angles, jav_point. Child ids are local zero-based indices.
"""
import csv
import math
from pathlib import Path

class NavFormatError(ValueError):
    pass

def vec3(text, field, required=False):
    s = (text or '').strip()
    if not s:
        if required:
            raise NavFormatError(f'{field} missing')
        return None
    parts = s.replace('(', '').replace(')', '').replace(',', ' ').split()
    if len(parts) != 3:
        raise NavFormatError(f'{field} must contain exactly three numbers: {text!r}')
    try:
        result = [float(x) for x in parts]
    except ValueError as exc:
        raise NavFormatError(f'{field} contains non-numbers: {text!r}') from exc
    if not all(math.isfinite(x) for x in result):
        raise NavFormatError(f'{field} contains nonfinite value')
    return result

def read_botwarfare_csv(filename):
    path = Path(filename)
    with path.open('r', encoding='utf-8-sig', newline='', errors='strict') as f:
        rows = list(csv.reader(f))
    if not rows or not rows[0] or not rows[0][0].strip():
        raise NavFormatError('Bot Warfare CSV has no waypoint count in row 1 col 1')
    try:
        count = int(rows[0][0].strip())
    except ValueError as exc:
        raise NavFormatError('First CSV cell must be waypoint count') from exc
    if count <= 0 or count > 100000:
        raise NavFormatError('Invalid or excessive waypoint count')
    if len(rows) - 1 != count:
        raise NavFormatError(f'Waypoint count {count}, actual data rows {len(rows)-1}')
    nodes = []
    for i, row in enumerate(rows[1:]):
        # Official iw4_bot_warfare 31415e49 mp_rust exports a trailing comma.
        # GSC consumes fields 0..4; only the empty sixth field is harmless.
        if len(row) == 6 and not row[-1].strip():
            row = row[:5]
        if len(row) < 3 or len(row)>5:
            raise NavFormatError(f'Waypoint row {i+2} has {len(row)} columns; expected 3..5')
        row = row + ['']*(5-len(row))
        origin = vec3(row[0], f'row {i+2} origin', required=True)
        try:
            children = [int(x) for x in row[1].split()] if row[1].strip() else []
        except ValueError as exc:
            raise NavFormatError(f'Waypoint row {i+2} has non-integer child index') from exc
        for c in children:
            if not 0<=c<count:
                raise NavFormatError(f'Waypoint {i}: child {c} outside 0..{count-1}')
            if c==i:
                raise NavFormatError(f'Waypoint {i}: self-linked')
        node_type = row[2].strip()
        if not node_type:
            raise NavFormatError(f'Waypoint {i}: empty type')
        nodes.append({'index':i,'origin_source_units':origin,'children':list(dict.fromkeys(children)),
                      'type':node_type,'angles':vec3(row[3],f'row {i+2} angles'),
                      'jav_point':vec3(row[4],f'row {i+2} jav_point')})
    return nodes

def scaled_nodes(nodes, map_instance_id, source_units_to_meter):
    if isinstance(source_units_to_meter,bool) or not isinstance(source_units_to_meter,(float,int)) or not math.isfinite(source_units_to_meter) or source_units_to_meter<=0:
        raise NavFormatError('Explicit calibrated positive source_units_to_meter required')
    result = []
    for n in nodes:
        result.append({'id': f'{map_instance_id}:wp:{n["index"]}',
            'owner_map': map_instance_id, 'kind':n['type'],
            'position_local_m':[c*source_units_to_meter for c in n['origin_source_units']],
            'original_index':n['index'],'origin_source_units':n['origin_source_units']})
    return result

def components(node_ids, edges):
    graph={key:set() for key in node_ids}
    for e in edges:
        a,b=e['from'],e['to']
        if a not in graph or b not in graph: continue
        # Weak components for audit; directional reachability is validated separately.
        graph[a].add(b); graph[b].add(a)
    groups=[]; unseen=set(node_ids)
    while unseen:
        start=min(unseen); stack=[start]; unseen.remove(start); group=[]
        while stack:
            x=stack.pop();group.append(x)
            for y in sorted(graph[x]):
                if y in unseen:unseen.remove(y);stack.append(y)
        groups.append(sorted(group))
    return sorted(groups,key=lambda x:(-len(x),x[0]))

def reachable(start, goal, edges):
    adj={}
    # Conditions remain declarative; callers label routes conditional. Disabled
    # links are absent from topology, even if their endpoint IDs still exist.
    for e in edges:
        if e.get('enabled',True) is not False:
            adj.setdefault(e['from'],[]).append(e['to'])
    seen={start}; stack=[start]
    while stack:
        p=stack.pop()
        if p==goal:return True
        for nxt in adj.get(p,[]):
            if nxt not in seen:seen.add(nxt);stack.append(nxt)
    return False
