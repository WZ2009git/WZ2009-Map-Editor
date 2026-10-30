"""RUN17-kompakt: 3x3 km Karvorsk (Kleiner Huen, 19.10.2026).

Pure Python + numpy core (headless). Builds the compact plan from the RUN16
world (read only), rotates maps so their native water faces the shared water
net, generates the terrain (relief, droplet erosion, river/lake/fjord/sea
carving, beach blending, road flattening), scatters MW2 vegetation per biome,
places road props (lamps/guardrails/barriers) along roads and writes a NEW
export folder incl. collision extensions v2 sized to the new edges.
RUN16 files are never written.  Optional bpy part: import operator.
"""
import json, math, hashlib, struct, os, time, datetime, copy, zlib
import numpy as np

PRESET = 'RUN17-kompakt'
S = 1.2                    # 2.5 km plan -> 3 km
PLAY = 1500.               # playable half size (3x3 km = 9 km2)
HALF = 1800.               # terrain half size (300 m sea/mountain ring)
CELL = 8.
SEA = 0.

# core centre targets (2.5 km plan, scaled by S), yaw default, ground z, region
LAYOUT = {
 'mp_highrise': (0,120,0,8,'Stadt'), 'mp_nightshift': (260,160,0,4,'Stadt'),
 'mp_invasion': (-40,-170,0,3,'Stadt'), 'mp_checkpoint': (260,-150,0,3,'Stadt'),
 'mp_favela': (120,-480,0,6,'Stadt'), 'mp_rundown': (-330,-430,0,2,'Stadt'),
 'mp_underpass': (-360,-60,0,4,'Stadt'), 'mp_terminal': (560,-10,90,5,'Stadt'),
 'mp_estate': (-860,360,0,18,'Gruen'), 'mp_brecourt': (-860,-250,0,10,'Gruen'),
 'mp_derail': (-380,930,0,55,'Schnee'), 'mp_subbase': (420,980,0,8,'Schnee'),
 'mp_boneyard': (920,60,90,22,'Wueste'), 'mp_quarry': (900,470,0,30,'Wueste'),
 'mp_afghan': (930,-360,0,25,'Wueste'), 'mp_rust': (780,-760,0,12,'Wueste')}
# which water each map's own water should face
WATER_TARGET = {'mp_estate':'lake_estate','mp_rundown':'river','mp_underpass':'river',
    'mp_brecourt':'river','mp_favela':'sea','mp_subbase':'fjord'}
RESERVE = [('Reserve-1 Gruen-Sued', -1150, -820), ('Reserve-2 Schnee-West', -1150, 1250),
           ('Reserve-3 Wueste-Ost', 1300, 250), ('Reserve-4 Schnee-Ost', 1200, 1250),
           ('Reserve-5 Wueste-Sued', 1250, -700)]
RIVER = [(-650,1300,40),(-620,1050,34),(-660,760,24),(-760,640,20),(-620,450,16),(-560,200,10),
         (-560,-60,7),(-520,-300,5),(-420,-620,3),(-250,-800,1),(-120,-930,-2),(-80,-1050,-4)]
STREAM = [(-860,-90,9),(-700,-60,8),(-560,-60,7)]
LAKES = [('lake_estate',-780,650,120,80,20),('lake_glacier',-620,1180,90,60,40)]
FJORD = [(180,1500),(260,1180),(360,1130),(520,1140),(640,1220),(760,1500)]
SOUTH_SEA_Y = -880
ROADS = {
 'R1 West-Highway': [(-490,-60),(-700,-150),(-860,-90)],
 'R1b Estate-Zubringer': [(-700,-150),(-760,100),(-860,210)],
 'R2 Nordpass': [(0,240),(60,450),(200,700),(420,845)],
 'R2b Derail-Abzweig': [(60,450),(-50,800),(-230,900)],
 'R3 Wuestenstrasse': [(665,0),(805,20),(900,180),(900,355)],
 'R3b Afghan-Rust': [(805,20),(880,-150),(930,-255),(930,-465),(780,-690)],
 'R4 Kuestenstrasse': [(-330,-560),(-250,-800),(-100,-700),(120,-595),(500,-700),(715,-760)],
 'R5 Bergstrasse': [(900,585),(700,850),(550,960)],
 'R6 Stadtring': [(-230,50),(-110,260),(260,290),(455,135),(455,-160),(260,-290),(-40,-300),(-210,-300),(-230,50)]}
RIDGES = [('Nordkamm',[(-1500,680),(-900,720),(-500,640),(-150,700),(200,660),(650,720),(1500,700)],170,180,
           [(-50,740),(200,690),(-750,700)]),
          ('Wuesten-Abbruchkante',[(690,-420),(680,-640),(650,-900),(640,-1250)],40,120,[]),
          ('Westhuegel Estate',[(-1250,200),(-1150,500),(-1200,900)],70,250,[])]
BIOME_ASSETS = {
 'snow': dict(trees=['foliage_tree_pine_snow_lg_a','foliage_tree_pine_snow_lg_b','foliage_tree_pine_snow_tall','foliage_tree_pine_snow_sm_b','foliage_dead_pine_lg'],
              ground=['foliage_shrub_snow_1','foliage_shrub_snow_twigs_1','foliage_pacific_bushtree02_snow'], rocks=['prop_misc_rock_boulder_snow_03']),
 'desert': dict(trees=['foliage_red_pine_med','foliage_cod5_tree_jungle_02'],
              ground=['foliage_shrub_desertspikey','foliage_bush_sticks','foliage_drygrass_squareclump_mp','foliage_litedrygrass_smallsquareclump','foliage_shrub_brownmid'],
              rocks=['rock_tan_boulder_1','rock_tan_boulder_2','afch_bigrock_01']),
 'green': dict(trees=['foliage_red_pine_lg','foliage_tree_river_birch_lg_a','foliage_dead_pine_lg','foliage_bush_big_mp'],
              ground=['foliage_litegrass_squareclump','foliage_litedrygrass_multiclump','foliage_shrub_group01','foliage_cod5_tallgrass10a','foliage_poppy','foliage_shrub_brownshort'],
              rocks=['prop_misc_literock_small_01','prop_misc_literock_small_06']),
 'city': dict(trees=['foliage_tree_palm_med_1','foliage_tree_palm_bushy_3_static'],
              ground=['foliage_shrub_grayshort','foliage_litegrass_smallsquareclump'], rocks=[]),
 'beach': dict(trees=['foliage_pacific_palms08','foliage_tree_palm_tall_2_static'],
              ground=['foliage_pacific_grass_clump02','foliage_pacific_tropic_shrub03','foliage_pacific_fern02'], rocks=['prop_misc_literock_small_07'])}
ROAD_PROPS = dict(lamp='me_streetlightdual', lamp_alt='me_streetlight', guardrail='com_guardrail',
    barrier='me_concrete_barrier', desert=['bc_hesco_barrier_med','roadkill_cd_powerpole'],
    snow='snow_barrier_tall_1', sign='stopsign_01')

def sc(p): return tuple(v*S for v in p)
def scz(p): return (p[0]*S, p[1]*S, p[2])
def chash(v): return hashlib.sha256(json.dumps(v, sort_keys=True, separators=(',',':'), allow_nan=False).encode()).hexdigest()
def rot(v, deg):
    c, s = math.cos(math.radians(deg)), math.sin(math.radians(deg)); return (c*v[0]-s*v[1], s*v[0]+c*v[1])
def seg_dist(px, py, a, b):
    dx, dy = b[0]-a[0], b[1]-a[1]; L = dx*dx+dy*dy or 1e-9
    t = np.clip(((px-a[0])*dx+(py-a[1])*dy)/L, 0, 1); return np.hypot(px-a[0]-t*dx, py-a[1]-t*dy), t
def poly_dist(px, py, pts):
    best = np.full(np.shape(px), 1e9); tt = np.zeros(np.shape(px)); idx = np.zeros(np.shape(px))
    for i, (a, b) in enumerate(zip(pts, pts[1:])):
        d, t = seg_dist(px, py, a, b); m = d < best; best = np.where(m, d, best); tt = np.where(m, t, tt); idx = np.where(m, i, idx)
    return best, idx, tt
def smooth(x): x = np.clip(x, 0, 1); return x*x*(3-2*x)

# ---------------------------------------------------------------- plan
def water_net():
    river = [scz(p) for p in RIVER]; stream = [scz(p) for p in STREAM]
    lakes = [dict(id=n, center_m=[x*S, y*S], radii_m=[a*S, b*S], level_m=z) for n, x, y, a, b, z in LAKES]
    fjord = [sc(p) for p in FJORD] + [(760*S, HALF+50), (180*S, HALF+50)]
    return river, stream, lakes, fjord

def water_direction_target(kind, xy, river, lakes, fjord):
    x, y = xy
    if kind == 'sea': return (x, SOUTH_SEA_Y*S-60)
    if kind == 'fjord':
        cx = sum(p[0] for p in fjord[:6])/6; cy = min(p[1] for p in fjord[:6]); return (cx, cy+40)
    if kind.startswith('lake'):
        l = [l for l in lakes if l['id'] == kind][0]; return tuple(l['center_m'])
    d, i, t = poly_dist(np.array([x]), np.array([y]), [(p[0], p[1]) for p in river])
    a, b = river[int(i[0])], river[int(i[0])+1]; t = float(t[0]); return (a[0]+t*(b[0]-a[0]), a[1]+t*(b[1]-a[1]))

def make_plan(world, map_bounds, water_local):
    river, stream, lakes, fjord = water_net()
    src = {m['source_map']: m for m in world['maps']}
    rows = []
    for code, (x, y, yaw, gz, region) in LAYOUT.items():
        old = src[code]; b = map_bounds[code]
        core_local = ((b['p05_95_x'][0]+b['p05_95_x'][1])/2, -(b['p05_95_z'][0]+b['p05_95_z'][1])/2)
        size = (b['p05_95_x'][1]-b['p05_95_x'][0], b['p05_95_z'][1]-b['p05_95_z'][0])
        target = (x*S, y*S); new_yaw = float(yaw); why = 'Plan'
        kind = WATER_TARGET.get(code); w = water_local.get(code)
        if kind and w:
            v = (w[0]-core_local[0], w[1]-core_local[1])
            if math.hypot(*v) > 5:
                tx, ty = water_direction_target(kind, target, river, lakes, fjord)
                want = math.degrees(math.atan2(ty-target[1], tx-target[0])); have = math.degrees(math.atan2(v[1], v[0]))
                new_yaw = round(((want-have+180) % 360-180)/15.)*15.; why = 'Water -> '+kind
        src_rot = list(old.get('rotation_deg', [0, 0, 0])); src_pos = list(old['position_m'])
        # origin so that the rotated core centre lands on the target
        rc = rot(core_local, new_yaw); origin = [target[0]-rc[0], target[1]-rc[1], src_pos[2]+gz]
        # translated_world rotates about the source origin by (new-src) yaw; keep src frame consistent
        rows.append(dict(id=old['id'], source_map=code, region=region, core_center_m=[*target, gz],
            core_size_m=[round(size[0]), round(size[1])], core_local_m=list(core_local),
            source_position_m=src_pos, position_m=origin, source_rotation_deg=src_rot,
            rotation_deg=[src_rot[0], src_rot[1], new_yaw], yaw_reason=why))
    plan = dict(kind='wz09_karvorsk_landscape_plan', preset=PRESET, version=17, units='editor_design_meters',
        bounds_m=[-HALF, -HALF, HALF, HALF], play_bounds_m=[-PLAY, -PLAY, PLAY, PLAY],
        playable_km2=(2*PLAY/1000)**2, scale_from_25km_plan=S, maps=rows,
        reserve_slots=[dict(id=n, center_m=[x, y], size_m=[320, 320], status='frei fuer weitere Maps') for n, x, y in RESERVE],
        river=dict(points_m=[list(p) for p in river], width_m=32., bank_width_m=36., depth_m=6.),
        stream=dict(points_m=[list(p) for p in stream], width_m=10.), lakes=lakes,
        fjord_polygon_m=[list(p) for p in fjord], south_sea_y_m=SOUTH_SEA_Y*S, sea_level_m=SEA,
        roads={k: [list(sc(p)) for p in v] for k, v in ROADS.items()},
        ridges=[dict(name=n, points_m=[list(sc(p)) for p in pts], height_m=h, width_m=w*S, passes_m=[list(sc(p)) for p in ps]) for n, pts, h, w, ps in RIDGES],
        biomes=dict(snow_y_m=650*S, sand_x_m=700*S, city_center_m=[100*S, -150*S], city_radius_m=620*S),
        gameplay=dict(circle_centers_within_m=1200., flight=dict(start_m=[-PLAY-400, -PLAY-400, 900], end_m=[PLAY+400, PLAY+400, 900], speed_mps=70.),
            oob_timer_s=10, border_walls=True),
        collision_verified=False, runtime_integrated=False, physical_scale_verified=False)
    plan['overlaps'] = core_overlaps(rows)
    plan['sha256'] = chash(plan); return plan

def core_rect(r):
    w, h = r['core_size_m']; yaw = r['rotation_deg'][2]; cx, cy = r['core_center_m'][:2]
    return [(cx+p[0], cy+p[1]) for p in (rot((sx*w/2, sy*h/2), yaw) for sx, sy in ((-1,-1),(1,-1),(1,1),(-1,1)))]
def core_overlaps(rows, gap=40.):
    out = []
    for i, a in enumerate(rows):
        for b in rows[i+1:]:
            ra, rb = core_rect(a), core_rect(b)
            ax = (min(p[0] for p in ra), max(p[0] for p in ra)); ay = (min(p[1] for p in ra), max(p[1] for p in ra))
            bx = (min(p[0] for p in rb), max(p[0] for p in rb)); by = (min(p[1] for p in rb), max(p[1] for p in rb))
            dx = max(bx[0]-ax[1], ax[0]-bx[1]); dy = max(by[0]-ay[1], ay[0]-by[1]); d = max(dx, dy)
            if d < gap: out.append(dict(a=a['source_map'], b=b['source_map'], abstand_m=round(d, 1)))
    return out

# ---------------------------------------------------------------- terrain
def fbm(X, Y, seed, octaves=5, scale=420.):
    rng = np.random.default_rng(seed); z = np.zeros_like(X); amp = 1.
    for o in range(octaves):
        a, b, c = rng.uniform(0, 2*math.pi, 3); f = (2**o)/scale; th = rng.uniform(0, math.pi)
        u = X*math.cos(th)-Y*math.sin(th); v = X*math.sin(th)+Y*math.cos(th)
        z += amp*np.sin(u*f*6.283+a)*np.cos(v*f*6.283*0.83+b+np.sin(u*f*3.1+c)); amp *= .5
    return z

def droplet_erosion(H, n=40000, seed=17, steps=48, inertia=.08, capacity=6., deposit=.25, erode=.35, evap=.02, mask=None):
    """Classic particle hydraulic erosion (after H. Beyer 2015 / S. Lague, MIT)."""
    rng = np.random.default_rng(seed); R, C = H.shape
    for _ in range(n):
        x, y = rng.uniform(1, C-2), rng.uniform(1, R-2); dx = dy = 0.; v = 1.; w = 1.; sed = 0.
        for _ in range(steps):
            ix, iy = int(x), int(y); fx, fy = x-ix, y-iy
            if mask is not None and mask[iy, ix]: break
            h00, h10, h01, h11 = H[iy, ix], H[iy, ix+1], H[iy+1, ix], H[iy+1, ix+1]
            gx = (h10-h00)*(1-fy)+(h11-h01)*fy; gy = (h01-h00)*(1-fx)+(h11-h10)*fx
            h = h00*(1-fx)*(1-fy)+h10*fx*(1-fy)+h01*(1-fx)*fy+h11*fx*fy
            dx = dx*inertia-gx*(1-inertia); dy = dy*inertia-gy*(1-inertia); L = math.hypot(dx, dy)
            if L < 1e-9: break
            dx /= L; dy /= L; nx, ny = x+dx, y+dy
            if not (1 <= nx < C-2 and 1 <= ny < R-2): break
            jx, jy = int(nx), int(ny); gx2, gy2 = nx-jx, ny-jy
            nh = H[jy, jx]*(1-gx2)*(1-gy2)+H[jy, jx+1]*gx2*(1-gy2)+H[jy+1, jx]*(1-gx2)*gy2+H[jy+1, jx+1]*gx2*gy2
            dh = nh-h; cap = max(-dh, .01)*v*w*capacity
            if sed > cap or dh > 0:
                amt = min(dh, sed) if dh > 0 else (sed-cap)*deposit; sed -= amt
                H[iy, ix] += amt*(1-fx)*(1-fy); H[iy, ix+1] += amt*fx*(1-fy); H[iy+1, ix] += amt*(1-fx)*fy; H[iy+1, ix+1] += amt*fx*fy
            else:
                amt = min((cap-sed)*erode, -dh); sed += amt; H[iy, ix] -= amt
            v = math.sqrt(max(0., v*v+dh*-1.)); w *= (1-evap); x, y = nx, ny
    return H

def thermal(H, iters=20, talus=1.1*CELL, k=.25):
    for _ in range(iters):
        P = np.pad(H, 1, mode='edge'); dsum = np.zeros_like(H); moved = np.zeros_like(H)
        for oy, ox in ((0,1),(2,1),(1,0),(1,2)):
            d = H-P[oy:oy+H.shape[0], ox:ox+H.shape[1]]; m = np.where(d > talus, (d-talus)*k/4, 0); moved += m
            dsum += np.roll(np.roll(m, oy-1, 0), ox-1, 1)
        H = H-moved+dsum
    return H

def point_in_poly(X, Y, poly):
    inside = np.zeros(X.shape, bool); n = len(poly)
    for i in range(n):
        x1, y1 = poly[i]; x2, y2 = poly[(i+1) % n]
        c = ((y1 > Y) != (y2 > Y)) & (X < (x2-x1)*(Y-y1)/((y2-y1) or 1e-9)+x1); inside ^= c
    return inside

def build_terrain(plan, erosion_drops=40000):
    n = int(2*HALF/CELL)+1; ax = np.linspace(-HALF, HALF, n); X, Y = np.meshgrid(ax, ax)  # row = +Y
    H = 22+12*fbm(X, Y, 2009)+4*fbm(X, Y, 7, scale=140.)
    for r in plan['ridges']:
        d, _, _ = poly_dist(X, Y, r['points_m']); prof = np.exp(-2.5*(d/r['width_m'])**2)
        for p in r['passes_m']:
            prof *= 1-.75*np.exp(-((X-p[0])**2+(Y-p[1])**2)/(2*70.**2))
        H += r['height_m']*prof*(1+.25*fbm(X, Y, hash(r['name']) % 999, 3, 160.))
    # snow highlands and outer mountain ring (N/E/W), sea ring south
    H += 60*smooth((Y-plan['biomes']['snow_y_m'])/500.)
    ring = smooth((np.maximum(np.abs(X), Y)-PLAY+80)/380.)
    H += ring*(260+60*fbm(X, Y, 99, 4, 300.))
    cc = plan['biomes']['city_center_m']; H -= 6*np.exp(-((X-cc[0])**2+(Y-cc[1])**2)/(2*plan['biomes']['city_radius_m']**2))
    core = np.zeros(X.shape, bool)
    for r in plan['maps']: core |= point_in_poly(X, Y, core_rect(r))
    H = droplet_erosion(H, n=erosion_drops, mask=None); H = thermal(H)
    # coast south with noisy shoreline -> beach blend
    shore = plan['south_sea_y_m']+35*np.sin(X/170.)+18*np.sin(X/61.+1)+300*np.exp(-((X-150.)/260.)**2)  # Favela-Bucht
    below = smooth((shore-Y)/140.)
    beach_mask = (np.abs(Y-shore) < 45)
    H = H*(1-below)+(-14)*below
    H = np.where(beach_mask, np.minimum(H, 1.5+(Y-shore)*.04), H)
    # fjord
    fj = point_in_poly(X, Y, plan['fjord_polygon_m']); H = np.where(fj, -12, H)
    near = fj.astype(float)
    for _ in range(4):
        P = np.pad(near, 1, mode='edge'); near = np.maximum.reduce([near, P[:-2, 1:-1], P[2:, 1:-1], P[1:-1, :-2], P[1:-1, 2:]])
    for _ in range(3):
        P = np.pad(H, 1, mode='edge'); Hb = (P[:-2, 1:-1]+P[2:, 1:-1]+P[1:-1, :-2]+P[1:-1, 2:]+H)/5; H = np.where(near > 0, Hb, H)
    water = np.zeros(X.shape, float); wlevel = np.full(X.shape, np.nan)
    # river carving with monotone level and bank blend
    rp = plan['river']['points_m']; d, idx, t = poly_dist(X, Y, [p[:2] for p in rp])
    lvl = np.array([rp[int(i)][2] for i in idx.ravel()]).reshape(X.shape)*(1-t)+np.array([rp[int(i)+1][2] for i in idx.ravel()]).reshape(X.shape)*t
    hw = plan['river']['width_m']/2; bank = plan['river']['bank_width_m']
    bed = lvl-plan['river']['depth_m']*smooth(1-d/hw)
    bankh = lvl+1.0+(d-hw)*.08
    blend = smooth((d-hw)/(bank*2.5))
    H = np.where(d < hw, np.minimum(H, bed), np.where(d < hw+bank*2.5, np.minimum(H, bankh)*(1-blend)+H*blend, H))
    water = np.where(d < hw, 1, water); wlevel = np.where(d < hw, lvl, wlevel)
    sp = plan['stream']['points_m']; d2, i2, t2 = poly_dist(X, Y, [p[:2] for p in sp])
    l2 = 9-2*((i2+t2)/max(1, len(sp)-1))
    H = np.where(d2 < 5, np.minimum(H, l2-2), np.where(d2 < 30, np.minimum(H, l2+.5+(d2-5)*.1), H))
    water = np.where(d2 < 5, 1, water); wlevel = np.where(d2 < 5, l2, wlevel)
    for l in plan['lakes']:
        e = ((X-l['center_m'][0])/l['radii_m'][0])**2+((Y-l['center_m'][1])/l['radii_m'][1])**2
        H = np.where(e < 1, np.minimum(H, l['level_m']-5*(1-e)), np.where(e < 2.2, np.minimum(H, l['level_m']+1+(e-1)*6), H))
        water = np.where(e < 1, 1, water); wlevel = np.where(e < 1, l['level_m'], wlevel)
    low = (H < SEA+.3) & ~(water > 0); seed = np.zeros_like(low); seed[0, :] = True; seed |= fj; sea = seed & low
    for _ in range(600):
        P = np.pad(sea, 1); nxt = low & (sea | P[:-2, 1:-1] | P[2:, 1:-1] | P[1:-1, :-2] | P[1:-1, 2:])
        if (nxt == sea).all(): break
        sea = nxt
    H = np.where(low & ~sea, 1.5, H)  # inland pockets: fill, no stray puddles
    water = np.where(sea, 2, water); wlevel = np.where(sea, SEA, wlevel)
    # map pads: flatten to core ground z with 70 m falloff
    for r in plan['maps']:
        rc = core_rect(r); cx, cy, gz = r['core_center_m']; w, h = r['core_size_m']; yaw = math.radians(r['rotation_deg'][2])
        u = (X-cx)*math.cos(-yaw)-(Y-cy)*math.sin(-yaw); v = (X-cx)*math.sin(-yaw)+(Y-cy)*math.cos(-yaw)
        dd = np.maximum(np.abs(u)-w/2, np.abs(v)-h/2); f = smooth(1-dd/70.); f = np.where(dd < 0, 1, f)
        H = H*(1-f)+gz*f
    for s in plan['reserve_slots']:
        cx, cy = s['center_m']; dd = np.maximum(np.abs(X-cx), np.abs(Y-cy))-160; f = np.where(dd < 0, 1, smooth(1-dd/80.))
        pad = float(np.median(H[(np.abs(X-cx) < 160) & (np.abs(Y-cy) < 160)])); pad = max(pad, 4.); s['ground_m'] = round(pad, 1)
        H = H*(1-f)+pad*f
    roads = road_flatten(H, X, Y, plan)
    return dict(X=X, Y=Y, H=H, water=water, wlevel=wlevel, core=core, roads=roads, n=n)

def sample(H, x, y):
    n = H.shape[0]; fx = (x+HALF)/CELL; fy = (y+HALF)/CELL; ix = min(max(int(fx), 0), n-2); iy = min(max(int(fy), 0), n-2)
    ax, ay = fx-ix, fy-iy
    return float(H[iy, ix]*(1-ax)*(1-ay)+H[iy, ix+1]*ax*(1-ay)+H[iy+1, ix]*(1-ax)*ay+H[iy+1, ix+1]*ax*ay)

def resample(pts, step):
    out = [tuple(pts[0])]
    for a, b in zip(pts, pts[1:]):
        L = math.dist(a, b); k = max(1, int(L//step))
        for i in range(1, k+1): out.append((a[0]+(b[0]-a[0])*i/k, a[1]+(b[1]-a[1])*i/k))
    return out

def road_flatten(H, X, Y, plan, width=10., shoulder=14., max_grade=.10):
    info = {}
    for name, pts in plan['roads'].items():
        sp = resample(pts, 8.); z = [sample(H, x, y) for x, y in sp]
        z = np.convolve(np.pad(z, 6, mode='edge'), np.ones(13)/13, 'valid').tolist()
        bridges = []
        for i, (x, y) in enumerate(sp):  # bridge where the road crosses water
            if any(math.dist((x, y), b) < 30 for b in [sc(p[:2]) for p in ((-560,-110),(-250,-800))]): bridges.append(i)
        deck = max([9.]+[z[i] for i in bridges])
        for i in bridges: z[i] = deck
        lim = max_grade*8.; zz_ = np.array(z); idx_ = np.arange(len(z))
        for i in bridges: zz_ = np.maximum(zz_, deck-lim*np.abs(idx_-i))      # ramps up to bridge decks
        zz_ = np.min(zz_[None, :]+lim*np.abs(idx_[:, None]-idx_[None, :]), axis=1)  # Lipschitz cut: max 10 %
        z = zz_.tolist()
        grades = [abs(z[i+1]-z[i])/8. for i in range(len(z)-1)]
        for (x, y), zz in zip(sp, z):
            m = (np.abs(X-x) < width/2+shoulder+8) & (np.abs(Y-y) < width/2+shoulder+8)
            if not m.any(): continue
            d = np.hypot(X[m]-x, Y[m]-y); f = np.where(d < width/2, 1, smooth(1-(d-width/2)/shoulder))
            H[m] = H[m]*(1-f)+zz*f
        info[name] = dict(points=[[round(x, 2), round(y, 2), round(zz, 2)] for (x, y), zz in zip(sp, z)],
            max_grade_pct=round(100*max(grades), 1), bridge_samples=bridges, length_m=round(8*(len(sp)-1)))
    return info

# ---------------------------------------------------------------- scatter
def biome_at(plan, x, y, z, slope, wet):
    b = plan['biomes']
    if wet < 30 and z < 4: return 'beach'
    if y > b['snow_y_m'] or z > 160: return 'snow'
    if x > b['sand_x_m'] or (y < -400 and x > 650*S-60): return 'desert'
    if math.hypot(x-b['city_center_m'][0], y-b['city_center_m'][1]) < b['city_radius_m']: return 'city'
    return 'green'

def scatter_vegetation(plan, T, seed=2009, spacing=14.):
    rng = np.random.default_rng(seed); H = T['H']; out = []
    from_water = water_distance(T)
    roadpts = np.array([p[:2] for r in T['roads'].values() for p in r['points']])
    n = int((2*PLAY/spacing)**2)
    xs = rng.uniform(-PLAY, PLAY, n)+0; ys = rng.uniform(-PLAY, PLAY, n)
    dens = dict(snow=.35, desert=.12, green=.55, city=.04, beach=.25)
    for x, y in zip(xs, ys):
        z = sample(H, x, y); gx = sample(H, x+4, y)-sample(H, x-4, y); gy = sample(H, x, y+4)-sample(H, x, y-4)
        slope = math.hypot(gx, gy)/8.
        if z < .5 or slope > .7: continue
        ci = int((y+HALF)/CELL); cj = int((x+HALF)/CELL)
        if T['core'][ci, cj] or T['water'][ci, cj]: continue
        if len(roadpts) and np.min(np.hypot(roadpts[:, 0]-x, roadpts[:, 1]-y)) < 12: continue
        bio = biome_at(plan, x, y, z, slope, from_water[ci, cj])
        if rng.random() > dens[bio]*(1-slope): continue
        a = BIOME_ASSETS[bio]; r = rng.random()
        kind = 'trees' if r < .35 else ('rocks' if r < .45 and a['rocks'] else 'ground')
        if not a[kind]: kind = 'ground'
        out.append([a[kind][int(rng.integers(len(a[kind])))], round(x, 2), round(y, 2), round(z, 2), round(float(rng.uniform(0, 360)), 1), round(float(rng.uniform(.8, 1.25)), 2), bio])
    return out

def water_distance(T, maxd=200.):
    w = T['water'] > 0; d = np.where(w, 0., maxd)
    for _ in range(int(maxd/CELL)):
        P = np.pad(d, 1, mode='edge'); d = np.minimum(d, np.minimum.reduce([P[:-2, 1:-1], P[2:, 1:-1], P[1:-1, :-2], P[1:-1, 2:]])+CELL)
    return d

def scatter_props(plan, T, lamp_step=30., rail_step=4., slope_thr=.08):
    """Lamps every 30 m, guardrails/barriers every 4 m on slopes >8 % and on bridges, biome variants."""
    H = T['H']; out = []
    for name, r in T['roads'].items():
        pts = r['points']
        acc = 0.
        for i in range(len(pts)-1):
            a, b = pts[i], pts[i+1]; L = math.dist(a[:2], b[:2]) or 1; dx, dy = (b[0]-a[0])/L, (b[1]-a[1])/L; nx, ny = -dy, dx
            yaw = math.degrees(math.atan2(dy, dx)); grade = abs(b[2]-a[2])/L
            x, y = a[0], a[1]; bio = biome_at(plan, x, y, a[2], 0, 999)
            terr_side = max(abs(sample(H, x+nx*14, y+ny*14)-a[2]), abs(sample(H, x-nx*14, y-ny*14)-a[2]))/14.
            bridge = i in r['bridge_samples']
            acc += L
            if acc >= lamp_step:
                acc = 0.; side = 1 if (i//4) % 2 else -1
                asset = 'roadkill_cd_powerpole' if bio == 'desert' else (ROAD_PROPS['lamp'] if 'Stadt' in name or 'Highway' in name else ROAD_PROPS['lamp_alt'])
                out.append(dict(asset=asset, kind='lamp', road=name, position_m=[round(x+nx*7*side, 2), round(y+ny*7*side, 2), round(a[2], 2)], yaw_deg=round(yaw+90*side, 1)))
            if bridge or grade > slope_thr or terr_side > slope_thr*1.6:
                asset = ROAD_PROPS['guardrail'] if bio in ('green', 'city', 'beach') else (ROAD_PROPS['snow'] if bio == 'snow' else ('bc_hesco_barrier_med' if bio == 'desert' else ROAD_PROPS['barrier']))
                if bridge: asset = ROAD_PROPS['barrier']
                k = max(1, int(L//rail_step))
                for j in range(k):
                    t = j/k; px, py, pz = a[0]+(b[0]-a[0])*t, a[1]+(b[1]-a[1])*t, a[2]+(b[2]-a[2])*t
                    for side in (-1, 1):
                        out.append(dict(asset=asset, kind='rail', road=name, bridge=bridge, position_m=[round(px+nx*6*side, 2), round(py+ny*6*side, 2), round(pz, 2)], yaw_deg=round(yaw, 1)))
    return out

# ---------------------------------------------------------------- writers
def write_png16(path, A, lo, hi):
    q = np.clip((A-lo)/(hi-lo)*65535, 0, 65535).astype('>u2')[::-1]
    raw = b''.join(b'\x00'+row.tobytes() for row in q)
    def chunk(t, d): return struct.pack('>I', len(d))+t+d+struct.pack('>I', zlib.crc32(t+d) & 0xffffffff)
    open(path, 'wb').write(b'\x89PNG\r\n\x1a\n'+chunk(b'IHDR', struct.pack('>IIBBBBB', A.shape[1], A.shape[0], 16, 0, 0, 0, 0))+chunk(b'IDAT', zlib.compress(raw, 6))+chunk(b'IEND', b''))

def write_glb(path, T, step=1):
    H = T['H'][::step, ::step]; X = T['X'][::step, ::step]; Y = T['Y'][::step, ::step]; r, c = H.shape
    # glTF Y-up: (x, z, -y)
    pos = np.stack([X, H, -Y], -1).reshape(-1, 3).astype('<f4')
    i = np.arange(r*c).reshape(r, c); a = i[:-1, :-1].ravel(); b = i[:-1, 1:].ravel(); cc = i[1:, :-1].ravel(); d = i[1:, 1:].ravel()
    idx = np.stack([a, cc, b, b, cc, d], -1).ravel().astype('<u4')
    bin_ = pos.tobytes()+idx.tobytes()
    g = dict(asset=dict(version='2.0', generator='wz09 run17_kompakt'), scene=0, scenes=[dict(nodes=[0])],
        nodes=[dict(name='KARVORSK_RUN17_TERRAIN', mesh=0)], meshes=[dict(primitives=[dict(attributes=dict(POSITION=0), indices=1)])],
        buffers=[dict(byteLength=len(bin_))], bufferViews=[dict(buffer=0, byteOffset=0, byteLength=pos.nbytes, target=34962),
            dict(buffer=0, byteOffset=pos.nbytes, byteLength=idx.nbytes, target=34963)],
        accessors=[dict(bufferView=0, componentType=5126, count=len(pos), type='VEC3', min=pos.min(0).tolist(), max=pos.max(0).tolist()),
            dict(bufferView=1, componentType=5125, count=len(idx), type='SCALAR')])
    j = json.dumps(g).encode(); j += b' '*(-len(j) % 4)
    open(path, 'wb').write(struct.pack('<III', 0x46546C67, 2, 12+8+len(j)+8+len(bin_))+struct.pack('<II', len(j), 0x4E4F534A)+j+struct.pack('<II', len(bin_), 0x004E4942)+bin_)

def resource(job, payload, folder='collision_ext'):
    h = hashlib.sha256(payload).hexdigest(); os.makedirs(os.path.join(job, folder), exist_ok=True)
    p = os.path.join(job, folder, h+'.bin'); open(p, 'wb').write(payload)
    return dict(path=folder+'/'+h+'.bin', sha256=h, byte_length=len(payload))

def collision_ext(job, plan, T, world):
    H = T['H']; zmin = float(H.min()); P = PLAY; t = 16.
    walls = [dict(id='wall-'+s, side=s, solid=True, box_min_m=a, box_max_m=b) for s, a, b in (
        ('west', [-P-t, -P-t, zmin-60], [-P, P+t, 1500.]), ('east', [P, -P-t, zmin-60], [P+t, P+t, 1500.]),
        ('south', [-P-t, -P-t, zmin-60], [P+t, -P, 1500.]), ('north', [-P-t, P, zmin-60], [P+t, P+t, 1500.]))]
    catch = [dict(id='catch-below', kind='below_world', box_min_m=[-HALF, -HALF, zmin-1000], box_max_m=[HALF, HALF, zmin-50],
        action='teleport_to_safe_ground', safe_ground='nearest_heightfield_point')]
    for s, a, b in (('west', [-HALF, -HALF, zmin-1000], [-P-t, HALF, 3000]), ('east', [P+t, -HALF, zmin-1000], [HALF, HALF, 3000]),
                    ('south', [-HALF, -HALF, zmin-1000], [HALF, -P-t, 3000]), ('north', [-HALF, P+t, zmin-1000], [HALF, HALF, 3000])):
        catch.append(dict(id='catch-'+s, kind='outside_edge', box_min_m=a, box_max_m=b, action='teleport_to_safe_ground', safe_ground='clamp_into_play_bounds'))
    hf = resource(job, H.astype('<f4').tobytes())
    native = [m['id'] for m in world['maps'] if m['source_map'] == 'mp_invasion']
    doc = dict(kind='wz09_collision_extensions', schema_version=2, schema_minor=1, preset=PRESET, world_id='wz09_karvorsk',
        units='m', axis='world_z_up', play_bounds_m=plan['play_bounds_m'], native_collision_map_ids=native,
        base_terrain_collision=None,
        base_heightfield=dict(note='RUN17 replaces the v1 terrain tiles with continuous ground, including below all cities',
            origin_m=[-HALF, -HALF], cell_size_m=CELL, columns=T['n'], rows=T['n'], encoding='f32_le', row_axis='+Y', column_axis='+X',
            heights='absolute_world_z_m', height_samples=hf),
        hole_fill=dict(tiles=[], filled_cells=0, remaining_hole_cells=0), detail_patches=[], boundary_walls=walls, catch_volumes=catch,
        object_collision=[], open_mesh_warnings=[], collision_verified=False, runtime_integrated=False)
    b = json.dumps(doc, indent=1).encode(); h = hashlib.sha256(b).hexdigest(); os.makedirs(os.path.join(job, 'products'), exist_ok=True)
    open(os.path.join(job, 'products', h+'.json'), 'wb').write(b)
    return dict(path='products/'+h+'.json', sha256=h, byte_length=len(b)), doc

def translated_world(world, plan):
    """Same semantics as landscape_core.translated_world (RUN16 code untouched)."""
    res = dict(world); tf = {r['id']: r for r in plan['maps']}
    def tp(p, r):
        yaw = math.radians(r['rotation_deg'][2]-r['source_rotation_deg'][2]); c, s = math.cos(yaw), math.sin(yaw)
        x, y, z = [a-b for a, b in zip(p, r['source_position_m'])]; t = r['position_m']
        return [t[0]+c*x-s*y, t[1]+s*x+c*y, t[2]+z]
    for key in ('maps', 'markers', 'asset_placements', 'audio_zones', 'action_elements'):
        rows = []
        for row in world.get(key, []):
            row = dict(row); owner = row['id'] if key == 'maps' else (row.get('owner_map') or row.get('map_instance_id') or row.get('properties', {}).get('editor_owner_map'))
            if not (key == 'asset_placements' and row.get('placement_kind') == 'cinematic'):
                r = tf.get(owner)
                if r is not None and 'position_m' in row:
                    row['position_m'] = tp(row['position_m'], r)
                    if key == 'maps': row['rotation_deg'] = list(r['rotation_deg'])
                    elif 'rotation_deg' in row:
                        rr = list(row['rotation_deg']); rr[2] = (rr[2]+r['rotation_deg'][2]-r['source_rotation_deg'][2]+180) % 360-180; row['rotation_deg'] = rr
            rows.append(row)
        res[key] = rows
    nav = copy.deepcopy(world.get('nav', {}))
    for row in nav.get('nodes', []):
        r = tf.get(row.get('owner_map'))
        if r is not None: row['position_m'] = tp(row['position_m'], r)
    res['nav'] = nav; res['karvorsk_preset'] = PRESET
    return res

def build(world_path, map_bounds, water_local, exports_dir, erosion_drops=40000, stamp=None):
    t0 = time.time(); world = json.load(open(world_path, encoding='utf-8'))
    plan = make_plan(world, map_bounds, water_local)
    stamp = stamp or datetime.datetime.now().strftime('%Y%m%dT%H%M%S')
    job = os.path.join(exports_dir, 'karvorsk-landscape-RUN17-kompakt-'+stamp)
    if 'RUN16' in job or os.path.exists(job): raise FileExistsError(job)
    os.makedirs(job)
    T = build_terrain(plan, erosion_drops)
    veg = scatter_vegetation(plan, T); props = scatter_props(plan, T)
    plan['roads_built'] = {k: dict(max_grade_pct=v['max_grade_pct'], length_m=v['length_m'], bridges=len(v['bridge_samples'])) for k, v in T['roads'].items()}
    write_glb(os.path.join(job, 'generated-terrain.glb'), T)
    write_png16(os.path.join(job, 'heightmap_16bit.png'), T['H'], -20., 480.)
    open(os.path.join(job, 'heightfield_f32.raw'), 'wb').write(T['H'].astype('<f4').tobytes())
    np.save(os.path.join(job, 'water_mask.npy'), T['water'].astype('u1'))
    json.dump(dict(kind='wz09_vegetation_scatter', preset=PRESET, columns=['asset', 'x', 'y', 'z', 'yaw', 'scale', 'biome'], instances=veg,
        note='MW2 assets from native maps, loaded as linked instances'), open(os.path.join(job, 'vegetation_scatter.json'), 'w'))
    json.dump(dict(kind='wz09_road_props', preset=PRESET, props=props, roads={k: v['points'] for k, v in T['roads'].items()}), open(os.path.join(job, 'road_props.json'), 'w'))
    tw = translated_world(world, plan)
    json.dump(tw, open(os.path.join(job, 'world.json'), 'w', encoding='utf-8'))
    ref, doc = collision_ext(job, plan, T, world)
    summ = dict(boundary_walls=4, catch_volumes=len(doc['catch_volumes']), heightfield=f"{T['n']}x{T['n']} @ {CELL} m", play_bounds_m=plan['play_bounds_m'])
    json.dump(dict(kind='wz09_runtime_extensions', version=1, schema_version=2, schema_minor=1, preset=PRESET, world_id='wz09_karvorsk',
        runtime_world=None, world='world.json', products=dict(collision_extensions=ref), summary=summ), open(os.path.join(job, 'wz09_runtime_extensions.json'), 'w'), indent=1)
    plan['stats'] = dict(vegetation=len(veg), road_props=len(props), lamps=sum(p['kind'] == 'lamp' for p in props),
        terrain_z_min=round(float(T['H'].min()), 1), terrain_z_max=round(float(T['H'].max()), 1), seconds=round(time.time()-t0, 1))
    plan['sha256'] = chash({k: v for k, v in plan.items() if k != 'sha256'})
    json.dump(plan, open(os.path.join(job, 'landscape-plan.json'), 'w'), indent=1)
    return job, plan, T

if __name__ == '__main__':
    import sys
    a = dict(arg.split('=', 1) for arg in sys.argv[1:] if '=' in arg)
    job, plan, T = build(a['world'], json.load(open(a['bounds'])), json.load(open(a['water'])), a['exports'], int(a.get('drops', 40000)))
    print('RUN17_JOB', job); print(json.dumps(plan['stats'])); print('OVERLAPS', json.dumps(plan['overlaps']))
    print('ROADS', json.dumps(plan['roads_built'])); print('YAWS', json.dumps({r['source_map']: [r['rotation_deg'][2], r['yaw_reason']] for r in plan['maps']}))
