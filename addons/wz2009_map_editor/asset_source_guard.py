"""Pure guards separating reversible map crops from canonical source assets.

A map crop changes a disposable render mesh while retaining its native model
metadata. That metadata must never make the derivative an original asset again.
These functions do not import Blender, restore meshes, read files or mutate data.
"""


def cropped_source_parts(parts):
    """Return supplied parts carrying actual crop data, not merely hidden parts.

    Outside-core objects can be hidden without changing geometry. Their crop
    status/visibility alone is deliberately not evidence of a clipped mesh.
    The saved Blender original-mesh pointer and the derivative mesh's own flag
    survive the respective map-crop and placement-copy paths independently.
    """
    result=[]
    for part in parts:
        data=getattr(part,'data',None)
        getter=getattr(data,'get',lambda *_args:None)
        if (getattr(part,'wz09_core_original_mesh',None) is not None
                or bool(getter('wz09_core_derived_preview',False))):
            result.append(part)
    return tuple(result)


def require_uncropped_source(parts):
    """Fail before normalization, copying or accepting a placement baseline."""
    parts=tuple(parts)
    if not parts:raise ValueError('Original asset source has no mesh parts')
    if cropped_source_parts(parts):
        raise ValueError('Cropped map preview cannot be an original asset source; load an exact source slice or use explicit custom geometry')
    return parts


def may_reuse_source(parts,*,is_exact_slice=False,force_exact_source=False):
    """Keep intact exact slices; never reuse a clipped canonical map preview.

    Explicit force requests load the established hash-verified slice unless an
    intact slice is already canonical. Ordinary intact sources keep the prior
    fast path when exact loading has not been requested.
    """
    if type(is_exact_slice) is not bool or type(force_exact_source) is not bool:
        raise ValueError('Exact source decisions require explicit boolean flags')
    parts=tuple(parts)
    if not parts or cropped_source_parts(parts):return False
    return not force_exact_source or is_exact_slice
