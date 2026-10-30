"""Linear collection lookup for newly imported objects, with no RNA user scans."""


def _key(value):
    return value.as_pointer() if hasattr(value,'as_pointer') else id(value)


def collection_index(objects,api):
    """Read each global/master collection once, indexed by candidate pointer.

    Object.users_collection in Blender materializes every collection's object
    list per object. This operation instead costs total collection links plus
    the import size and retains support for objects linked to several collections.
    """
    chosen=list(objects)
    result={_key(obj):[] for obj in chosen}
    collections=list(api.data.collections)
    collections.extend(scene.collection for scene in api.data.scenes)
    seen=set()
    for collection in collections:
        key=_key(collection)
        if key in seen:continue
        seen.add(key)
        for obj in collection.objects:
            key=_key(obj)
            if key in result:result[key].append(collection)
    return result


def move_imported_objects(objects,destination,*,bpy_module=None):
    """Relink only supplied objects; the caller owns rollback of new imports."""
    if bpy_module is None:import bpy as bpy_module
    chosen=list(objects)
    links=collection_index(chosen,bpy_module)
    destination_key=_key(destination)
    for obj in chosen:
        previous=links[_key(obj)]
        for collection in previous:
            if _key(collection)!=destination_key:collection.objects.unlink(obj)
        if not any(_key(collection)==destination_key for collection in previous):
            destination.objects.link(obj)
    return links
