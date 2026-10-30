"""Bounded native-texture splat bake using the existing sampled material field.

Only material weights are interpolated from an explicit coarse grid. Native
textures retain bilinear detail when magnified and are integrated over the
actual output pixel footprint when minified. Geometry uses the exact field.
NumPy is Blender's bundled array runtime; legacy callers need not import it.
"""
import math


def _periodic_axis(source, coordinates, footprint, axis, np):
    """Sample one repeating native-image axis, without mutating its pixels.

    Coordinates address texel centers (integer 0 is the first texel). For
    minification, integrate the periodic piecewise-linear image exactly over
    each pixel's footprint. Complete texture periods use their measured mean;
    only the remaining bounded interval needs prefix-integral evaluation.
    X and Y are filtered separately so a wide world tile cannot blur a
    magnified perpendicular axis. This also supports odd/non-square images.
    """
    plane=np.moveaxis(source,axis,0)
    size=plane.shape[0]
    following=np.roll(plane,-1,axis=0)
    expand=(slice(None),)+(None,)*(plane.ndim-1)
    if footprint<=1.:
        wrapped=coordinates%size
        low=np.floor(wrapped).astype(np.int64)
        fraction=(wrapped-low)[expand]
        result=plane[low]*(1-fraction)+following[low]*fraction
    else:
        # One interval [i,i+1] under the linear reconstruction is a trapezoid.
        prefix=np.concatenate((np.zeros_like(plane[:1]),
            np.cumsum((plane+following)*.5,axis=0,dtype=np.float64)),axis=0)
        period=prefix[-1]
        remainder=footprint%size
        start=(coordinates-footprint*.5)%size

        def primitive(position):
            cycles=np.floor(position/size)
            wrapped=position%size
            low=np.floor(wrapped).astype(np.int64)
            fraction=(wrapped-low)[expand]
            return (cycles[expand]*period+prefix[low]+plane[low]*fraction
                    +(following[low]-plane[low])*(.5*fraction*fraction))

        result=((1.-remainder/footprint)*(period/size)
                +(primitive(start+remainder)-primitive(start))/footprint)
    return np.moveaxis(result,0,axis)


def bake(field,bounds,resolution,repeat,samplers,channels,palette,*,weight_cells=32):
    import numpy as np
    if (type(resolution) is not int or not 32<=resolution<=1024
            or type(repeat) not in (int,float) or not math.isfinite(repeat) or repeat<=0
            or type(weight_cells) is not int or not 4<=weight_cells<=128):
        raise ValueError('Bounded material bake resolution/repeat/grid required')
    if len(bounds)!=4 or any(type(v) not in (int,float) or not math.isfinite(v) for v in bounds):
        raise ValueError('Four finite material bounds required')
    x0,y0,x1,y1=bounds
    if x1<=x0 or y1<=y0 or not math.isfinite(x1-x0) or not math.isfinite(y1-y0):
        raise ValueError('Positive finite material bounds required')
    channels=tuple(channels)
    if (not 1<=len(channels)<=16 or any(not isinstance(c,str) or not c for c in channels)
            or len(set(channels))!=len(channels)):
        raise ValueError('One to sixteen unique material channels required')
    if set(samplers)-set(channels):raise ValueError('Native samplers must belong to declared material channels')
    for channel,source in samplers.items():
        if not isinstance(source,(tuple,list)) or len(source)!=3:
            raise ValueError('Native sampler requires width, height and RGB pixels')
        w,h,values=source
        if type(w) is not int or type(h) is not int or not 1<=w<=256 or not 1<=h<=256 or len(values)!=w*h:
            raise ValueError('Native samplers require 1..256 pixels per axis and matching RGB data')
    grid=np.empty((weight_cells+1,weight_cells+1,len(channels)),dtype=np.float32)
    for y in range(weight_cells+1):
        for x in range(weight_cells+1):
            weights=field.sample_surface(x0+x/weight_cells*(x1-x0),y0+y/weight_cells*(y1-y0))['biome_weights']
            grid[y,x]=[weights.get(channel,0.) for channel in channels]
    if not np.isfinite(grid).all() or (grid<0).any() or (grid.sum(axis=2)<=0).any():
        raise ValueError('Finite normalized nonempty source material weights required')
    index=(np.arange(resolution,dtype=np.float64)+.5)/resolution*weight_cells
    low=np.floor(index).astype(np.int32);high=np.minimum(weight_cells,low+1);fraction=index-low
    xx=x0+(np.arange(resolution,dtype=np.float64)+.5)/resolution*(x1-x0)
    yy=y0+(np.arange(resolution,dtype=np.float64)+.5)/resolution*(y1-y0)
    pixels=np.zeros((resolution,resolution,4),dtype=np.float32);pixels[:,:,3]=1
    total=np.zeros((resolution,resolution),dtype=np.float32)
    for ci,channel in enumerate(channels):
        lower=grid[low[:,None],low[None,:],ci]*(1-fraction[None,:])+grid[low[:,None],high[None,:],ci]*fraction[None,:]
        upper=grid[high[:,None],low[None,:],ci]*(1-fraction[None,:])+grid[high[:,None],high[None,:],ci]*fraction[None,:]
        weight=lower*(1-fraction[:,None])+upper*fraction[:,None];total+=weight
        if channel in samplers:
            w,h,values=samplers[channel];source=np.asarray(values,dtype=np.float32).reshape(h,w,3)
            if not np.isfinite(source).all():raise ValueError('Nonfinite native image pixels')
            # Preserve the established global world-XY UVs and repeat length.
            # Filtering changes coverage, never UV scale/orientation or source
            # MW2 images. No unresolvable native detail is point-sampled.
            u=(xx/repeat%1)*w-.5;v=(yy/repeat%1)*h-.5
            footprint_x=(x1-x0)/resolution/repeat*w
            footprint_y=(y1-y0)/resolution/repeat*h
            if not math.isfinite(footprint_x) or not math.isfinite(footprint_y):
                raise ValueError('Finite native texture pixel footprints required')
            color=_periodic_axis(source,u,footprint_x,1,np)
            color=_periodic_axis(color,v,footprint_y,0,np)
        else:
            # Same declared legacy fallback; native channel images avoid it.
            variation=.94+.06*np.sin(xx[None,:]*.83+np.sin(yy[:,None]*.51))*np.cos(yy[:,None]*.71)
            color=variation[:,:,None]*np.asarray(palette[channel],dtype=np.float32)
        pixels[:,:,:3]+=color*weight[:,:,None]
    pixels[:,:,:3]/=total[:,:,None]
    if not np.isfinite(pixels).all():raise ValueError('Nonfinite baked pixels')
    return pixels.reshape(-1)
