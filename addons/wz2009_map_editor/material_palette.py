"""Compact unused per-piece slots without losing materials or legacy hashes."""
NAME='wz09_fingerprint_material_name'
PALETTE='wz09_material_palette'
SLOTS='wz09_material_slot_map'

def material_name(material):
    if material is None:return None
    name=material.name if getattr(material,'library',None) is not None else material.name_full
    return getattr(material,'get',lambda key,default:default)(NAME,name)

def fingerprint_layout(mesh):
    get=getattr(mesh,'get',lambda key,default=None:default)
    palette=get(PALETTE)
    origins=get(SLOTS)
    if palette is None or origins is None:return [material_name(m) for m in mesh.materials],None
    names=[material_name(m) for m in palette.materials]
    slots=[int(index) for index in origins]
    if any(index<0 or index>=len(names) for index in slots):raise ValueError('Invalid retained material palette')
    mapped=[]
    for index,material in enumerate(mesh.materials):
        if index<len(slots):
            original=slots[index];names[original]=material_name(material)
        else:
            original=len(names);names.append(material_name(material))
        mapped.append(original)
    return names,mapped

def compact_copy(source,mesh,mapping,palettes,generated,bpy):
    """Only local copies change. A shared hidden Mesh retains the full palette."""
    if (source.get(PALETTE) is not None or source.get('wz09_city_baseline_id')
            or len(source.materials)<32 or not source.polygons or any(m is None for m in source.materials)):
        return False
    import numpy as np
    indices=np.empty(len(source.polygons),np.int32);source.polygons.foreach_get('material_index',indices)
    used,inverse=np.unique(indices,return_inverse=True)
    if len(used)*4>=len(source.materials):return False
    if int(used[-1])>=len(source.materials):raise ValueError('Mesh uses an invalid material slot')
    key=tuple(m.as_pointer() for m in source.materials)
    palette=palettes.get(key)
    if palette is None:
        palette=bpy.data.meshes.new('Retained native material palette')
        palette['wz09_palette_only']=True
        for material in source.materials:palette.materials.append(mapping.get(material,material))
        palettes[key]=palette;generated.add(palette)
    mesh.materials.clear()
    for index in used:
        material=source.materials[int(index)];mesh.materials.append(mapping.get(material,material))
    mesh.polygons.foreach_set('material_index',inverse.astype(np.int32))
    mesh[PALETTE]=palette;mesh[SLOTS]=used.astype(int).tolist()
    return True
