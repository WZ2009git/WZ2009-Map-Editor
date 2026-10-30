"""Global editable height grid over the existing TerrainField; no native edits."""
import copy
import hashlib
import json
import math
import uuid

try:
    from . import terrain_core as core
except ImportError:
    import terrain_core as core


def fingerprint(regions, waters, settings):
    clean={k:v for k,v in settings.items() if k!='heightfield_authoring'}
    return hashlib.sha256(json.dumps([regions,waters,clean],sort_keys=True,
        separators=(',',':'),allow_nan=False).encode()).hexdigest()


def validate_grid(source):
    required={'kind','version','id','bounds_m','width','height','samples','locked','source_sha256','operations'}
    if (not isinstance(source,dict) or set(source)!=required
            or source.get('kind')!='wz09_heightfield_authoring'
            or type(source.get('version')) is not int or source.get('version')!=1):
        raise ValueError('Heightmap: exakter Version-1-Datensatz erforderlich')
    core._identifier(source['id'],'heightmap.id')
    width=core._integer(source['width'],'heightmap.width',2,513)
    height=core._integer(source['height'],'heightmap.height',2,513)
    bounds=source['bounds_m']
    if not isinstance(bounds,(tuple,list)) or len(bounds)!=4:
        raise ValueError('Heightmap requires four world bounds')
    bounds=[core._number(v,'heightmap.bounds') for v in bounds]
    if bounds[2]<=bounds[0] or bounds[3]<=bounds[1]:raise ValueError('Heightmap area must be positive')
    if (not isinstance(source['samples'],(list,tuple)) or not isinstance(source['locked'],(list,tuple))
            or len(source['samples'])!=width*height or len(source['locked'])!=width*height
            or any(type(v) is not bool for v in source['locked'])):
        raise ValueError('Heightmap grid differs from the protection mask')
    for value in source['samples']:core._number(value,'heightmap.sample')
    if (not isinstance(source['source_sha256'],str) or len(source['source_sha256'])!=64
            or any(c not in '0123456789abcdefABCDEF' for c in source['source_sha256'])):
        raise ValueError('Heightmap requires its source surface fingerprint')
    try:int(source['source_sha256'],16)
    except ValueError:raise ValueError('Invalid heightmap fingerprint') from None
    if not isinstance(source['operations'],list) or len(source['operations'])>1024:
        raise ValueError('Heightmap supports at most 1024 editing steps')
    return source


def capture(field,bounds,width,height,source_sha256,*,identity=None):
    width=core._integer(width,'width',2,513);height=core._integer(height,'height',2,513)
    x0,y0,x1,y1=bounds;samples=[];locked=[]
    for y in range(height):
        for x in range(width):
            point=field.sample_surface(x0+x/(width-1)*(x1-x0),y0+y/(height-1)*(y1-y0))
            samples.append(point['height_m'])
            locked.append(bool(point['protected'] or point.get('underwater')
                or point.get('core_distance_m',0)<2 or x in (0,width-1) or y in (0,height-1)))
    return validate_grid(dict(kind='wz09_heightfield_authoring',version=1,id=identity or str(uuid.uuid4()),
        bounds_m=list(bounds),width=width,height=height,samples=samples,locked=locked,
        source_sha256=source_sha256,operations=[]))


def value_at(grid,x,y):
    x0,y0,x1,y1=grid['bounds_m'];w,h=grid['width'],grid['height']
    if not x0<=x<=x1 or not y0<=y<=y1:return None
    fx=(x-x0)/(x1-x0)*(w-1);fy=(y-y0)/(y1-y0)*(h-1)
    ix,iy=min(w-2,int(fx)),min(h-2,int(fy));a,b=fx-ix,fy-iy
    values=grid['samples'];j=iy*w+ix
    return ((values[j]*(1-a)+values[j+1]*a)*(1-b)
        +(values[j+w]*(1-a)+values[j+w+1]*a)*b)


def brush(source,center,radius,strength,mode='raise',*,target_height=None):
    validate_grid(source);radius=core._number(radius,'radius',positive=True)
    strength=core._number(strength,'strength',nonnegative=True)
    if mode not in ('raise','lower','smooth','flatten'):raise ValueError('Unknown height tool')
    if target_height is not None:target_height=core._number(target_height,'target_height')
    if mode=='flatten' and target_height is None:raise ValueError('Flatten requires a target height')
    cx,cy=core._xy(center);out=copy.deepcopy(source)
    w,h=source['width'],source['height'];x0,y0,x1,y1=source['bounds_m'];changed=0
    for y in range(h):
        py=y0+y/(h-1)*(y1-y0)
        if abs(py-cy)>radius:continue
        for x in range(w):
            j=y*w+x
            if source['locked'][j]:continue
            px=x0+x/(w-1)*(x1-x0);distance=math.hypot(px-cx,py-cy)
            if distance>=radius:continue
            weight=1-core._smooth(distance/radius);before=source['samples'][j]
            if mode in ('raise','lower'):after=before+strength*weight*(1 if mode=='raise' else -1)
            else:
                neighbors=[source['samples'][ny*w+nx] for ny in range(max(0,y-1),min(h,y+2))
                    for nx in range(max(0,x-1),min(w,x+2))]
                goal=sum(neighbors)/len(neighbors) if mode=='smooth' else target_height
                after=before+(goal-before)*min(1.,strength)*weight
            out['samples'][j]=after;changed+=after!=before
    out['operations'].append(dict(kind=mode,center_m=[cx,cy],radius_m=radius,strength=strength,
        target_height_m=target_height,changed_samples=changed))
    return validate_grid(out)


def erode(source,iterations=12,*,talus_deg=32.,rate=.25):
    """Conservative thermal transport; locked native/water/border cells never move."""
    validate_grid(source);iterations=core._integer(iterations,'iterations',1,100)
    talus_deg=core._number(talus_deg,'talus_deg',nonnegative=True)
    rate=core._number(rate,'rate',positive=True)
    if talus_deg>=89 or rate>.5:raise ValueError('Erosion: Winkel <89°, Rate <=0.5 erforderlich')
    out=copy.deepcopy(source);w,h=source['width'],source['height'];b=source['bounds_m']
    spacing=((b[2]-b[0])/(w-1),(b[3]-b[1])/(h-1));moved=0.
    for _ in range(iterations):
        old=out['samples'];delta=[0.]*len(old)
        for y in range(h):
            for x in range(w):
                j=y*w+x
                if source['locked'][j]:continue
                for nx,ny,step in ((x+1,y,spacing[0]),(x,y+1,spacing[1])):
                    if nx>=w or ny>=h:continue
                    k=ny*w+nx
                    if source['locked'][k]:continue
                    diff=old[j]-old[k];excess=abs(diff)-math.tan(math.radians(talus_deg))*step
                    if excess>0:
                        flow=excess*rate/4*(1 if diff>0 else -1)
                        delta[j]-=flow;delta[k]+=flow;moved+=abs(flow)
        out['samples']=[value+change for value,change in zip(old,delta)]
    out['operations'].append(dict(kind='thermal_erosion',iterations=iterations,
        talus_deg=talus_deg,rate=rate,transported_height_sum=moved))
    return validate_grid(out)


class SculptField(core.TerrainField):
    """Same clipping, river and sea rules; optional authored relief and height grid."""
    def __init__(self,base,*,grid=None,landscape=None,relief=None,materials=None):
        self.base_field=base;self.grid=validate_grid(grid) if grid else None
        self.landscape=landscape;self.relief=relief;self.materials=materials

    def __getattr__(self,name):return getattr(self.base_field,name)

    def _base(self,x,y):
        height,weights,protected,owner,distance=self.base_field._base(x,y)
        if not protected and self.landscape is not None:
            fade=core._smooth(distance/self._regions_by_id[owner]['transition_m'])
            # Keep native material matching at the recorded transition width,
            # while mountain relief needs a much broader geometric foothill.
            # A140-unit full-height ramp created near-vertical walls at winter
            # map boundaries even though the unattenuated ridge was smooth.
            relief_fade=core._smooth(distance/max(500.,self._regions_by_id[owner]['transition_m']))
            height+=self.relief(x,y,self.landscape)*relief_fade
            designed=self.materials(x,y,height,self.landscape)
            weights={key:weights.get(key,0.)*(1-fade)+designed.get(key,0.)*fade for key in set(weights)|set(designed)}
        return height,weights,protected,owner,distance

    def _layer(self,height,x,y,protected,distance):
        if self.grid is None or protected:return height
        target=value_at(self.grid,x,y)
        if target is None:return height
        # A locked raster sample alone cannot protect the water between samples:
        # bilinear interpolation could otherwise lift an existing carved bed.
        wet,underwater,_=self._wetness(height,x,y,self._river_at(x,y))
        if underwater:return height
        fade=core._smooth(distance/24.)*(1.-core._smooth(wet))
        return height+(target-height)*fade

    def _height(self,x,y):
        height=super()._height(x,y)
        if self.grid is None:return height
        _,_,protected,_,distance=self._base(x,y)
        return self._layer(height,x,y,protected,distance)

    def sample_surface(self,x,y):
        result=super().sample_surface(x,y)
        if self.grid is not None:
            result['height_m']=self._layer(result['height_m'],x,y,result['protected'],result['core_distance_m'])
            wet,under,distance=self._wetness(result['height_m'],x,y,self._river_at(x,y))
            result.update(wetness=wet,underwater=under,river_distance_m=distance)
        return result
