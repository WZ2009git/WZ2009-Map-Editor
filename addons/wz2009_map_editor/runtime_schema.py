"""Pure schema dictionaries shared by runtime and independent JSON validators."""
import copy


def definitions():
    def obj(properties, optional=()):
        return dict(type='object', additionalProperties=False, properties=properties,
                    required=[k for k in properties if k not in optional])
    def arr(items, minimum=0, maximum=None):
        value = dict(type='array', items=copy.deepcopy(items), minItems=minimum)
        if maximum is not None:
            value['maxItems'] = maximum
        return value
    def num(low=None, high=None):
        value = {'type': 'number'}
        if low is not None:
            value['minimum'] = low
        if high is not None:
            value['maximum'] = high
        return value
    def integer(low=0, high=None):
        return dict(num(low, high), type='integer')
    def const(value):
        return {'const': value}
    def ref(name):
        return {'$ref': '#/$defs/runtime_'+name}
    def nullable(value):
        result = copy.deepcopy(value)
        result['type'] = [result['type'], 'null']
        return result
    uid = dict(type='string', pattern='^[\\x21-\\x7e]{1,128}$', minLength=1, maxLength=128)
    sha = dict(type='string', pattern='^[0-9a-f]{64}$')
    false = const(False)
    v2, v3 = arr(num(-3200, 3200), 2, 2), arr(num(), 3, 3)
    unit = arr(num(-1.00001, 1.00001), 3, 3)
    polygon = arr(v2, 3, 256)
    resource = obj(dict(path=dict(type='string', minLength=1,
                       pattern=r'^(?!/)(?!.*(?:^|/)\.\.(?:/|$))(?:[^/\\:\x00-\x1f]+/)*[^/\\:\x00-\x1f]+$'),
                        sha256=sha, byte_length=integer(1)))
    pose = obj(dict(position_m=v3, forward=unit, up=unit))
    camera = copy.deepcopy(pose)
    camera['properties']['hfov_deg'] = num(.01, 179.99)
    camera['required'].append('hfov_deg')
    def header(kind):
        return dict(kind=const(kind), version=const(1), id=uid, world_id=const('wz09_karvorsk'),
                    units=const('meters'), coordinate_system=const('world_z_up'),
                    runtime_integrated=false, physical_scale_verified=false, clearance_verified=false)
    intro = header('wz09_intro_runtime')
    intro.update(sample_rate_hz=const(30), evaluation_source={'enum': ['blender_evaluated_depsgraph', 'synthetic_fixture']},
                 source_status={'enum': ['synthetic', 'evaluated_editor_scene']},
                 source_timeline_id=uid, source_timeline_sha256=sha, source_routes_sha256=sha,
                 aircraft_model_ref=const('vehicle_ac130_coop@common_mp'),
                 aircraft_forward_axis={'enum': ['+X', '-X', '+Y', '-Y', '+Z', '-Z']},
                 aircraft_up_axis={'enum': ['+X', '-X', '+Y', '-Y', '+Z', '-Z']},
                 spawn_marker_id=uid, spawn_offset_m=v3, insertion_s=num(0, 3600),
                 jump_open_s=num(0, 3600), jump_close_s=num(0, 3600),
                 jump_open_abs_s=num(0, 3600), jump_close_abs_s=num(0, 3600), auto_jump_at_close=const(True),
                 camera_axes=obj(dict(forward=const('-Z'), up=const('+Y'))), duration_s=num(.001, 3600),
                 samples=arr(obj(dict(t_s=num(0, 3600), presentation_only={'type': 'boolean'}, aircraft=pose, camera=camera)), 2, 108002),
                 audio_events=arr(obj(dict(id=uid, t_s=num(0, 3600), alias_ref=dict(type='string', pattern='^[A-Za-z0-9_]+$'),
                     parameters=obj(dict(volume=num(0, 1), fade_in_s=num(0), fade_out_s=num(0), loop={'type': 'boolean'})))), 0, 10000),
                 continuation=obj(dict(route_id=uid, route_elapsed_at_insertion_s=num(0), start_abs_s=num(0), end_abs_s=num(0),
                     interpolation=const('sampled_linear'), speed_mode=const('per_segment'), segment_speeds_mps=arr(num(0), 1, 108001))))
    royale = header('wz09_royale_layout')
    royale.update(play_bounds=polygon, drop_bounds=polygon,
                  out_of_bounds=arr(obj(dict(id=uid, polygon_m=polygon,
                      reason={'enum': ['deep_water', 'world_edge', 'authored_exclusion']})), 0, 4096),
                  flight_altitude_m=num(.01, 3200),
                  spawn=obj(dict(marker_id=uid, position_m=v3, aircraft_local_offset_m=v3)),
                  aircraft_corridor=obj(dict(radius_m=num(.01), minimum_clearance_m=num(0), verification_status=const('unverified'))),
                  parachute=obj(dict(deploy_height_m=num(.01), auto_deploy={'type': 'boolean'}, ground_clearance_verified=false,
                      model_ref=const('parachute_deploy@invasion')), optional=('model_ref',)),
                  zone_center_candidates=arr(obj(dict(id=uid, position_m=v3, weight=num(.000001))), 0, 10000),
                  poi_regions=arr(obj(dict(poi_id=uid, name=dict(type='string', minLength=1, maxLength=24, pattern='^[A-Z0-9 _-]+$'),
                                          polygon_m=polygon, priority=integer(-10000, 10000))), 0, 1024), gulag_arena=const(None))
    calibration = obj(dict(basis={'enum': ['export_registration', 'p1a_export_convention']},
                      source_units_to_meter=num(.025399, .025401),
                      axis_mapping=arr({'enum': ['x', 'y', 'z']}, 3, 3), offset_m=v3,
                      exporter_sha256=sha, source_sha256=sha, evidence_sha256=sha,
                      evidence_status={'enum': ['synthetic', 'source_bound', 'unverified']}, physical_scale_verified=false))
    marker_props = dict(name=dict(type='string', maxLength=128), loot_table=uid, shop_catalog=uid, profile=uid,
                        route_id=uid, sequence=integer(), time_s=num(0), ground_snap={'type': 'boolean'})
    tactical = obj(dict(status={'enum': ['not_generated', 'generated']}, image=nullable(resource), width_px=integer(), height_px=integer(),
                        bounds_m=arr(num(-3200, 3200), 4, 4), pixel_to_world=arr(num(), 6, 6),
                        format={'enum': ['png_rgba8', 'png_rgb8', 'none']}, labels_baked=const(False)))
    world = header('wz09_runtime_world')
    world.update(world_kind={'enum': ['composite', 'single_map_overlay']}, source_status={'enum': ['synthetic', 'source_bound']}, runtime_ready=false,
                 maps=arr(obj(dict(id=uid, source_map={'enum': ['mp_afghan', 'mp_boneyard', 'mp_brecourt', 'mp_checkpoint', 'mp_derail',
                     'mp_estate', 'mp_favela', 'mp_highrise', 'mp_invasion', 'mp_nightshift', 'mp_quarry', 'mp_rundown',
                     'mp_rust', 'mp_subbase', 'mp_terminal', 'mp_underpass']}, position_m=v3, rotation_deg=v3,
                     source_units_to_meter=num(.025399, .025401), calibration=calibration)), 1, 256),
                 markers=arr(obj(dict(id=uid, kind={'enum': ['poi', 'loot_crate', 'buy_station', 'player_spawn', 'aircraft_keyframe',
                     'loadout_drop', 'ground_loot', 'warmup_spawn']}, map_instance_id=nullable(uid), position_m=v3, rotation_deg=v3,
                     properties=obj(marker_props, marker_props))), 0, 100000),
                 products=obj(dict(intro=resource, royale_layout=resource, terrain_collision=nullable(resource))), tactical_map=tactical)
    core = obj(dict(id=uid, map_instance_id=uid, polygon_local_m=polygon, z_range_m=arr(num(-3200, 3200), 2, 2),
                    position_m=v3, yaw_deg={'enum': [0, 90, 180, 270]}, boundary_heights_m=arr(num(), 3, 256),
                    ground_evidence_status={'enum': ['unverified', 'source_bound']}, boundary_height_tolerance_m=const(.1)))
    terrain = header('wz09_terrain_collision')
    terrain.update(source_status=const('editor_geometry_unverified'), collision_verified=false,
                   grid=dict(type='object', additionalProperties=False, properties=dict(columns=const(33), rows=const(33),
                       encoding=const('f32_le'), row_axis=const('+Y'), column_axis=const('+X'), heights=const('absolute_world_z_m')),
                       required=['columns', 'rows', 'encoding', 'row_axis', 'column_axis', 'heights']),
                   surface_table=const(['sand', 'dirt', 'grass', 'snow', 'rock', 'asphalt']),
                   hole_policy=const('conservative_cell_core_intersection'), native_boundary_verified=false,
                   collision_cores=arr(core, 0, 256),
                   tiles=arr(obj(dict(id=uid, tile=arr(integer(-100000, 100000), 2, 2), origin_m=v3,
                       cell_size_m=num(.001), height_samples=resource, hole_mask=resource, surface_mask=resource)), 1, 4096),
                   water_volumes=arr(obj(dict(id=uid, polygon_m=polygon, level_m=num(-3200, 3200), depth_m=num(.001),
                       walkable_depth_m=num(0), out_of_bounds_id=nullable(uid))), 0, 4096),
                   connectors=arr(obj(dict(id=uid, vertices=resource, indices=resource, surface_mask=resource,
                       vertex_count=integer(3, 65535), triangle_count=integer(1, 1000000),
                       vertex_encoding=const('f32_le_xyz'), index_encoding=const('u16_le'), winding=const('ccw'), collision_verified=false)), 0, 4096))
    result = {'runtime_resource': resource, **{'product_'+key: obj(value) for key, value in (
        ('wz09_intro_runtime', intro), ('wz09_royale_layout', royale), ('wz09_runtime_world', world), ('wz09_terrain_collision', terrain))}}
    # Optional within v1: legacy and synthetic sidecars remain byte-identical.
    result['product_wz09_royale_layout']['properties']['speed_mps'] = {'type': 'number', 'exclusiveMinimum': 0}
    result['product_wz09_intro_runtime']['properties']['screen_fades'] = arr(obj(dict(
        id=uid, source_event_id=uid, t_s=num(0, 3600), color=arr(num(0, 1), 3, 3),
        from_alpha=num(0, 1), to_alpha=num(0, 1), duration_s=num(.001, 3600))), 0, 10000)
    return result


def standalone(kind='wz09_runtime_world'):
    return {'$schema': 'https://json-schema.org/draft/2020-12/schema',
            '$id': 'https://wz2009.local/runtime-world-v1.schema.json',
            'title': 'KARVORSK runtime interchange v1 (not consumer approval)',
            '$defs': definitions(), '$ref': '#/$defs/product_'+kind}
