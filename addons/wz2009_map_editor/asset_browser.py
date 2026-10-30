"""Local native-model browser and non-destructive visual asset placement.

Blender is imported lazily. Source mesh/material datablocks are shared without
editing them. A separate optional gameplay marker is authoring metadata only.
"""
import copy
import json
import math
import uuid
try:
    from .asset_catalog import CATEGORIES, build_catalog, validate_asset_record, validate_placement_records
    from .authoring_helpers import resolve_owner_map
    from .source_bundle import verify_preview_bundle
    from .asset_source_guard import cropped_source_parts,require_uncropped_source
except ImportError:
    from asset_catalog import CATEGORIES, build_catalog, validate_asset_record, validate_placement_records
    from authoring_helpers import resolve_owner_map
    from source_bundle import verify_preview_bundle
    from asset_source_guard import cropped_source_parts,require_uncropped_source


_CLASSES = []
_MARKERS = {'NONE', 'loot_crate', 'buy_station', 'player_spawn', 'poi'}
_ICONS = dict((key, icon) for key, _label, icon in CATEGORIES)
_PART_BASELINE='wz09_asset_parts_json'


def _key(value):return value.as_pointer() if hasattr(value,'as_pointer') else id(value)


def _in_scene(scene,obj):
    objects=scene.objects
    if hasattr(objects,'get'):return _same(objects.get(obj.name),obj)
    return any(_same(obj,item) for item in objects)  # Small offline fakes only.


def _relative_matrix(root,obj):
    """Static parent-local composition avoids a depsgraph update per placement."""
    from mathutils import Matrix
    result=Matrix.Identity(4);visited=set();cursor=obj
    while not _same(cursor,root):
        if cursor is None or _key(cursor) in visited:raise ValueError('Asset part is detached or has a parent cycle')
        visited.add(_key(cursor))
        if getattr(cursor,'parent_type','OBJECT')!='OBJECT' or cursor.constraints or cursor.animation_data or cursor.modifiers:
            raise ValueError('Static asset part has unsupported animation/constraints; use explicit custom geometry')
        result=cursor.matrix_parent_inverse@cursor.matrix_basis@result;cursor=cursor.parent
    return [float(value) for row in result for value in row]


def _part_content(part,cache):
    try:from .preview_render import _mesh_digest,_material_digest
    except ImportError:from preview_render import _mesh_digest,_material_digest
    meshes=cache.setdefault('meshes',{});materials=cache.setdefault('materials',{});images=cache.setdefault('images',{})
    key=_key(part.data)
    if key not in meshes:meshes[key]=_mesh_digest(part.data)
    raw=json.loads(part.get('wz09_alpha_preview_slots_json','[]'))
    if not isinstance(raw,list) or any(type(i) is not int or not 0<=i<len(part.material_slots) for i in raw) or len(raw)!=len(set(raw)):
        raise ValueError('Invalid tracked texture-alpha preview slots')
    overridden=set(raw);slots=[]
    for index,slot in enumerate(part.material_slots):
        material,link=slot.material,slot.link
        if index in overridden:
            original=part.get('wz09_alpha_original_material_'+str(index));preview=part.get('wz09_alpha_preview_material_'+str(index))
            link=part.get('wz09_alpha_original_link_'+str(index))
            if original is None or preview is None or not _same(material,preview) or not preview.get('wz09_preview_alpha_inferred') or link not in {'DATA','OBJECT'}:
                raise ValueError('Tracked texture-alpha preview was changed; restore its original material binding')
            material=original
        slots.append({'link':link,'sha256':_material_digest(material,materials,images)})
    return {'mesh_sha256':meshes[key],'materials':slots}


def _part_snapshot(root,parts,cache):
    return [dict(id=part.get('wz09_asset_part_id'),matrix_local=_relative_matrix(root,part),**_part_content(part,cache)) for part in parts]


def _clear_copied_properties(part):
    """Keep only reversible alpha-preview evidence on a copied render part."""
    raw=part.get('wz09_alpha_preview_slots_json')
    keep=set()
    if raw:
        indices=json.loads(raw)
        if not isinstance(indices,list) or any(type(i) is not int or not 0<=i<len(part.material_slots) for i in indices) or len(indices)!=len(set(indices)):
            raise ValueError('Invalid tracked texture-alpha preview slots')
        keep.add('wz09_alpha_preview_slots_json')
        for index in indices:
            keep.update(prefix+str(index) for prefix in ('wz09_alpha_original_material_','wz09_alpha_original_link_','wz09_alpha_preview_material_'))
    for key in list(part.keys()):
        if key not in keep:del part[key]


def validate_part_snapshot(actual,expected):
    """Pure comparison of immutable part IDs, relative matrices and data hashes."""
    def checked(records):
        if not isinstance(records,list) or not records:raise ValueError('Asset placement has missing render parts')
        lookup={}
        for item in records:
            if not isinstance(item,dict) or set(item)!={'id','matrix_local','mesh_sha256','materials'}:raise ValueError('Invalid asset part baseline')
            uid=item['id']
            if not isinstance(uid,str) or not uid.strip() or uid in lookup:raise ValueError('Asset render parts require unique persistent IDs')
            matrix=item['matrix_local']
            if not isinstance(matrix,list) or len(matrix)!=16 or any(type(v) not in (int,float) or not math.isfinite(v) for v in matrix):raise ValueError('Asset part transform must be finite')
            lookup[uid]=item
        return lookup
    left,right=checked(actual),checked(expected)
    if left.keys()!=right.keys():raise ValueError('Asset placement has missing/extra render parts; restore its original source')
    for uid,item in left.items():
        baseline=right[uid]
        if any(abs(a-b)>2e-4 for a,b in zip(item['matrix_local'],baseline['matrix_local'])):
            raise ValueError('Asset render part moved; manipulate its placement root or use custom geometry')
        if item['mesh_sha256']!=baseline['mesh_sha256'] or item['materials']!=baseline['materials']:
            raise ValueError('Asset source mesh/material changed; restore it or export an explicit custom geometry copy')


def capture_placement_integrity(root,parts,*,cache=None):
    """Authoring-only capture. Serialization never creates IDs or baselines."""
    parts=tuple(parts);cache={} if cache is None else cache
    if not parts or any(part.type!='MESH' for part in parts):raise ValueError('Asset integrity requires actual mesh parts')
    for part in parts:
        if not part.get('wz09_asset_part_id'):part['wz09_asset_part_id']=str(uuid.uuid4())
        part.hide_select=True
    snapshot=_part_snapshot(root,parts,cache);validate_part_snapshot(snapshot,snapshot)
    root[_PART_BASELINE]=_encoded({'version':1,'asset_id':str(root['wz09_asset_id']),
        'origin_offset_m':list(root['wz09_asset_origin_offset_m']),'parts':snapshot})
    return snapshot


def _placement_parts(root,children,tagged):
    uid=root['wz09_asset_placement_id'];parts=[];stack=list(children.get(_key(root),()));seen=set()
    while stack:
        obj=stack.pop();key=_key(obj)
        if key in seen:raise ValueError('Asset placement child hierarchy contains a cycle')
        seen.add(key)
        if obj.get('wz09_asset_placement') or obj.get('wz09_type') or obj.get('wz09_action_element') or obj.get('wz09_audio_zone'):continue
        if obj.get('wz09_action_contour') or obj.get('wz09_audio_contour') or obj.get('wz09_editor_overlay') and not obj.get('wz09_asset_part'):continue
        if obj.type in {'CURVE','SURFACE','FONT','META','VOLUME','POINTCLOUD','CURVES'} or getattr(obj,'instance_type','NONE')!='NONE':
            raise ValueError('Asset placement contains extra instanced/non-mesh geometry; use explicit custom geometry')
        if obj.type=='MESH':
            if not obj.get('wz09_asset_part') or obj.get('wz09_asset_placement_id')!=uid:raise ValueError('Asset placement contains an extra/untracked mesh part')
            parts.append(obj)
        stack.extend(children.get(key,()))
    if not parts or {_key(p) for p in parts}!={_key(p) for p in tagged.get(uid,())}:raise ValueError('Asset placement has missing/detached render parts')
    return parts


def _legacy_matrix_matches(actual,expected,*,native_roundtrip=False):
    """Keep basis checks strict; bound historical native translation roundoff.

    The original native importer normalized a source template with a float32
    inverse-world product, then reparented each instance preserving its world
    matrix. At map-sized coordinates those two historical products can differ
    by 0.00030517578125 in translation with identical geometry/materials and a
    basis difference below 2.4e-7. The native pose check already allows 1e-3
    editor units. This exception applies only to proven legacy native imports;
    immutable new-placement baselines retain their existing 2e-4 check.
    """
    if len(actual)!=16 or len(expected)!=16:return False
    return all(type(a) in (int,float) and type(b) in (int,float)
        and math.isfinite(a) and math.isfinite(b)
        and abs(a-b)<=(1e-3 if native_roundtrip and i in (3,7,11) else 2e-4)
        for i,(a,b) in enumerate(zip(actual,expected)))


def _validate_placement_parts(root,parts,entry,source_roots,children,scene_keys,cache,legacy_cache):
    # Copies made from a cropped preview retained the derivative Mesh flag even
    # after object metadata was cleared. A self-captured baseline cannot prove
    # that this is the original asset; preserve and reject the old placement.
    require_uncropped_source(parts)
    if root.get(_PART_BASELINE):
        baseline=json.loads(root[_PART_BASELINE])
        if (not isinstance(baseline,dict) or set(baseline)!={'version','asset_id','origin_offset_m','parts'}
                or type(baseline['version']) is not int or baseline['version']!=1 or baseline['asset_id']!=entry['id']
                or baseline['origin_offset_m']!=list(root['wz09_asset_origin_offset_m'])):raise ValueError('Asset integrity baseline does not match its source/normalization')
        validate_part_snapshot(_part_snapshot(root,parts,cache),baseline['parts']);return
    # Legacy files acquire no new properties during export. Compare their current
    # source-relative geometry; mutations already shared with that old source
    # before this feature cannot be distinguished without reimporting the source.
    from mathutils import Matrix
    source_key=(entry.get('source_map_instance_id',''),entry['source_sha256'],entry['model'])
    source=source_roots.get(source_key)
    if source is None:
        source=next((value for key,value in source_roots.items() if key[1:]==source_key[1:]),None)
    if source is None:raise ValueError('Legacy asset source preview is not loaded; load its source before checking render parts')
    offset=tuple(_finite3(root['wz09_asset_origin_offset_m'],'Legacy asset origin'));key=(_key(source),offset)
    if key not in legacy_cache:
        original=_mesh_parts(None,source,_children_index=children,_scene_keys=scene_keys)
        require_uncropped_source(original)
        normalization=Matrix.Translation(offset);expected=[]
        for part in original:
            values=_relative_matrix(source,part);matrix=normalization@Matrix([values[i:i+4] for i in range(0,16,4)])
            expected.append(dict(matrix_local=[float(v) for row in matrix for v in row],**_part_content(part,cache)))
        legacy_cache[key]=expected
    actual=[dict(matrix_local=_relative_matrix(root,part),**_part_content(part,cache)) for part in parts]
    native_roundtrip=(bool(source.get('wz09_native_visual_library'))
        and source.get('wz09_native_visual_source_sha')==entry['source_sha256']
        and all(part.get('wz09_native_visual_source_sha')==entry['source_sha256'] for part in parts))
    unmatched=list(legacy_cache[key])
    for part in actual:
        found=next((i for i,item in enumerate(unmatched) if part['mesh_sha256']==item['mesh_sha256'] and part['materials']==item['materials']
            and _legacy_matrix_matches(part['matrix_local'],item['matrix_local'],native_roundtrip=native_roundtrip)),None)
        if found is None:raise ValueError('Legacy asset render parts differ from source geometry/normalization')
        unmatched.pop(found)
    if unmatched:raise ValueError('Legacy asset placement has missing source render parts')


def _encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False)


def _catalog(scene):
    value = json.loads(scene.get('wz09_asset_catalog_json', '{"assets":[],"sources":{}}'))
    if not isinstance(value, dict) or not isinstance(value.get('assets'), list) or not isinstance(value.get('sources'), dict):
        raise ValueError('Asset catalog is invalid; refresh the library')
    return value


def model_metadata(obj):
    """Read imported glTF extras. Blender object names never identify a model."""
    raw = obj.get('iw4l')
    if hasattr(raw, 'get') and isinstance(raw.get('model'), str) and raw.get('model').strip():
        return {'model': raw.get('model'), 'authored_slot': raw.get('authored_slot')}
    return None


def _same(a, b):
    if a is None or b is None:
        return a is b
    return (a.as_pointer() == b.as_pointer()) if hasattr(a, 'as_pointer') else a is b


def _model_roots(scene,*,source_bound=False):
    roots = {}
    ranks = {}
    owner_sources={}
    for obj in scene.objects:
        if (not obj.get('wz09_preview_only') or obj.get('wz09_asset_part')
                or obj.get('wz09_asset_placement') or obj.get('wz09_recast_reference')
                or obj.get('wz09_compacted_source_parts')):
            continue
        metadata = model_metadata(obj)
        owner = resolve_owner_map(obj)
        if metadata and owner is not None:
            owner_id=str(owner.get('wz09_id',''))
            if owner_id not in owner_sources:
                provenance=json.loads(owner.get('wz09_map_provenance_json','{}'))
                owner_sources[owner_id]=owner.get('wz09_preview_sha256') or provenance.get('preview_bundle',{}).get('reference_sha256','')
            digest=str(obj.get('wz09_native_visual_source_sha') or owner_sources[owner_id])
            key=(owner_id,digest,metadata['model']) if source_bound else (owner_id,metadata['model'])
            if source_bound:
                # Keep the exact lazy slice canonical after a later full native
                # import. Existing placements already share that slice's mesh
                # and materials; replacing it would split future placements
                # into duplicate datablocks for the same hash-bound asset.
                rank=(2 if obj.get('wz09_slice_bundle_json') else
                      1 if obj.get('wz09_native_visual_library') else 0)
                if key not in roots or rank>ranks[key]:
                    roots[key]=obj;ranks[key]=rank
            else:
                roots.setdefault(key,obj)
    return roots


def rebuild_catalog(context):
    """Index only verified native models actually present in the active scene."""
    scene = context.scene
    roots = _model_roots(scene,source_bound=True)
    result = {'kind': 'native_preview_model_catalog', 'version': 1, 'sources': {}, 'assets': [],
              'collision_verified': False, 'runtime_integrated': False}
    unique, catalogs = {}, {}
    for owner in scene.objects:
        if owner.get('wz09_type') != 'map_instance':
            continue
        provenance = json.loads(owner.get('wz09_map_provenance_json', '{}'))
        if not isinstance(provenance, dict):
            raise ValueError('Map source provenance must be an object')
        bundles=[provenance['preview_bundle']] if provenance.get('preview_bundle') else []
        supplemental=json.loads(owner.get('wz09_supplemental_preview_bundles_json','[]'))
        if not isinstance(supplemental,list):raise ValueError('Supplemental preview bundles must be a list')
        bundles.extend(supplemental)
        source_map = str(owner.get('wz09_source_map', ''))
        for bundle in bundles:
            digest = bundle.get('reference_sha256') if isinstance(bundle, dict) else None
            if digest in catalogs:
                if result['sources'][digest]['source_map'] != source_map:
                    raise ValueError('The same preview source is assigned to conflicting original map codes')
                catalog = catalogs[digest]
            else:
                catalog = build_catalog(bundle, source_map)
                catalogs[digest] = catalog
                result['sources'].update(catalog['sources'])
            for entry in catalog['assets']:
                if (not scene.get('wz09_light_project')
                        and (str(owner.get('wz09_id', '')),digest,entry['model']) not in roots):
                    continue
                if entry['id'] not in unique:
                    entry = copy.deepcopy(entry)
                    entry['source_map_instance_id'] = str(owner.get('wz09_id', ''))
                    unique[entry['id']] = entry
    shared=json.loads(scene.get('wz09_shared_asset_bundles_json','[]'))
    if not isinstance(shared,list) or len(shared)>64:raise ValueError('Shared asset sources must be a bounded list')
    owner_ids={str(o.get('wz09_id','')) for o in scene.objects if o.get('wz09_type')=='map_instance'}
    for source in shared:
        if (not isinstance(source,dict) or set(source)!={'source_map','source_map_instance_id','preview_bundle'}
                or source['source_map']!='common_mp' or source['source_map_instance_id'] not in owner_ids):
            raise ValueError('Shared source requires common_mp and an existing organizational map owner')
        catalog=build_catalog(source['preview_bundle'],'common_mp')
        for digest,value in catalog['sources'].items():
            if digest in result['sources'] and result['sources'][digest]!=value:
                raise ValueError('Shared source conflicts with an existing source binding')
            result['sources'][digest]=value
        for entry in catalog['assets']:
            entry=copy.deepcopy(entry);entry['source_map_instance_id']=source['source_map_instance_id']
            previous=unique.get(entry['id'])
            if previous is not None and previous!=entry:raise ValueError('Conflicting shared source asset identity')
            unique[entry['id']]=entry
    result['assets'] = sorted(unique.values(), key=lambda entry: (entry['category'], entry['name'], entry['id']))
    # Finish all potentially failing parsing and source checks before UI mutation.
    encoded = _encoded(result)
    old_id = None
    if hasattr(scene, 'wz09_assets') and 0 <= scene.wz09_asset_index < len(scene.wz09_assets):
        old_id = scene.wz09_assets[scene.wz09_asset_index].asset_id
    scene['wz09_asset_catalog_json'] = encoded
    if hasattr(scene, 'wz09_assets'):
        scene.wz09_assets.clear()
        for entry in result['assets']:
            item = scene.wz09_assets.add()
            item.asset_id, item.name, item.category, item.source_map = entry['id'], entry['name'], entry['category'], entry['source_map']
            item.primitive_count = entry['primitive_count']
        scene.wz09_asset_index = next((i for i, entry in enumerate(result['assets']) if entry['id'] == old_id), 0)
        _update_filter(scene, context)
    return result


def _visible_item(scene, item):
    try:from .asset_workspace import role_matches
    except ImportError:from asset_workspace import role_matches
    words = scene.wz09_asset_search.casefold().split()
    text = ' '.join((item.name, item.source_map, item.category)).casefold()
    return ((scene.wz09_asset_category == 'ALL' or scene.wz09_asset_category == item.category)
            and (getattr(scene,'wz09_asset_source_filter','ALL') in ('ALL',item.source_map))
            and role_matches({'model':item.name,'category':item.category},getattr(scene,'wz09_asset_role','ALL'))
            and (not getattr(scene,'wz09_asset_favorites_only',False)
                 or item.asset_id in set(json.loads(scene.get('wz09_asset_favorites_json','[]'))))
            and all(word in text for word in words))


def _update_filter(scene, context):
    if not hasattr(scene, 'wz09_assets'):
        return
    if 0 <= scene.wz09_asset_index < len(scene.wz09_assets) and _visible_item(scene, scene.wz09_assets[scene.wz09_asset_index]):
        return
    scene.wz09_asset_index = next((i for i, item in enumerate(scene.wz09_assets) if _visible_item(scene, item)), 0)


def selected_asset_id(scene):
    if not hasattr(scene, 'wz09_assets') or not 0 <= scene.wz09_asset_index < len(scene.wz09_assets):
        raise ValueError('Refresh the asset library and select a model first')
    item = scene.wz09_assets[scene.wz09_asset_index]
    if not _visible_item(scene, item):
        raise ValueError('No asset matches the current search; adjust the filter')
    return item.asset_id


def _mesh_parts(scene, source_root, *, _children_index=None, _scene_keys=None):
    parts = []
    scene_keys = (_scene_keys if _scene_keys is not None else
                  {obj.as_pointer() if hasattr(obj, 'as_pointer') else id(obj) for obj in scene.objects})
    stack, visited = [source_root], set()
    while stack:
        obj = stack.pop()
        key = obj.as_pointer() if hasattr(obj, 'as_pointer') else id(obj)
        if key in visited:
            raise ValueError('Native model hierarchy contains a cycle')
        visited.add(key)
        if key not in scene_keys or obj.library or getattr(obj, 'override_library', None):
            raise ValueError('Only scene-local imported native models may be placed')
        if obj.constraints or obj.animation_data or obj.modifiers:
            raise ValueError('Native asset has modifiers, constraints or animation; bake it explicitly first')
        if obj.type == 'MESH':
            if not obj.data or not obj.data.vertices or not obj.data.polygons:
                raise ValueError('Native asset contains an empty mesh')
            parts.append(obj)
        # Blender Object.children scans all bpy.data.objects. Batch callers
        # supply one immutable adjacency index for the imported hierarchy.
        children=_children_index.get(key,()) if _children_index is not None else obj.children
        for child in children:
            if child.get('wz09_type') or child.get('wz09_asset_placement'):
                continue
            # Importer primitive children belong to this model. Nested separate
            # native model nodes must never accidentally become part of it.
            if model_metadata(child) and child is not source_root:
                continue
            stack.append(child)
    if not parts:
        raise ValueError('Native model has no loaded mesh parts')
    return parts


def _finite3(value, field):
    try:
        result = [float(value[i]) for i in range(3)]
    except (TypeError, ValueError, IndexError) as exc:
        raise ValueError(f'{field}: three finite coordinates required') from exc
    if any(not math.isfinite(item) for item in result):
        raise ValueError(f'{field}: three finite coordinates required')
    return result


def _source_asset(scene, asset_id):
    catalog = _catalog(scene)
    entries = [entry for entry in catalog['assets'] if entry.get('id') == asset_id]
    if len(entries) != 1:
        raise ValueError('Asset is missing or ambiguous; refresh the library')
    entry = validate_asset_record(entries[0])
    source = catalog['sources'].get(entry['source_sha256'])
    if not source or source.get('source_map') != entry['source_map']:
        raise ValueError('Asset source is missing; refresh the library')
    bundle = source['preview_bundle']
    if (bundle.get('reference_sha256') != entry['source_sha256']
            or bundle.get('reference_path') != entry['reference_path']):
        raise ValueError('Asset source binding changed')
    verify_preview_bundle(bundle)
    roots = _model_roots(scene,source_bound=True)
    source_root = roots.get((entry.get('source_map_instance_id', ''),entry['source_sha256'],entry['model']))
    cropped=source_root is not None and bool(cropped_source_parts(_mesh_parts(scene,source_root)))
    if cropped or source_root is None and getattr(scene,'wz09_lazy_assets_enabled',False):
        import bpy
        from . import asset_workspace
        if bpy.context.scene!=scene:raise ValueError('Activate the asset source scene before lazy loading')
        # Explicit Insert/Preview is authorized to recover the exact source,
        # also when ordinary lazy loading is disabled. Never restore the crop.
        source_root=asset_workspace.ensure_source_loaded(bpy.context,asset_id,entry,bundle,
            force_exact_source=cropped)
    if source_root is None:
        raise ValueError('Loaded source model is missing; refresh the library')
    require_uncropped_source(_mesh_parts(scene,source_root))
    return entry, bundle, source_root


def place_asset(context, asset_id, *, owner_map=None, location=None, normal=None,
                marker_kind='NONE', bpy_module=None, _verified_source=None,_verified_parts=None,_integrity_cache=None,_activate_result=True):
    """Place one shared-data model at cursor/hit, with a new persistent UUID.

    A bottom-centred local origin makes insertion intuitive, retaining its exact
    explicit source-origin offset. Source instance TRS is stripped; imported
    local geometry stays in the established glTF/Blender export convention.
    """
    if bpy_module is None:
        import bpy as bpy_module
    from mathutils import Matrix, Vector
    bpy = bpy_module
    scene = context.scene
    if getattr(context, 'mode', 'OBJECT') != 'OBJECT':
        raise ValueError('Asset placement requires Object Mode; press Tab to leave mesh editing')
    owner = owner_map if owner_map is not None else resolve_owner_map(context.active_object)
    if (owner is None or owner.get('wz09_type') != 'map_instance' or not owner.get('wz09_id')
            or owner.library or getattr(owner, 'override_library', None)
            or not _in_scene(scene,owner)):
        raise ValueError('Select a map or one of its objects as the placement owner')
    if marker_kind not in _MARKERS:
        raise ValueError('Unsupported optional gameplay marker')
    position = _finite3(location if location is not None else scene.cursor.location, 'Asset position')
    entry, bundle, source_root = _source_asset(scene, asset_id) if _verified_source is None else _verified_source
    if entry.get('id')!=asset_id:
        raise ValueError('Batch placement source belongs to another asset')
    parts = _mesh_parts(scene, source_root) if _verified_parts is None else tuple(_verified_parts)
    if not parts or any(part.type!='MESH' for part in parts):raise ValueError('Verified source parts must contain actual static meshes')
    # Also covers cached/batch callers bypassing _source_asset. Check before
    # reading bound_box or creating any object/baseline from the source.
    require_uncropped_source(parts)
    determinant = source_root.matrix_world.determinant()
    if not math.isfinite(determinant) or abs(determinant) < 1e-12:
        raise ValueError('Native source has a singular transform')
    inverse = source_root.matrix_world.inverted()
    matrices = [inverse @ obj.matrix_world for obj in parts]
    corners = [matrix @ Vector(corner) for obj, matrix in zip(parts, matrices) for corner in obj.bound_box]
    if not corners or any(not math.isfinite(v) for corner in corners for v in corner):
        raise ValueError('Native asset has invalid bounds')
    offset = [-(min(point[i] for point in corners) + max(point[i] for point in corners))*.5 for i in (0, 1)]
    offset.append(-min(point.z for point in corners))
    normalization = Matrix.Translation(offset)
    transform = Matrix.Translation(position)
    if normal is not None:
        direction = Vector(_finite3(normal, 'Surface normal'))
        if direction.length <= 1e-8:
            raise ValueError('Surface normal must have positive length')
        transform = transform @ direction.normalized().to_track_quat('Z', 'Y').to_matrix().to_4x4()
    uid = str(uuid.uuid4())
    created = []
    try:
        root = bpy.data.objects.new('Asset · '+entry['name'], None)
        created.append(root)
        scene.collection.objects.link(root)
        root.empty_display_type = 'PLAIN_AXES'
        root.empty_display_size = .7
        root['wz09_asset_placement'] = True
        root['wz09_asset_placement_id'] = uid
        root['wz09_asset_id'] = entry['id']
        root['wz09_owner_map'] = str(owner['wz09_id'])
        root['wz09_asset_origin_offset_m'] = offset
        root['wz09_asset_record_json'] = _encoded({'asset': entry, 'preview_bundle': bundle})
        root['wz09_preview_only'] = True
        root['wz09_preview_id'] = str(uuid.uuid4())
        root['wz09_collision_verified'] = False
        root.parent = owner
        root.matrix_parent_inverse = Matrix.Identity(4)
        root.matrix_world = transform
        for source, matrix in zip(parts, matrices):
            # Object copies preserve any per-object material-slot overrides.
            # Their mesh and material datablocks intentionally remain shared.
            obj = source.copy()
            obj.name = entry['name']
            created.append(obj)
            _clear_copied_properties(obj)
            scene.collection.objects.link(obj)
            obj.hide_viewport = False
            obj.hide_render = False
            obj.hide_select = True
            obj.hide_set(False)
            obj.parent = root
            obj.matrix_parent_inverse = Matrix.Identity(4)
            obj.matrix_basis = normalization @ matrix
            obj['wz09_asset_part'] = True
            obj['wz09_asset_placement_id'] = uid
            obj['wz09_preview_only'] = True
            obj['wz09_preview_id'] = str(uuid.uuid4())
            obj.color = source.color
        capture_placement_integrity(root,created[1:],cache=_integrity_cache)
        if marker_kind != 'NONE':
            marker = bpy.data.objects.new('Marker · '+marker_kind, None)
            created.append(marker)
            scene.collection.objects.link(marker)
            marker.parent = root
            marker.matrix_parent_inverse = Matrix.Identity(4)
            marker.matrix_basis = Matrix.Identity(4)
            marker.empty_display_type = 'CUBE' if marker_kind in {'loot_crate', 'buy_station'} else 'SPHERE'
            marker.empty_display_size = .6
            marker.show_in_front = True
            marker['wz09_type'] = marker_kind
            marker['wz09_id'] = str(uuid.uuid4())
            marker['wz09_profile'] = 'default'
            marker['wz09_properties_json'] = _encoded({'asset_placement_id': uid,
                'editor_owner_map': str(owner['wz09_id']), 'runtime_integrated': False})
            if marker_kind == 'loot_crate':
                marker['wz09_loot_table'] = 'standard'
            if marker_kind == 'buy_station':
                marker['wz09_shop_catalog'] = 'standard'
            if marker_kind == 'poi':
                marker['wz09_poi_name'] = entry['name']
            root['wz09_asset_marker_id'] = marker['wz09_id']
        if _activate_result:
            for obj in context.selected_objects:obj.select_set(False)
            root.select_set(True);context.view_layer.objects.active=root
        return root
    except Exception:
        for obj in reversed(created):
            bpy.data.objects.remove(obj, do_unlink=True)
        raise


def serialize_placements(scene):
    """Editor manifest records, with explicit visual-only/gameplay separation."""
    result, ids = [], set()
    objects=tuple(scene.objects);children={};tagged={};scene_keys={_key(obj) for obj in objects};cache={};legacy_cache={};source_roots=None
    for obj in objects:
        if obj.parent is not None:children.setdefault(_key(obj.parent),[]).append(obj)
        if obj.get('wz09_asset_part') and getattr(obj,'type',None)=='MESH':tagged.setdefault(obj.get('wz09_asset_placement_id'),[]).append(obj)
    map_ids = {str(obj.get('wz09_id')) for obj in scene.objects if obj.get('wz09_type') == 'map_instance'}
    marker_ids = {str(obj.get('wz09_id')) for obj in scene.objects if obj.get('wz09_type') in _MARKERS-{'NONE'}}
    for obj in scene.objects:
        if not obj.get('wz09_asset_placement'):
            continue
        uid = obj.get('wz09_asset_placement_id')
        if not isinstance(uid, str) or not uid.strip() or uid in ids:
            raise ValueError('Asset placements require unique persistent IDs')
        ids.add(uid)
        owner = resolve_owner_map(obj)
        if (owner is None or obj.get('wz09_owner_map') != owner.get('wz09_id')
                or obj.get('wz09_owner_map') not in map_ids):
            raise ValueError('Asset placement owner and map hierarchy disagree')
        metadata = json.loads(obj.get('wz09_asset_record_json', '{}'))
        entry = validate_asset_record(metadata.get('asset'))
        bundle = metadata.get('preview_bundle')
        if (not isinstance(bundle, dict) or entry['source_sha256'] != bundle.get('reference_sha256')
                or entry['reference_path'] != bundle.get('reference_path') or obj.get('wz09_asset_id') != entry['id']):
            raise ValueError('Asset placement has inconsistent source binding')
        parts=_placement_parts(obj,children,tagged)
        if not obj.get(_PART_BASELINE) and source_roots is None:source_roots=_model_roots(scene,source_bound=True)
        _validate_placement_parts(obj,parts,entry,source_roots or {},children,scene_keys,cache,legacy_cache)
        scale = _finite3(obj.matrix_world.to_scale(), 'Asset scale')
        if any(value <= 0 for value in scale) or obj.matrix_world.determinant() <= 0:
            raise ValueError('Asset placements require positive, non-reflected scale')
        # Matrix decomposition must round-trip: scale plus Euler cannot encode shear.
        rotation = obj.matrix_world.to_euler('XYZ')
        from mathutils import Matrix
        reconstructed = rotation.to_matrix().to_4x4() @ Matrix.Diagonal([*scale, 1])
        if any(abs(float(reconstructed[r][c])-float(obj.matrix_world[r][c])) > 1e-5 for r in range(3) for c in range(3)):
            raise ValueError('Asset placement shear cannot be exported')
        record = {'id': uid, 'asset_id': entry['id'], 'owner_map': str(owner['wz09_id']),
                  'position_m': _finite3(obj.matrix_world.translation, 'Asset position'),
                  'rotation_deg': [math.degrees(value) for value in _finite3(rotation, 'Asset rotation')],
                  'scale': scale, 'origin_offset_m': _finite3(obj.get('wz09_asset_origin_offset_m'), 'Asset origin'),
                  'source': copy.deepcopy(entry), 'preview_bundle': copy.deepcopy(bundle),
                  'collision_verified': False, 'runtime_integrated': False}
        marker = obj.get('wz09_asset_marker_id')
        if marker:
            if marker not in marker_ids:
                raise ValueError('Asset placement marker reference is missing')
            record['marker_id'] = str(marker)
        result.append(record)
    # Semantic/file checks share one per-source cache across every placement.
    marker_records = []
    for obj in scene.objects:
        if obj.get('wz09_type') in _MARKERS-{'NONE'}:
            properties = json.loads(obj.get('wz09_properties_json', '{}'))
            if not isinstance(properties, dict):
                raise ValueError('Gameplay marker properties must be an object')
            owner = resolve_owner_map(obj)
            if owner is not None:
                properties['editor_owner_map'] = str(owner.get('wz09_id', ''))
            marker_records.append({'id': obj.get('wz09_id'), 'kind': obj.get('wz09_type'), 'properties': properties})
    errors = validate_placement_records(result, [{'id': uid} for uid in map_ids], marker_records, verify_files=True)
    if errors:
        raise ValueError(errors[0])
    return sorted(result, key=lambda item: item['id'])


def draw_browser(layout, context):
    scene = context.scene
    space = getattr(context, 'space_data', None)
    if getattr(space, 'type', None) == 'VIEW_3D' and hasattr(space, 'shading'):
        layout.prop(space.shading, 'type', text='View')
    row = layout.row(align=True)
    row.operator('wz09.refresh_assets', text='Refresh models', icon='FILE_REFRESH')
    row.label(text=str(len(scene.wz09_assets)))
    layout.prop(scene, 'wz09_asset_search', text='', icon='VIEWZOOM')
    layout.prop(scene, 'wz09_asset_category', text='Category')
    layout.template_list('WZ09_UL_assets', '', scene, 'wz09_assets', scene, 'wz09_asset_index', rows=6)
    if (scene.wz09_assets and 0 <= scene.wz09_asset_index < len(scene.wz09_assets)
            and _visible_item(scene, scene.wz09_assets[scene.wz09_asset_index])):
        selected = scene.wz09_assets[scene.wz09_asset_index]
        layout.label(text=selected.source_map+' · '+str(selected.primitive_count)+' Materialteile', icon='INFO')
        layout.prop(scene, 'wz09_asset_marker_kind', text='Gameplay marker')
        row = layout.row(align=True)
        row.operator('wz09.place_asset', text='Am Cursor', icon='PIVOT_CURSOR')
        row.operator('wz09.place_asset_surface', text='Click a surface', icon='SNAP_FACE')
        layout.prop(scene, 'wz09_asset_align_normal')
        row = layout.row(align=True)
        row.operator('wz09.asset_texture_alpha', text='Texture alpha', icon='SHADING_TEXTURE')
        row.operator('wz09.asset_restore_alpha', text='Original', icon='LOOP_BACK')
        layout.label(text='Texture alpha requires material preview', icon='INFO')
    else:
        layout.label(text='No results - adjust the filters.' if scene.wz09_assets
                          else 'Load a native glTF map first.', icon='INFO')
    layout.label(text='Visuals and optional markers', icon='INFO')
    layout.label(text='Runtime feature not integrated')


def _classes(bpy):
    from bpy.props import StringProperty, IntProperty

    class WZ09_AssetItem(bpy.types.PropertyGroup):
        asset_id: StringProperty()
        category: StringProperty()
        source_map: StringProperty()
        primitive_count: IntProperty()

    class WZ09_UL_assets(bpy.types.UIList):
        def draw_item(self, context, layout, data, item, icon, active_data, active_propname, index):
            row = layout.row(align=True)
            row.label(text=item.name, icon=_ICONS.get(item.category, 'OBJECT_DATA'))

        def filter_items(self, context, data, propname):
            scene = context.scene
            flags = []
            for item in getattr(data, propname):
                flags.append(self.bitflag_filter_item if _visible_item(scene, item) else 0)
            sort=getattr(scene,'wz09_asset_sort','NAME')
            items=list(getattr(data,propname))
            order=sorted(range(len(items)),key=lambda i:((items[i].source_map if sort=='SOURCE' else items[i].category if sort=='CATEGORY' else ''),items[i].name.casefold(),items[i].asset_id))
            ranks=[0]*len(items)
            for rank,index in enumerate(order):ranks[index]=rank
            return flags, ranks

    class WZ09_OT_refresh_assets(bpy.types.Operator):
        bl_idname = 'wz09.refresh_assets'
        bl_label = 'Index models from loaded maps'
        bl_description = 'Browse hash-verified glTF model sources; exclude world meshes and preserve original names'
        bl_options = {'REGISTER', 'UNDO'}

        def execute(self, context):
            try:
                result = rebuild_catalog(context)
                self.report({'INFO'}, f"{len(result['assets'])} native model assets available")
                return {'FINISHED'}
            except (OSError, ValueError, TypeError, KeyError, RuntimeError) as exc:
                self.report({'ERROR'}, str(exc)[:240])
                return {'CANCELLED'}

    class WZ09_OT_place_asset(bpy.types.Operator):
        bl_idname = 'wz09.place_asset'
        bl_label = 'Place asset at 3D cursor'
        bl_description = 'Assign a new placement ID to a model in the active map; shared source data remains unchanged'
        bl_options = {'REGISTER', 'UNDO'}

        def execute(self, context):
            try:
                place_asset(context, selected_asset_id(context.scene), marker_kind=context.scene.wz09_asset_marker_kind)
                self.report({'INFO'}, 'Asset platziert · G verschieben · R drehen · S skalieren')
                return {'FINISHED'}
            except (OSError, ValueError, TypeError, KeyError, RuntimeError) as exc:
                self.report({'ERROR'}, str(exc)[:240])
                return {'CANCELLED'}

    class WZ09_OT_place_asset_surface(bpy.types.Operator):
        bl_idname = 'wz09.place_asset_surface'
        bl_label = 'Place asset on visible surface'
        bl_description = 'Click a surface in the 3D viewport; Esc or right-click cancels. A visible mesh hit does not verify collision'
        bl_options = {'REGISTER', 'UNDO'}

        def invoke(self, context, event):
            try:
                if context.area.type != 'VIEW_3D':
                    raise ValueError('Asset placement requires a 3D viewport')
                self._asset_id = selected_asset_id(context.scene)
                self._owner = resolve_owner_map(context.active_object)
                if self._owner is None:
                    raise ValueError('Select a target map or one of its objects first')
                self._marker = context.scene.wz09_asset_marker_kind
                self._align = context.scene.wz09_asset_align_normal
                self._area, self._window = context.area, context.window
                context.window_manager.modal_handler_add(self)
                context.window.cursor_modal_set('CROSSHAIR')
                context.workspace.status_text_set('Place asset: click a surface - middle mouse navigates - Esc cancels')
                return {'RUNNING_MODAL'}
            except (ValueError, RuntimeError) as exc:
                self.report({'ERROR'}, str(exc)[:240])
                return {'CANCELLED'}

        def _finish(self, context):
            context.window.cursor_modal_restore()
            context.workspace.status_text_set(None)

        def modal(self, context, event):
            if event.type in {'ESC', 'RIGHTMOUSE'}:
                self._finish(context)
                return {'CANCELLED'}
            if context.area != self._area or context.window != self._window:
                self._finish(context)
                return {'CANCELLED'}
            if event.type == 'LEFTMOUSE' and event.value == 'PRESS':
                from bpy_extras import view3d_utils
                region = next((region for region in context.area.regions if region.type == 'WINDOW'), None)
                if region is None:
                    self._finish(context)
                    return {'CANCELLED'}
                point = (event.mouse_x-region.x, event.mouse_y-region.y)
                if not 0 <= point[0] < region.width or not 0 <= point[1] < region.height:
                    return {'PASS_THROUGH'}
                if any(item.type in {'UI', 'TOOLS', 'HEADER', 'TOOL_HEADER', 'HUD'}
                       and item.x <= event.mouse_x < item.x+item.width
                       and item.y <= event.mouse_y < item.y+item.height for item in self._area.regions):
                    return {'PASS_THROUGH'}
                view = context.space_data.region_3d
                origin = view3d_utils.region_2d_to_origin_3d(region, view, point)
                direction = view3d_utils.region_2d_to_vector_3d(region, view, point)
                location, normal = None, None
                depsgraph = context.evaluated_depsgraph_get()
                ray_origin = origin.copy()
                for _ in range(64):
                    hit, candidate, hit_normal, _index, obj, _matrix = context.scene.ray_cast(depsgraph, ray_origin, direction)
                    if not hit:
                        break
                    original = getattr(obj, 'original', obj)
                    if (not original.get('wz09_editor_overlay') and not original.get('wz09_recast_reference')
                            and original.visible_get(view_layer=context.view_layer, viewport=context.space_data)):
                        location, normal = candidate, hit_normal
                        break
                    ray_origin = candidate+direction*.0001
                if location is None:
                    self.report({'WARNING'}, 'No visible mesh surface hit; click again or press Esc')
                    return {'RUNNING_MODAL'}
                try:
                    place_asset(context, self._asset_id, owner_map=self._owner, location=location,
                                normal=normal if self._align else None, marker_kind=self._marker)
                    self._finish(context)
                    return {'FINISHED'}
                except (OSError, ValueError, TypeError, KeyError, RuntimeError) as exc:
                    self._finish(context)
                    self.report({'ERROR'}, str(exc)[:240])
                    return {'CANCELLED'}
            return {'PASS_THROUGH'}

    return [WZ09_AssetItem, WZ09_UL_assets, WZ09_OT_refresh_assets,
            WZ09_OT_place_asset, WZ09_OT_place_asset_surface]


def register():
    import bpy
    from bpy.props import CollectionProperty, IntProperty, StringProperty, EnumProperty, BoolProperty
    global _CLASSES
    _CLASSES = _classes(bpy)
    for cls in _CLASSES:
        bpy.utils.register_class(cls)
    bpy.types.Scene.wz09_assets = CollectionProperty(type=_CLASSES[0])
    bpy.types.Scene.wz09_asset_index = IntProperty(default=0, min=0)
    bpy.types.Scene.wz09_asset_search = StringProperty(name='Search models', description='Search original names, map codes and categories', update=_update_filter)
    bpy.types.Scene.wz09_asset_category = EnumProperty(name='Category',
        items=[('ALL', 'All models', 'All loaded model categories')]
            + [(key, label, 'Search category based on the preserved original name') for key, label, _icon in CATEGORIES], update=_update_filter)
    bpy.types.Scene.wz09_asset_marker_kind = EnumProperty(name='Gameplay marker', default='NONE', items=[
        ('NONE', 'Decoration only', 'Place a visible source model only'),
        ('loot_crate', 'Lootkiste', 'Link a separate loot crate marker; runtime behavior is not implemented'),
        ('buy_station', 'Buystation', 'Link a separate shop marker; runtime behavior is not implemented'),
        ('player_spawn', 'Spielerspawn', 'Link a separate spawn marker'),
        ('poi', 'Location / POI', 'Link a separate POI marker')])
    bpy.types.Scene.wz09_asset_align_normal = BoolProperty(name='Align to surface normal', default=False,
        description='Align model Z to the visible mesh normal when placing it')
    from . import preview_materials
    preview_materials.register()


def unregister():
    import bpy
    from . import preview_materials
    preview_materials.unregister()
    for name in ('wz09_assets', 'wz09_asset_index', 'wz09_asset_search', 'wz09_asset_category',
                 'wz09_asset_marker_kind', 'wz09_asset_align_normal'):
        if hasattr(bpy.types.Scene, name):
            delattr(bpy.types.Scene, name)
    for cls in reversed(_CLASSES):
        bpy.utils.unregister_class(cls)
    _CLASSES.clear()
