"""Source-bound authored ground-cut visual skirts; never collision or native edits.

Only explicit World-material selections and NEW core-plane cuts are admitted.
The upper envelope suppresses stacked source surfaces. Native UV/color/material
bindings are retained on separate derived meshes. Missing contacts stay reported.
"""
import copy
import hashlib
import json
import math
from pathlib import Path
import re
import struct
import uuid
try:
    from . import composite_world as clip,terrain_collision,preview_render
    from .source_bundle import verify_file
except ImportError:
    import composite_world as clip,terrain_collision,preview_render
    from source_bundle import verify_file

SETTINGS_KEY='wz09_edge_closure_settings_json'
MANIFEST_KEY='wz09_edge_closure_outputs_json'
ROLE='native_ground_cut_rim_visual_derived'
KIND='wz09_native_ground_cut_rim_settings'
PROFILE_KIND='wz09_native_cut_boundary_profile_plan'
PROFILE_KEY='wz09_core_boundary_profile_json'
# Exact authored World0 material selection, reviewed from bound source glTFs.
# This is a design whitelist, never a verified native ground classification.
DEFAULT_MATERIALS={'mp_afghan': ('wc/asphalt_grooved',
               'wc/dirt_cracked_01',
               'wc/dirt_rockycreekbed_01',
               'wc/dirt_sand_01',
               'wc/grass_dry_rocks01',
               'wc/me_ground_drygrass',
               'wc/rock_brown1',
               'wc/rock_brown2',
               'wc/rock_brown3',
               'wc/rock_cliffwall_1',
               'wc/rock_cliffwall_2',
               'wc/rock_cliffwall_3',
               'wc/rock_cliffwall_4'),
 'mp_boneyard': ('wc/asphalt_road', 'wc/dirt_cracked_dark', 'wc/grime_block_dirt_dc', 'wc/me_asphalt_01'),
 'mp_brecourt': ('wc/ch_asphalt01',
                 'wc/ch_grass_02',
                 'wc/ch_ground_gravel01',
                 'wc/ch_ground_mud01',
                 'wc/dirt_01',
                 'wc/me_ground_drygrass'),
 'mp_checkpoint': ('wc/ch_grass_01',
                   'wc/ch_ground_dirt03',
                   'wc/ch_ground_gravel02',
                   'wc/me_asphalt_01',
                   'wc/me_asphalt_road1',
                   'wc/me_ground_dirt03',
                   'wc/me_ground_drygrass',
                   'wc/rock_checkered_marble_tile',
                   'wc/rock_cliffwall_2'),
 'mp_derail': ('wc/asphalt_road',
               'wc/ch_asphalt01',
               'wc/snow_bright',
               'wc/snow_clean',
               'wc/snow_cliff_1',
               'wc/snow_cliff_ice_2',
               'wc/snow_icy_rockcliff_1',
               'wc/snow_pebbles',
               'wc/snow_rockfacewall_1',
               'wc/snow_rockwall',
               'wc/snow_soft2',
               'wc/snow_tracks_runway'),
 'mp_estate': ('wc/ch_ground_dirt03',
               'wc/ch_ground_gravel02',
               'wc/ch_ground_mud03',
               'wc/ch_ground_mud03_footprints',
               'wc/grass_dry01',
               'wc/grass_dry_rocks01',
               'wc/grass_spotted01',
               'wc/me_asphalt_chunky',
               'wc/rock_cliff',
               'wc/rock_cliffwall_1',
               'wc/rock_cliffwall_2',
               'wc/rock_pebbles',
               'wc/snow_cliff_2'),
 'mp_favela': ('wc/asphalt_road',
               'wc/asphalt_road_cracks',
               'wc/ct_asphalt_puddle1',
               'wc/dirt_cracked_01',
               'wc/grass_crabgrass',
               'wc/me_asphalt_chunky',
               'wc/me_ground_dirt02',
               'wc/rock_cliff',
               'wc/rock_cliffwall_2'),
 'mp_invasion': ('wc/asphalt_road',
                 'wc/ch_ground_dirt03',
                 'wc/dirt_cracked_01',
                 'wc/grass_crabgrass',
                 'wc/me_asphalt_01',
                 'wc/rock_brown2',
                 'wc/rock_brown3'),
 'mp_nightshift': ('wc/asphalt_road', 'wc/ch_ground_gravel01', 'wc/me_ground_concrete01'),
 'mp_quarry': ('wc/ch_ground_dirt02_footprint',
               'wc/me_asphalt_01',
               'wc/me_ground_sand1',
               'wc/rock_cliffwall_1'),
 'mp_rundown': ('wc/asphalt_road_cracks',
                'wc/ch_cliff01',
                'wc/ch_grass_02',
                'wc/ch_ground_dirt02',
                'wc/ch_ground_mud03',
                'wc/me_ground_dirt02',
                'wc/me_ground_dirt03',
                'wc/rock_checkered_marble_tile'),
 'mp_rust': ('wc/ch_ground_dirt03',
             'wc/dirt_01',
             'wc/dirt_cracked_02',
             'wc/dirt_sand_01',
             'wc/dirt_sand_pebbles',
             'wc/grass_dry_rocks01',
             'wc/me_ground_dirt02',
             'wc/rock_brown2',
             'wc/rock_brown4',
             'wc/rock_cliffwall_3',
             'wc/rock_pebbles'),
 'mp_subbase': ('wc/ch_ground_dirt03', 'wc/snow_clean', 'wc/snow_rockfacewall_1', 'wc/snow_soft2'),
 'mp_terminal': ('wc/asphalt_grooved',
                 'wc/ch_grass_01',
                 'wc/ch_ground_dirt03',
                 'wc/ch_ground_gravel01',
                 'wc/dirt_01',
                 'wc/me_ground_drygrass'),
 'mp_underpass': ('wc/asphalt_wet_road',
                  'wc/ch_ground_dirt3_footprint',
                  'wc/ch_ground_gravel01',
                  'wc/ch_ground_mud01',
                  'wc/ch_ground_mud03',
                  'wc/dirt_01',
                  'wc/grass_crabgrass',
                  'wc/me_ground_dirt03')}

def _hash(value):return hashlib.sha256(json.dumps(value,sort_keys=True,separators=(',',':'),allow_nan=False).encode()).hexdigest()
def _id(owner,region):return str(uuid.uuid5(uuid.NAMESPACE_URL,'wz09/'+ROLE+'/'+owner+'/'+region))
def _material(value):return re.sub(r'\.\d{3,}$','',value.name) if value else ''
def _owner(obj):
    while obj is not None:
        if obj.get('wz09_type')=='map_instance':return obj
        obj=obj.parent
    return None

def _source(obj):
    original=getattr(obj,'wz09_core_original_mesh',None) or obj.get('wz09_core_original_mesh')
    if original is not None:return original
    if obj.get('wz09_core_crop_status'):
        raise ValueError('Cropped World lacks its original mesh pointer; register the addon before loading')
    return obj.data


def _bindings(obj,mesh,cache,image_cache,indices=None):
    slots=list(mesh.materials)
    selected=range(len(slots)) if indices is None else sorted(indices)
    return [dict(index=i,name=_material(slots[i]),sha256=preview_render._material_digest(slots[i],cache,image_cache),
        object_binding=preview_render._material_digest(obj.material_slots[i].material,cache,image_cache)
        if i<len(getattr(obj,'material_slots',())) else None) for i in selected]


def _output_state(obj,cache,image_cache):
    return dict(id=str(obj.get('wz09_geometry_id')),map_id=str(obj.get('wz09_edge_closure_map_id')),
        core_id=str(obj.get('wz09_edge_closure_region_id')),mesh_sha256=preview_render._mesh_digest(obj.data),
        matrix=[list(r) for r in obj.matrix_world],material_slot_count=len(obj.data.materials),
        materials=_bindings(obj,obj.data,cache,image_cache,{p.material_index for p in obj.data.polygons}),
        layer_selection=preview_render._mesh_layer_selection(obj.data),
        custom_geometry=obj.get('wz09_custom_geometry'),role=obj.get('wz09_edge_closure_role'),
        provenance_sha256=_hash(json.loads(obj['wz09_edge_closure_provenance_json'])))


def _crosses_polygon(a,b,polygon):
    """Closed segment/convex-core intersection, including narrow intervening cores."""
    lo,hi=0.,1.
    for p,q in zip(polygon,polygon[1:]+polygon[:1]):
        start=(q[0]-p[0])*(a[1]-p[1])-(q[1]-p[1])*(a[0]-p[0])
        end=(q[0]-p[0])*(b[1]-p[1])-(q[1]-p[1])*(b[0]-p[0])
        delta=end-start
        if abs(delta)<1e-14:
            if start<-1e-8:return False
        elif delta>0:lo=max(lo,(-1e-8-start)/delta)
        else:hi=min(hi,(-1e-8-start)/delta)
        if lo>hi:return False
    return lo<=hi

def _world_objects(scene,owner,expected_materials=None):
    result=[]
    for obj in scene.objects:
        if obj.type!='MESH' or not obj.get('wz09_preview_only') or obj.get('wz09_custom_geometry') or _owner(obj)!=owner:continue
        p=obj;model=False
        while p is not None and p!=owner:
            if any(p.get(k) for k in ('wz09_type','wz09_ground_patch','wz09_region_kind','wz09_editor_overlay',
                    'wz09_water_json','wz09_asset_placement','wz09_asset_placement_id','wz09_asset_part',
                    'wz09_audio_zone','wz09_audio_contour','wz09_action_element','wz09_action_contour')):
                model=True;break
            meta=p.get('iw4l')
            if meta is not None and hasattr(meta,'get') and meta.get('model'):model=True;break
            p=p.parent
        if model:continue
        # Exact exporter World0 material table disambiguates native mesh objects
        # whose original exporter extras have no model metadata. No object names.
        candidate=getattr(obj,'wz09_core_original_mesh',None) or obj.get('wz09_core_original_mesh') or obj.data
        if expected_materials is not None and {_material(m) for m in candidate.materials if m}!=set(expected_materials):continue
        _source(obj)
        result.append(obj)
    if len(result)!=1:raise ValueError('Ground rims require exactly one metadata/material-bound native World mesh: '+str(owner.get('wz09_source_map'))+'; candidates='+str(len(result)))
    return result

def _active(scene):return [o for o in scene.objects if o.get('wz09_edge_closure_role')==ROLE and not o.get('wz09_archived_generation')]

def _configuration(scene,*,material_names=None):
    """Capture exact authored whitelist and current map/glTF bindings; no geometry."""
    names=DEFAULT_MATERIALS if material_names is None else material_names;maps=[]
    elevated=set(json.loads(scene.get('wz09_landscape_plan_json','{}')).get('elevated_map_ids',[]))
    for owner in sorted((o for o in scene.objects if o.get('wz09_type')=='map_instance'),key=lambda o:str(o['wz09_id'])):
        code=str(owner['wz09_source_map'])
        if code=='mp_highrise' or str(owner['wz09_id']) in elevated:continue
        if code not in names or not names[code]:raise ValueError('Explicit ground material whitelist missing: '+code)
        bundle=json.loads(owner['wz09_map_provenance_json'])['preview_bundle']
        path=verify_file(bundle['reference_path'],bundle['reference_sha256'],'Ground rim glTF')
        doc=json.loads(path.read_text(encoding='utf-8'));world=[n['mesh'] for n in doc['nodes'] if n.get('name')=='World' and 'mesh' in n]
        if world!=[0]:raise ValueError('Ground rim requires exact exporter World0 node')
        observed={doc['materials'][p['material']]['name'] for p in doc['meshes'][0]['primitives'] if 'material' in p}
        selected=sorted(set(names[code]))
        if set(selected)-observed:raise ValueError('Authored ground materials absent from exact World0: '+code)
        maps.append(dict(map_id=str(owner['wz09_id']),source_map=code,reference_path=str(path),reference_sha256=bundle['reference_sha256'],material_names=selected,world_mesh_index=0,
            selection_basis='authored_exact_World0_material_whitelist_not_semantic_ground_proof',
            world_material_names=sorted(observed),material_exceptions={'wc/rock_checkered_marble_tile':'authored_plaza_floor_not_verified_ground'}
            if 'wc/rock_checkered_marble_tile' in selected else {}))
    settings=dict(kind=KIND,version=1,maps=maps,minimum_up=.05,max_contact_m=150.,source_classification_unverified=True,collision_verified=False,physical_scale_verified=False)
    return settings

def configure(scene,*,material_names=None):
    settings=_configuration(scene,material_names=material_names)
    scene[SETTINGS_KEY]=json.dumps(settings,sort_keys=True,allow_nan=False)
    return settings

def _settings(scene,value=None):
    value=json.loads(scene[SETTINGS_KEY]) if value is None else copy.deepcopy(value)
    if (value.get('kind')!=KIND or type(value.get('version')) is not int or value['version']!=1
            or value.get('source_classification_unverified') is not True
            or value.get('collision_verified') is not False or value.get('physical_scale_verified') is not False):
        raise ValueError('Versioned visual-only ground rim settings required')
    for key,lo,hi in (('minimum_up',.05,1.),('max_contact_m',0.,150.)):
        v=value.get(key)
        if type(v) not in (int,float) or not math.isfinite(v) or not lo<=v<=hi:raise ValueError('Bounded finite ground rim setting required: '+key)
    if not isinstance(value.get('maps'),list) or not 1<=len(value['maps'])<=256:raise ValueError('Ground rim settings require bounded explicit map bindings')
    ids=[r['map_id'] for r in value['maps']]
    codes=[r['source_map'] for r in value['maps']]
    if len(ids)!=len(set(ids)) or len(codes)!=len(set(codes)) or any(r['source_map']=='mp_highrise' or not r['material_names'] for r in value['maps']):raise ValueError('Ground rim settings require unique non-elevated source maps')
    return value

def cut_segments(triangles,polygon,*,minimum_up=.05):
    """Clip selected source triangles and retain only genuinely new core cuts."""
    polygon=clip.convex_polygon(polygon);result=[]
    for record in triangles:
        payload=record['payload'];points=[p['co'] for p in payload]
        if sum(p['normal'][2] for p in payload)/3<minimum_up:continue
        signs=[];outside=False
        for a,b in zip(polygon,polygon[1:]+polygon[:1]):
            values=[(b[0]-a[0])*(p[1]-a[1])-(b[1]-a[1])*(p[0]-a[0]) for p in points]
            if max(values)<-1e-7:outside=True;break
            signs.append(values)
        if outside or all(min(v)>=-1e-7 for v in signs):continue
        seen=set()
        for tri in clip.clip_triangle(payload,polygon,_validated=True):
            for a,b in zip(tri,tri[1:]+tri[:1]):
                if math.dist(a['co'],b['co'])<1e-8:continue
                for edge,(p,q) in enumerate(zip(polygon,polygon[1:]+polygon[:1])):
                    dx,dy=q[0]-p[0],q[1]-p[1];l2=dx*dx+dy*dy
                    if min(signs[edge])>=-1e-7:continue
                    if max(abs(dx*(v['co'][1]-p[1])-dy*(v['co'][0]-p[0])) for v in (a,b))/math.sqrt(l2)>1e-6:continue
                    ts=[((v['co'][0]-p[0])*dx+(v['co'][1]-p[1])*dy)/l2 for v in (a,b)]
                    if ts[0]>ts[1]:a,b=b,a;ts.reverse()
                    key=(edge,*[round(t,12) for t in ts])
                    if key in seen or ts[1]-ts[0]<1e-10:continue
                    seen.add(key);result.append(dict(edge=edge,t0=ts[0],t1=ts[1],a=a,b=b,source=record['source']))
    return result

def upper_envelope(segments):
    """Exact piecewise-linear top selection within the explicit authored subset."""
    result=[]
    for edge in sorted({s['edge'] for s in segments}):
        rows=[s for s in segments if s['edge']==edge];events={max(0.,min(1.,s[k])) for s in rows for k in ('t0','t1')}
        def line(s):
            slope=(s['b']['co'][2]-s['a']['co'][2])/(s['t1']-s['t0']);return slope,s['a']['co'][2]-slope*s['t0']
        lines=[line(s) for s in rows]
        for i,a in enumerate(rows):
            for j in range(i):
                b=rows[j];den=lines[i][0]-lines[j][0]
                if abs(den)<1e-14:continue
                t=(lines[j][1]-lines[i][1])/den
                if max(a['t0'],b['t0'])+1e-10<t<min(a['t1'],b['t1'])-1e-10:events.add(t)
        times=sorted(events)
        for lo,hi in zip(times,times[1:]):
            if hi-lo<1e-10:continue
            mid=(lo+hi)/2;hits=[(lines[i][0]*mid+lines[i][1],-i,s) for i,s in enumerate(rows) if s['t0']-1e-10<=mid<=s['t1']+1e-10]
            if not hits:continue
            selected=max(hits,key=lambda x:(x[0],x[1]))[2]
            point=lambda t:clip._interpolate(selected['a'],selected['b'],(t-selected['t0'])/(selected['t1']-selected['t0']))
            result.append(dict(edge=edge,t0=lo,t1=hi,a=point(lo),b=point(hi),source=selected['source']))
    return result

def boundary_profile(polygon,fallback_heights,envelope,*,max_points=48,max_error_m=2.,min_spacing_m=1.):
    """Adaptive double-precision boundary heights; original XY corners survive.

    Exact piecewise-linear residuals cover all source endpoints and intervening
    controls. Uncovered intervals are explicitly unverified design fallback.
    Neither the selected materials nor this fit prove semantic native ground.
    """
    points=[list(p) for p in clip.convex_polygon(polygon)];n=len(points)
    if points!=[list(p) for p in polygon]:raise ValueError('Original CCW corner order required')
    if len(fallback_heights)!=n or any(type(z) not in (int,float) or not math.isfinite(z) for z in fallback_heights):raise ValueError('Finite fallback heights required')
    if type(max_points) is not int or not n<=max_points<=64:raise ValueError('Point budget must retain original corners and be at most64')
    if type(max_error_m) not in (int,float) or not math.isfinite(max_error_m) or not 0<=max_error_m<=100:raise ValueError('Error budget0..100 required')
    if type(min_spacing_m) not in (int,float) or not math.isfinite(min_spacing_m) or not .01<=min_spacing_m<=48:raise ValueError('Control spacing.01..48 required')
    lengths=[math.dist(a,b) for a,b in zip(points,points[1:]+points[:1])];rows=[]
    for r in envelope:
        e,lo,hi=r.get('edge'),r.get('t0'),r.get('t1')
        if type(e) is not int or not 0<=e<n or any(type(t) not in (int,float) or not math.isfinite(t) for t in (lo,hi)) or not -1e-8<=lo<hi<=1+1e-8:raise ValueError('Invalid envelope interval')
        z0,z1=r['a']['co'][2],r['b']['co'][2]
        if any(type(z) not in (int,float) or not math.isfinite(z) for z in (z0,z1)):raise ValueError('Finite envelope height required')
        rows.append(dict(edge=e,t0=max(0.,lo),t1=min(1.,hi),z0=float(z0),z1=float(z1),source=copy.deepcopy(r.get('source',{}))))
    rows.sort(key=lambda r:(r['edge'],r['t0'],r['t1']))
    if any(a['edge']==b['edge'] and a['t1']>b['t0']+1e-9 for a,b in zip(rows,rows[1:])):raise ValueError('Nonoverlapping upper envelope required')
    def line(r,t):return r['z0']+(r['z1']-r['z0'])*(t-r['t0'])/(r['t1']-r['t0'])
    def observations(e,t):
        result=[(line(r,t),r['source']) for r in rows if r['edge']==e and r['t0']-1e-10<=t<=r['t1']+1e-10]
        if t==0:result.extend((r['z1'],r['source']) for r in rows if r['edge']==(e-1)%n and r['t1']>=1-1e-10)
        return result
    controls={}
    for e in range(n):
        hits=observations(e,0.)
        controls[(e,0.)]=dict(edge=e,t=0.,height_m=max(z for z,_ in hits) if hits else float(fallback_heights[e]),original_corner=e,
            basis='SELECTED_SOURCE_CUT_UNVERIFIED_GROUND' if hits else 'EXISTING_DESIGN_FALLBACK',sources=[s for _,s in hits])
    def at(e,t,values):
        local=sorted((c['t'],c['height_m']) for c in values.values() if c['edge']==e)+[(1.,values[((e+1)%n,0.)]['height_m'])]
        for (a,za),(b,zb) in zip(local,local[1:]):
            if a-1e-10<=t<=b+1e-10:return za+(zb-za)*max(0.,min(1.,(t-a)/(b-a)))
        raise ValueError('Profile query escaped edge')
    def errors(values):
        probes=[];square=0.;length=0.
        for r in rows:
            ts=sorted({r['t0'],r['t1']}|{c['t'] for c in values.values() if c['edge']==r['edge'] and r['t0']<c['t']<r['t1']})
            residual=[at(r['edge'],t,values)-line(r,t) for t in ts]
            probes.extend(dict(error=abs(z),edge=r['edge'],t=t) for t,z in zip(ts,residual))
            for lo,hi,a,b in zip(ts,ts[1:],residual,residual[1:]):
                span=(hi-lo)*lengths[r['edge']];length+=span;square+=span*(a*a+a*b+b*b)/3
        return sorted(probes,key=lambda p:(-p['error'],p['edge'],p['t'])),math.sqrt(square/length) if length else None
    old,_=errors({(e,0.):dict(edge=e,t=0.,height_m=z) for e,z in enumerate(fallback_heights)})
    while len(controls)<max_points:
        residual,_=errors(controls);chosen=None
        for p in residual:
            if p['error']<=max_error_m+1e-10:break
            e,t=p['edge'],p['t']
            if not 1e-10<t<1-1e-10:continue
            prior=[c['t'] for c in controls.values() if c['edge']==e]+[1.]
            if min(abs(t-v)*lengths[e] for v in prior)<min_spacing_m:continue
            chosen=p;break
        if chosen is None:break
        e,t=chosen['edge'],chosen['t'];hits=observations(e,t)
        controls[(e,t)]=dict(edge=e,t=t,height_m=max(z for z,_ in hits),original_corner=None,
            basis='SELECTED_SOURCE_CUT_UNVERIFIED_GROUND',sources=[s for _,s in hits])
    ordered=[controls[k] for k in sorted(controls)];output=[];heights=[];corners=[]
    for i,c in enumerate(ordered):
        e,t=c['edge'],c['t'];a,b=points[e],points[(e+1)%n]
        output.append(list(a) if t==0 else [a[j]+(b[j]-a[j])*t for j in (0,1)]);heights.append(c['height_m'])
        if c['original_corner'] is not None:corners.append(i)
    gaps=[];coverage=0.
    for e in range(n):
        cursor=0.
        for r in [x for x in rows if x['edge']==e]+[dict(t0=1.,t1=1.)]:
            if r['t0']>cursor+1e-10:gaps.append(dict(edge=e,t0=cursor,t1=r['t0'],status='DESIGN_FALLBACK_UNVERIFIED',
                existing_design_heights_m=[fallback_heights[e]*(1-t)+fallback_heights[(e+1)%n]*t for t in (cursor,r['t0'])]))
            coverage+=(r['t1']-r['t0'])*lengths[e];cursor=max(cursor,r['t1'])
    residual,rms=errors(controls);values=sorted(p['error'] for p in residual);maximum=values[-1] if values else None
    for gap in gaps:
        ts=[gap['t0'],(gap['t0']+gap['t1'])/2.,gap['t1']]
        gap.update(profile_t=ts,profile_heights_m=[at(gap['edge'],t,controls) for t in ts],
            profile_interpolation_basis='UNVERIFIED_INTERPOLATION_BETWEEN_BOUNDARY_CONTROLS',
            existing_design_heights_basis='HISTORICAL_REFERENCE_ONLY_NOT_APPLIED_PROFILE')
    return dict(kind='wz09_native_cut_boundary_profile',version=1,
        status='NO_SOURCE_COVERAGE' if not values else 'WITHIN_ERROR_BUDGET' if maximum<=max_error_m+1e-10 else 'ERROR_BUDGET_EXCEEDED',
        polygon=output,boundary_heights_m=heights,original_polygon=copy.deepcopy(points),original_corner_indices=corners,controls=ordered,
        source_gaps=gaps,source_coverage_fraction=coverage/sum(lengths),
        fit=dict(maximum_error_m=maximum,original_maximum_error_m=old[0]['error'] if old else None,
            p95_endpoint_error_m=values[int(.95*(len(values)-1))] if values else None,length_weighted_rms_error_m=rms,
            endpoint_comparisons=len(values),controls=len(controls),original_corners=n,max_points=max_points,
            max_error_m=float(max_error_m),min_spacing_m=float(min_spacing_m)),
        source_classification_unverified=True,physical_scale_verified=False,collision_verified=False,runtime_integrated=False)


def _source_cut_data(obj,source,row,region,*,attributes=True):
    """Shared source extraction: profile planner and existing visual skirts."""
    source.calc_loop_triangles();materials={i for i,m in enumerate(source.materials) if _material(m) in row['material_names']}
    triangles=[];normals=source.corner_normals;uvs=list(source.uv_layers) if attributes else []
    colors=[v for v in source.color_attributes if v.domain in ('POINT','CORNER')] if attributes else []
    normal_matrix=obj.matrix_world.to_3x3().inverted().transposed()
    for tri in source.loop_triangles:
        if tri.material_index not in materials:continue
        points=[tuple(obj.matrix_world@source.vertices[vi].co) for vi in tri.vertices]
        sides=[[(b[0]-a[0])*(p[1]-a[1])-(b[1]-a[1])*(p[0]-a[0]) for p in points]
            for a,b in zip(region['polygon'],region['polygon'][1:]+region['polygon'][:1])]
        if any(max(v)<-1e-7 for v in sides) or all(min(v)>=-1e-7 for v in sides):continue
        payload=[]
        for j,(vi,li) in enumerate(zip(tri.vertices,tri.loops)):
            payload.append(dict(co=points[j],normal=tuple((normal_matrix@normals[li].vector).normalized()),
                uvs={v.name:tuple(v.data[li].uv) for v in uvs},colors={v.name:tuple(v.data[li if v.domain=='CORNER' else vi].color) for v in colors},bary=tuple(float(j==k) for k in range(3))))
        triangles.append(dict(payload=payload,source=dict(preview_id=str(obj['wz09_preview_id']),polygon_index=tri.polygon_index,
            loop_triangle_index=tri.index,vertex_indices=list(tri.vertices),loop_indices=list(tri.loops),material_index=tri.material_index)))
    return triangles,materials,uvs,colors


def strip_mesh(segments,polygon,sample_height,*,forbidden=None,forbidden_segment=None,max_contact_m=150.):
    """Prefer vertical caps at actual terrain edges; bounded outward fallback."""
    vertices=[];payloads=[];faces=[];sources=[];missing=[];contacts=[];dropped=0
    forbidden=forbidden or (lambda x,y:False)
    for row in segments:
        p,q=polygon[row['edge']],polygon[(row['edge']+1)%len(polygon)];dx,dy=q[0]-p[0],q[1]-p[1];length=math.hypot(dx,dy);normal=(dy/length,-dx/length)
        count=max(1,math.ceil(math.dist(row['a']['co'],row['b']['co'])/6.))
        for part in range(count):
            a=clip._interpolate(row['a'],row['b'],part/count);b=clip._interpolate(row['a'],row['b'],(part+1)/count);target=None
            for distance in (0.,.25,.5,1.,2.,6.,12.,24.,72.,150.):
                if distance>max_contact_m:continue
                xy=[(v['co'][0]+normal[0]*distance,v['co'][1]+normal[1]*distance) for v in (a,b)]
                if any(forbidden(xy[0][0]+(xy[1][0]-xy[0][0])*t,xy[0][1]+(xy[1][1]-xy[0][1])*t) for t in (0.,.25,.5,.75,1.)):continue
                if forbidden_segment and any(forbidden_segment(start,end) for start,end in
                        ((a['co'],xy[0]),(b['co'],xy[1]),(xy[0],xy[1]),(a['co'],xy[1]),(b['co'],xy[0]))):continue
                heights=[sample_height(x,y) for x,y in xy]
                if any(z is None or not math.isfinite(z) for z in heights):continue
                target=[(x,y,z) for (x,y),z in zip(xy,heights)];contacts.append(distance);break
            if target is None:missing.append(dict(edge=row['edge'],source=row['source']));continue
            pa,pb=copy.deepcopy(a),copy.deepcopy(b);pa['co'],pb['co']=target
            for name in a['uvs']:
                u,v=a['uvs'][name],b['uvs'][name];du,dv=v[0]-u[0],v[1]-u[1];n=math.hypot(du,dv)
                if n>1e-12:
                    for foot,inner in ((pa,a),(pb,b)):
                        amount=math.dist(foot['co'],inner['co'])/96.
                        foot['uvs'][name]=(inner['uvs'][name][0]-dv/n*amount,inner['uvs'][name][1]+du/n*amount)
            quad=[a,pa,pb,b];offset=len(vertices)
            vertices.extend(tuple(v['co']) for v in quad);payloads.extend(quad)
            for f in ((0,1,2),(0,2,3)):
                original=[vertices[offset+i] for i in f];packed=[struct.unpack('<fff',struct.pack('<fff',*p)) for p in original]
                if terrain_collision._triangle_area2(*original)<1e-10 or terrain_collision._triangle_area2(*packed)<1e-10:dropped+=1;continue
                faces.append(tuple(offset+i for i in f));sources.append(row['source'])
    return dict(vertices=vertices,payloads=payloads,faces=faces,face_sources=sources,missing_contacts=missing,
        maximum_outward_contact_m=max(contacts,default=0.),dropped_degenerate_faces=dropped)

def _source_inputs(scene,settings,*,verify_resources=False):
    owners={str(o['wz09_id']):o for o in scene.objects if o.get('wz09_type')=='map_instance'}
    sources=[];material_cache={};image_cache={}
    for row in settings['maps']:
        owner=owners.get(row['map_id'])
        if owner is None or owner['wz09_source_map']!=row['source_map']:raise ValueError('Ground rim map binding changed; configure again')
        bundle=json.loads(owner['wz09_map_provenance_json'])['preview_bundle']
        if Path(bundle['reference_path']).resolve()!=Path(row['reference_path']).resolve() or bundle['reference_sha256']!=row['reference_sha256']:raise ValueError('Ground rim source binding changed')
        verify_file(row['reference_path'],row['reference_sha256'],'Ground rim current glTF')
        if verify_resources:
            try:from .source_bundle import verify_preview_bundle
            except ImportError:from source_bundle import verify_preview_bundle
            verify_preview_bundle(bundle)
        obj=_world_objects(scene,owner,row.get('world_material_names'))[0];mesh=_source(obj)
        indices={i for i,m in enumerate(mesh.materials) if _material(m) in row['material_names']}
        if {_material(mesh.materials[i]) for i in indices}!=set(row['material_names']):raise ValueError('Ground rim source material bindings changed')
        sources.append(dict(map_id=row['map_id'],matrix=[list(r) for r in owner.matrix_world],world_matrix=[list(r) for r in obj.matrix_world],
            preview_id=str(obj['wz09_preview_id']),mesh_sha256=preview_render._mesh_digest(mesh),
            selected_materials=_bindings(obj,mesh,material_cache,image_cache,indices),layer_selection=preview_render._mesh_layer_selection(mesh)))
    return owners,sources

def _inputs(scene,settings):
    try:from . import terrain_workflow
    except ImportError:import terrain_workflow
    regions,waters,terrain_settings=terrain_workflow._inputs(scene)
    owners,sources=_source_inputs(scene,settings)
    tiles=[]
    for obj in scene.objects:
        if obj.get('wz09_terrain_tile_json') and not obj.get('wz09_archived_generation'):
            record=json.loads(obj['wz09_terrain_tile_json'])
            if record['input_sha256']!=scene.get('wz09_terrain_input_sha256'):raise ValueError('Ground rim needs current generated terrain')
            tiles.append(dict(id=record['id'],mesh_sha256=preview_render._mesh_digest(obj.data),matrix=[list(r) for r in obj.matrix_world]))
    if not tiles:raise ValueError('Ground rims require generated terrain')
    fingerprint=_hash(dict(settings=settings,regions=regions,waters=waters,terrain_settings=terrain_settings,sources=sources,tiles=sorted(tiles,key=lambda r:r['id'])))
    return fingerprint,regions,waters,owners,sources

def _raw_core_state(obj):
    keys=('wz09_core_boundary_heights_local_json','wz09_core_boundary_heights_json','wz09_core_boundary_shape_sha',
        'wz09_core_ground_sample_json','wz09_core_confirmed','wz09_core_confirmed_sha','wz09_core_shape',
        'wz09_biome','wz09_transition_m')
    return dict(signature=clip._signature(obj),matrix=[list(r) for r in obj.matrix_world],
        properties_sha256=_hash({k:obj.get(k) for k in keys}))

def _core(scene,core_id,map_id):
    matches=[o for o in scene.objects if o.get('wz09_region_kind')=='map_core' and str(o.get('wz09_region_id'))==core_id
        and _owner(o) is not None and str(_owner(o).get('wz09_id'))==map_id]
    if len(matches)!=1:raise ValueError('Exactly one owned map/core binding required: '+core_id)
    return matches[0]

def _profile_inputs(scene,settings):
    # serialize_regions calls profile_region_record with its original raw record;
    # that hook never calls this function or serialize_regions again.
    records=clip.serialize_regions(scene);owners,sources=_source_inputs(scene,settings,verify_resources=True)
    bindings=[];regions={}
    for row in settings['maps']:
        owner=owners[row['map_id']]
        found=[r for r in records if r['map_instance_id']==row['map_id']]
        if len(found)!=1 or not found[0]['confirmed']:raise ValueError('One confirmed original core required per profile')
        record=found[0];obj=_core(scene,record['id'],row['map_id'])
        if obj.get(PROFILE_KEY):record=json.loads(obj[PROFILE_KEY])['binding']['original_record']
        regions[row['map_id']]=record
        bindings.append(dict(map_id=row['map_id'],core_id=record['id'],source_map=row['source_map'],
            core_state=_raw_core_state(obj),owner_matrix=[list(r) for r in owner.matrix_world],
            original_record=copy.deepcopy(record),original_record_sha256=_hash(record),
            preview_bundle_sha256=_hash(json.loads(owner['wz09_map_provenance_json'])['preview_bundle']),
            reference_path=row['reference_path'],reference_sha256=row['reference_sha256'],
            source_policy=copy.deepcopy(row),
            source_state=next(s for s in sources if s['map_id']==row['map_id'])))
    return _hash(dict(settings=settings,bindings=bindings)),owners,regions,bindings

def _computed_boundary_profile(scene,owner,row,record,*,max_points,max_error_m,min_spacing_m):
    obj=_world_objects(scene,owner,row.get('world_material_names'))[0];source=_source(obj)
    triangles,_,_,_=_source_cut_data(obj,source,row,record,attributes=False)
    cuts=cut_segments(triangles,record['polygon'],minimum_up=row.get('_minimum_up',.05));top=upper_envelope(cuts)
    profile=boundary_profile(record['polygon'],record.get('boundary_heights_m',[record['height_m']]*len(record['polygon'])),top,
        max_points=max_points,max_error_m=max_error_m,min_spacing_m=min_spacing_m)
    return profile,len(cuts),len(top)

def plan_boundary_profiles(ctx,*,settings=None,map_codes=None,max_points=48,max_error_m=2.,min_spacing_m=1.):
    """Read-only source/core planner. No terrain, mesh edit or settings write."""
    scene=getattr(ctx,'scene',ctx)
    policy=_settings(scene,settings) if settings is not None or scene.get(SETTINGS_KEY) else _configuration(scene)
    if map_codes is not None:
        wanted=set(map_codes)
        if not wanted or wanted-{'mp_highrise'}-{r['source_map'] for r in policy['maps']}:raise ValueError('Unknown profile source map')
        policy=copy.deepcopy(policy);policy['maps']=[r for r in policy['maps'] if r['source_map'] in wanted]
    if not policy['maps']:raise ValueError('No non-elevated profile maps selected')
    before,owners,regions,bindings=_profile_inputs(scene,policy);outputs=[]
    for row,binding in zip(policy['maps'],bindings):
        owner=owners[row['map_id']];record=regions[row['map_id']]
        profile,cuts,top=_computed_boundary_profile(scene,owner,dict(row,_minimum_up=policy['minimum_up']),record,
            max_points=max_points,max_error_m=max_error_m,min_spacing_m=min_spacing_m)
        outputs.append(dict(map_id=row['map_id'],core_id=record['id'],source_map=row['source_map'],binding=binding,
            cut_segments=cuts,upper_envelope_segments=top,profile=profile))
    if _profile_inputs(scene,policy)[0]!=before:raise ValueError('Source/core changed while planning boundary profiles')
    result=dict(kind=PROFILE_KIND,version=1,status='PLANNED_SOURCE_BOUND_EDITOR_DESIGN',settings=policy,input_sha256=before,profiles=outputs,
        source_classification_unverified=True,collision_verified=False,physical_scale_verified=False,runtime_integrated=False)
    result['sha256']=_hash(result);return result

def _profile_source_state(scene,owner,source_policy):
    # A direct source-only view avoids serialize_regions/_inputs recursion.
    # No persistent cache: UI edits and external bytes must be seen immediately.
    _,sources=_source_inputs(scene,dict(maps=[source_policy]),verify_resources=True)
    return sources[0]

def profile_region_record(obj,owner,record,*,scene=None):
    """Lazy serializer hook. Pure double XY from ORIGINAL edges; no recursion."""
    raw=obj.get(PROFILE_KEY)
    if not raw:return record
    value=json.loads(raw)
    if value.get('kind')!='wz09_bound_core_boundary_profile' or value.get('version')!=1 or value.get('sha256')!=_hash({k:v for k,v in value.items() if k!='sha256'}):
        raise ValueError('Invalid bound core boundary profile')
    binding=value['binding'];profile=value['profile']
    if (str(owner.get('wz09_id'))!=binding['map_id'] or owner.get('wz09_source_map')!=binding['source_map']
            or binding['source_map']=='mp_highrise' or record['id']!=binding['core_id']
            or _raw_core_state(obj)!=binding['core_state'] or [list(r) for r in owner.matrix_world]!=binding['owner_matrix']
            or _hash(record)!=binding['original_record_sha256']):raise ValueError('Boundary profile original core/map changed; replan')
    bundle=json.loads(owner['wz09_map_provenance_json'])['preview_bundle']
    if (_hash(bundle)!=binding['preview_bundle_sha256'] or Path(bundle['reference_path']).resolve()!=Path(binding['reference_path']).resolve()
            or bundle['reference_sha256']!=binding['reference_sha256']):raise ValueError('Boundary profile source binding changed')
    verify_file(binding['reference_path'],binding['reference_sha256'],'Boundary profile current glTF')
    if scene is None:
        scenes=getattr(obj,'users_scene',())
        if len(scenes)!=1:raise ValueError('Boundary profile needs an unambiguous owning scene')
        scene=scenes[0]
    if 'source_policy' not in binding or _profile_source_state(scene,owner,binding['source_policy'])!=binding['source_state']:
        raise ValueError('Boundary profile source World pose, mesh, materials or resource bytes changed; replan')
    controls=profile['controls'];n=len(record['polygon'])
    if (profile.get('kind')!='wz09_native_cut_boundary_profile' or profile.get('version')!=1
            or not n<=len(controls)<=64 or profile.get('source_classification_unverified') is not True
            or any(profile.get(k) is not False for k in ('physical_scale_verified','collision_verified','runtime_integrated'))):
        raise ValueError('Invalid profile control budget/basis')
    if profile['status']!='WITHIN_ERROR_BUDGET' and value.get('explicit_error_budget_override') is not True:raise ValueError('Boundary profile exceeds its declared error budget')
    original=[c.get('original_corner') for c in controls if c.get('original_corner') is not None]
    if original!=list(range(n)):raise ValueError('Boundary profile must preserve every original corner')
    local=[];world=[];heights=[];prior=(-1,-1.)
    for c in controls:
        edge,t,z=c['edge'],c['t'],c['height_m']
        if (type(edge) is not int or not 0<=edge<n or type(t) not in (int,float) or not math.isfinite(t) or not 0<=t<1
                or (edge,t)<=prior or type(z) not in (int,float) or not math.isfinite(z)):
            raise ValueError('Invalid ordered boundary control')
        if c.get('original_corner') is not None and (c['original_corner']!=edge or t!=0.):raise ValueError('Original profile corner moved')
        for key,out in (('polygon_local_m',local),('polygon',world)):
            a,b=record[key][edge],record[key][(edge+1)%n]
            out.append(list(a) if t==0. else [a[j]+(b[j]-a[j])*t for j in (0,1)])
        heights.append(z);prior=(edge,t)
    if world!=profile['polygon'] or heights!=profile['boundary_heights_m']:raise ValueError('Profile XY/height payload differs from original-edge controls')
    result=copy.deepcopy(record);result.update(polygon_local_m=local,polygon=world,boundary_heights_m=heights)
    result.pop('ground_capture',None)
    return result

def apply_boundary_profiles(ctx,plan,*,map_codes=None,require_tolerance=True):
    """Atomically bind private profile JSON; original meshes/properties unchanged."""
    scene=getattr(ctx,'scene',ctx)
    if type(require_tolerance) is not bool:raise ValueError('Explicit boolean tolerance policy required')
    if getattr(ctx,'mode','OBJECT')!='OBJECT':raise ValueError('Leave Edit Mode before applying boundary profiles')
    if (plan.get('kind')!=PROFILE_KIND or plan.get('version')!=1
            or plan.get('sha256')!=_hash({k:v for k,v in plan.items() if k!='sha256'})):
        raise ValueError('Sealed source-bound boundary profile plan required')
    settings=_settings(scene,plan['settings'])
    fingerprint,owners,regions,bindings=_profile_inputs(scene,settings)
    if fingerprint!=plan['input_sha256']:raise ValueError('Boundary profile plan is stale; replan')
    expected={(r['map_id'],r['source_map']):r for r in settings['maps']}
    rows=plan.get('profiles')
    if (not isinstance(rows,list) or len(rows)!=len(expected)
            or len({r['map_id'] for r in rows})!=len(rows) or len({r['core_id'] for r in rows})!=len(rows)
            or len({r['source_map'] for r in rows})!=len(rows)
            or {(r['map_id'],r['source_map']) for r in rows}!=set(expected)):
        raise ValueError('Boundary profile map/core set must be complete and unique')
    current_bindings={r['map_id']:r for r in bindings}
    if any(r['binding']!=current_bindings.get(r['map_id']) or r['core_id']!=current_bindings[r['map_id']]['core_id'] for r in rows):
        raise ValueError('Boundary profile map/core/source binding differs from actual inputs')
    wanted=set(map_codes) if map_codes is not None else {r['source_map'] for r in plan['profiles']}
    selected=[r for r in plan['profiles'] if r['source_map'] in wanted]
    if not selected or wanted-{r['source_map'] for r in selected}:raise ValueError('Unknown/empty boundary profile selection')
    prepared=[]
    for row in selected:
        if row['source_map']=='mp_highrise':raise ValueError('Highrise boundary profile is excluded')
        profile=row['profile']
        if profile['status']=='NO_SOURCE_COVERAGE':raise ValueError('No source coverage; no boundary profile may be applied')
        if require_tolerance and profile['status']!='WITHIN_ERROR_BUDGET':raise ValueError('Boundary profile error budget exceeded: '+row['source_map'])
        tuning=profile['fit'];policy=dict(expected[(row['map_id'],row['source_map'])],_minimum_up=settings['minimum_up'])
        computed,_,_=_computed_boundary_profile(scene,owners[row['map_id']],policy,regions[row['map_id']],
            max_points=tuning['max_points'],max_error_m=tuning['max_error_m'],min_spacing_m=tuning['min_spacing_m'])
        if computed!=profile:raise ValueError('Profile payload is not the recomputed source-envelope fit; checksum is not authorization')
        obj=_core(scene,row['core_id'],row['map_id'])
        value=dict(kind='wz09_bound_core_boundary_profile',version=1,binding=row['binding'],profile=profile,
            plan_sha256=plan['sha256'],explicit_error_budget_override=not require_tolerance)
        value['sha256']=_hash(value);raw=json.dumps(value,sort_keys=True,allow_nan=False)
        prepared.append((obj,raw,dict(core_id=row['core_id'],map_id=row['map_id'],previous=obj.get(PROFILE_KEY),
            applied_sha256=hashlib.sha256(raw.encode()).hexdigest()),row['binding']['original_record']))
    try:
        for obj,raw,_,record in prepared:
            obj[PROFILE_KEY]=raw;profile_region_record(obj,_owner(obj),record,scene=scene)
        if _profile_inputs(scene,settings)[0]!=plan['input_sha256']:raise ValueError('Original source/core changed during profile binding')
    except Exception:
        for obj,_,entry,_ in prepared:
            if entry['previous'] is None:
                if PROFILE_KEY in obj:del obj[PROFILE_KEY]
            else:obj[PROFILE_KEY]=entry['previous']
        raise
    return dict(kind='wz09_boundary_profile_apply',status='APPLIED_BOUNDARY_PROFILE_METADATA',applied=len(prepared),
        restore_token=dict(entries=[r[2] for r in prepared]),source_meshes_unchanged=True,core_meshes_unchanged=True)

def restore_boundary_profiles(ctx,restore_token):
    """Restore only the exact metadata written by the corresponding apply."""
    scene=getattr(ctx,'scene',ctx);pending=[]
    entries=restore_token['entries']
    if not entries or len({(r['core_id'],r['map_id']) for r in entries})!=len(entries):raise ValueError('Unique nonempty profile restore bindings required')
    for entry in entries:
        obj=_core(scene,entry['core_id'],entry['map_id']);raw=obj.get(PROFILE_KEY)
        if not isinstance(raw,str) or hashlib.sha256(raw.encode()).hexdigest()!=entry['applied_sha256']:
            raise ValueError('Boundary metadata changed after apply; refusing stale restore')
        pending.append((obj,entry,raw))
    try:
        for obj,entry,_ in pending:
            if entry['previous'] is None:del obj[PROFILE_KEY]
            else:obj[PROFILE_KEY]=entry['previous']
    except Exception:
        for obj,_,raw in pending:obj[PROFILE_KEY]=raw
        raise
    return len(pending)

def validate_current(scene):
    """Legacy no-op; active outputs without config or stale inputs refuse export."""
    active=_active(scene)
    if not scene.get(SETTINGS_KEY):
        if active:raise ValueError('Ground rim outputs lack their source settings')
        return None
    settings=_settings(scene);fingerprint,regions,_,_,_=_inputs(scene,settings)
    if scene.get('wz09_edge_closure_input_sha256')!=fingerprint or not active:raise ValueError('Ground rims stale: regenerate terrain/edge closures')
    if any(o.get('wz09_edge_closure_input_sha256')!=fingerprint for o in active):raise ValueError('Ground rim output fingerprint differs')
    manifest=json.loads(scene.get(MANIFEST_KEY,'{}'))
    if manifest.get('status')!='COMPLETE' or manifest.get('input_sha256')!=fingerprint:raise ValueError('Ground rim outputs need complete saved manifest')
    expected={_id(row['map_id'],next(r['id'] for r in regions if r['map_instance_id']==row['map_id'])) for row in settings['maps']}
    ids=[str(o.get('wz09_geometry_id')) for o in active]
    if len(ids)!=len(expected) or set(ids)!=expected:raise ValueError('Ground rim output set differs from configured maps/cores')
    cache={};images={};states=sorted((_output_state(o,cache,images) for o in active),key=lambda r:r['id'])
    if manifest.get('outputs')!=states:raise ValueError('Ground rim geometry, pose, layer or material changed; rebuild')
    return fingerprint

def remove(ctx):
    """Archive only this tool's generated geometry; original/native data untouched."""
    scene=getattr(ctx,'scene',ctx);active=_active(scene)
    for obj in active:
        if obj.get('wz09_geometry_id')!=_id(str(obj.get('wz09_edge_closure_map_id')),str(obj.get('wz09_edge_closure_region_id'))):raise ValueError('Ground rim role/ID binding differs; refusing removal')
    for obj in active:
        obj['wz09_archived_generation']=True;obj['wz09_custom_geometry']=False;obj.hide_render=True;obj.hide_set(True)
    for key in (SETTINGS_KEY,'wz09_edge_closure_input_sha256',MANIFEST_KEY):
        if key in scene:del scene[key]
    return len(active)

def rebuild(ctx,*,settings=None,map_codes=None):
    """Build separate source-bound visual skirts atomically; no shader/source edit."""
    import bpy
    from mathutils import Vector
    scene=ctx.scene;settings=_settings(scene,settings);fingerprint,regions,waters,owners,source_state=_inputs(scene,settings)
    selected=[r for r in settings['maps'] if map_codes is None or r['source_map'] in set(map_codes)]
    if not selected:raise ValueError('No configured maps selected for rim build')
    samplers=[]
    for obj in scene.objects:
        if not obj.get('wz09_terrain_tile_json') or obj.get('wz09_archived_generation'):continue
        record=json.loads(obj['wz09_terrain_tile_json']);width=record['cells']*record['cell_size_m'];x,y=record['tile_x'],record['tile_y'];obj.data.calc_loop_triangles()
        plan=dict(bounds_m=[x*width,y*width,(x+1)*width,(y+1)*width],cell_size_m=record['cell_size_m'],vertices=[tuple(obj.matrix_world@v.co) for v in obj.data.vertices],faces=[tuple(t.vertices) for t in obj.data.loop_triangles])
        samplers.append((plan['bounds_m'],terrain_collision._mesh_sampler(plan)))
    def height(x,y):
        hits=[fn(x,y) for box,fn in samplers if box[0]-1e-6<=x<=box[2]+1e-6 and box[1]-1e-6<=y<=box[3]+1e-6];hits=[z for z in hits if z is not None]
        if hits and max(hits)-min(hits)>1e-4:raise ValueError('Actual terrain contact has inconsistent shared heights')
        return hits[0] if hits else None
    created=[];reports=[];old=[];old_states=[]
    saved_scene={k:scene.get(k) for k in (SETTINGS_KEY,'wz09_edge_closure_input_sha256',MANIFEST_KEY)}
    try:
        for row in selected:
            owner=owners[row['map_id']];verify_file(row['reference_path'],row['reference_sha256'],'Ground rim source before build')
            region=next((r for r in regions if r['map_instance_id']==row['map_id'] and r.get('kind')=='map_core'),None)
            if region is None:region=next((r for r in regions if r['map_instance_id']==row['map_id']),None)
            if region is None or not region['confirmed']:raise ValueError('Confirmed map core required for rim derivation')
            obj=_world_objects(scene,owner,row.get('world_material_names'))[0];source=_source(obj);policy=preview_render._derived_mesh_layer_selection(source)
            triangles,materials,uvs,colors=_source_cut_data(obj,source,row,region)
            cuts=cut_segments(triangles,region['polygon'],minimum_up=settings['minimum_up']);top=upper_envelope(cuts)
            cut_material_counts={_material(source.materials[i]):sum(c['source']['material_index']==i for c in cuts)
                for i in sorted(materials)}
            edge_coverage=[dict(edge=i,fraction=sum(c['t1']-c['t0'] for c in top if c['edge']==i))
                for i in range(len(region['polygon']))]
            other=[r['polygon'] for r in regions if r['map_instance_id']!=row['map_id']]
            def forbidden(x,y):return any(clip.point_inside((x,y),p,_validated=True) for p in other)
            def forbidden_segment(a,b):return any(_crosses_polygon(a,b,p) for p in other)
            mesh_plan=strip_mesh(top,region['polygon'],height,forbidden=forbidden,forbidden_segment=forbidden_segment,max_contact_m=settings['max_contact_m'])
            if not mesh_plan['faces']:raise ValueError('No supported visual ground-cut closure faces: '+row['source_map'])
            uid=_id(row['map_id'],region['id']);mesh=bpy.data.meshes.new('Native ground cut rim · visual derived');mesh.from_pydata(mesh_plan['vertices'],[],mesh_plan['faces']);mesh.update()
            derived=bpy.data.objects.new('Ground cut rim · '+row['source_map'],mesh);created.append(derived)
            for mat in source.materials:mesh.materials.append(mat)
            new_uvs={v.name:mesh.uv_layers.new(name=v.name) for v in uvs};new_colors={v.name:mesh.color_attributes.new(name=v.name,type='FLOAT_COLOR',domain='CORNER') for v in colors}
            for polygon,face_source in zip(mesh.polygons,mesh_plan['face_sources']):
                polygon.material_index=face_source['material_index']
                for li,vi in zip(polygon.loop_indices,polygon.vertices):
                    payload=mesh_plan['payloads'][vi]
                    for name,layer in new_uvs.items():layer.data[li].uv=payload['uvs'][name]
                    for name,layer in new_colors.items():layer.data[li].color=payload['colors'][name]
            preview_render._apply_mesh_layer_selection(mesh,policy['resolved_selection'])
            provenance=dict(role=ROLE,map_id=row['map_id'],source_map=row['source_map'],core_id=region['id'],source_reference_path=row['reference_path'],source_reference_sha256=row['reference_sha256'],source_mesh_sha256=next(r['mesh_sha256'] for r in source_state if r['map_id']==row['map_id']),input_sha256=fingerprint,
                source_faces=sorted({(v['polygon_index'],v['loop_triangle_index']) for v in mesh_plan['face_sources']}),selected_material_names=row['material_names'],selection_basis=row.get('selection_basis','explicit_authored_material_selection_not_semantic_ground_proof'),material_exceptions=row.get('material_exceptions',{}),layer_selection=policy,source_classification_unverified=True,collision_verified=False,navigation_verified=False,physical_scale_verified=False,runtime_integrated=False,
                cut_segments=len(cuts),cut_material_counts=cut_material_counts,edge_coverage=edge_coverage,
                upper_envelope_segments=len(top),generated_triangles=len(mesh_plan['faces']),missing_contacts=len(mesh_plan['missing_contacts']),maximum_outward_contact_m=mesh_plan['maximum_outward_contact_m'],dropped_degenerate_faces=mesh_plan['dropped_degenerate_faces'],uv_extension='source_boundary_uv_perpendicular_strip_distance_divided_by96',source_mesh_modified=False)
            derived['wz09_custom_geometry']=True;derived['wz09_geometry_id']=uid;derived['wz09_geometry_kind']='custom';derived['wz09_edge_closure_role']=ROLE;derived['wz09_edge_closure_map_id']=row['map_id'];derived['wz09_edge_closure_region_id']=region['id'];derived['wz09_edge_closure_input_sha256']=fingerprint;derived['wz09_edge_closure_provenance_json']=json.dumps(provenance,sort_keys=True,allow_nan=False)
            mesh['wz09_edge_closure_owned']=True;reports.append(dict(id=uid,**provenance))
        if _inputs(scene,settings)[0]!=fingerprint:raise ValueError('Ground rim inputs changed while deriving outputs')
        old=[o for o in _active(scene) if o.get('wz09_edge_closure_map_id') in {r['map_id'] for r in selected}]
        old_states=[(o,o.get('wz09_archived_generation'),o.get('wz09_custom_geometry'),o.hide_render,o.hide_get()) for o in old]
        for obj in created:scene.collection.objects.link(obj)
        for obj in old:obj['wz09_archived_generation']=True;obj['wz09_custom_geometry']=False;obj.hide_render=True;obj.hide_set(True)
        scene[SETTINGS_KEY]=json.dumps(settings,sort_keys=True,allow_nan=False);scene['wz09_edge_closure_input_sha256']=fingerprint
        cache={};images={};states=sorted((_output_state(o,cache,images) for o in _active(scene)),key=lambda r:r['id'])
        complete=map_codes is None and len(selected)==len(settings['maps'])
        scene[MANIFEST_KEY]=json.dumps(dict(version=1,status='COMPLETE' if complete else 'PARTIAL',input_sha256=fingerprint,outputs=states),sort_keys=True,allow_nan=False)
        return dict(kind='wz09_native_ground_cut_rim_visual_build',version=1,status='DERIVED_VISUAL_ONLY' if complete else 'PARTIAL_DERIVED_VISUAL_ONLY',maps=len(reports),input_sha256=fingerprint,outputs=reports,source_meshes_unchanged=True,collision_verified=False,physical_scale_verified=False)
    except Exception:
        for obj,archived,custom,render,hidden in old_states:
            for key,value in (('wz09_archived_generation',archived),('wz09_custom_geometry',custom)):
                if value is None:
                    if key in obj:del obj[key]
                else:obj[key]=value
            obj.hide_render=render;obj.hide_set(hidden)
        for key,value in saved_scene.items():
            if value is None:
                if key in scene:del scene[key]
            else:scene[key]=value
        for obj in created:
            mesh=obj.data;bpy.data.objects.remove(obj,do_unlink=True)
            if mesh.users==0:bpy.data.meshes.remove(mesh)
        raise
