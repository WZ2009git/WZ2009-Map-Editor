"""Exact static-mesh slices of verified glTF, without a native geometry decoder.

Referenced accessor bytes and encoded images are copied unchanged. The source
catalog identity stays the original bundle/mesh identity. This small cache lets
Blender load one asset instead of importing its entire source map.
"""
import base64
import copy
import json
import mimetypes
from pathlib import Path

try:
    from .source_bundle import verify_preview_bundle,capture_gltf_bundle,sha256_file
    from .asset_catalog import validate_asset_record,catalog_from_document
except ImportError:
    from source_bundle import verify_preview_bundle,capture_gltf_bundle,sha256_file
    from asset_catalog import validate_asset_record,catalog_from_document


def _index(document,collection,value):
    values=document.get(collection,[])
    if not isinstance(values,list) or type(value) is not int or not 0<=value<len(values):
        raise ValueError('Invalid source '+collection+' index')
    return value


def _view(document,index):
    index=_index(document,'bufferViews',index);value=document['bufferViews'][index]
    if not isinstance(value,dict):raise ValueError('Invalid source buffer view')
    _index(document,'buffers',value.get('buffer',0))
    offset=value.get('byteOffset',0);length=value.get('byteLength')
    if type(offset) is not int or type(length) is not int or offset<0 or length<0:raise ValueError('Invalid source buffer view span')
    return value


def slice_document(document,mesh_index,buffer_bytes,image_bytes):
    """Pure reference remap; callbacks provide already verified original bytes."""
    _index(document,'meshes',mesh_index)
    mesh=copy.deepcopy(document['meshes'][mesh_index])
    if mesh.get('weights') or any(p.get('targets') or p.get('extensions') for p in mesh['primitives']):
        raise ValueError('Animated/morphed/compressed mesh requires its explicit animation importer')
    used_accessors=set();used_materials=set()
    for primitive in mesh['primitives']:
        used_accessors.update(_index(document,'accessors',value) for value in primitive.get('attributes',{}).values())
        if 'indices' in primitive:used_accessors.add(_index(document,'accessors',primitive['indices']))
        if 'material' in primitive:used_materials.add(_index(document,'materials',primitive['material']))
    accessors=[];accessor_map={};views=set()
    for old in sorted(used_accessors):
        value=copy.deepcopy(document['accessors'][old]);accessor_map[old]=len(accessors);accessors.append(value)
        if 'bufferView' in value:views.add(_index(document,'bufferViews',value['bufferView']))
        if 'sparse' in value:
            sparse=value['sparse']
            if not isinstance(sparse,dict) or not all(isinstance(sparse.get(key),dict) and 'bufferView' in sparse[key] for key in ('indices','values')):
                raise ValueError('Invalid source sparse accessor views')
            for key in ('indices','values'):views.add(_index(document,'bufferViews',sparse[key]['bufferView']))
    materials=[copy.deepcopy(document['materials'][i]) for i in sorted(used_materials)]
    material_map={old:new for new,old in enumerate(sorted(used_materials))}
    textures=set()
    def texture_infos(value):
        if isinstance(value,dict):
            for key,item in value.items():
                if key.lower().endswith('texture') and isinstance(item,dict) and 'index' in item:yield item
                else:yield from texture_infos(item)
        elif isinstance(value,list):
            for item in value:yield from texture_infos(item)
    for material in materials:
        for info in texture_infos(material):textures.add(_index(document,'textures',info['index']))
    texture_map={old:new for new,old in enumerate(sorted(textures))}
    selected_textures=[copy.deepcopy(document['textures'][i]) for i in sorted(textures)]
    image_ids=set();sampler_ids=set()
    for texture in selected_textures:
        if 'source' in texture:image_ids.add(_index(document,'images',texture['source']))
        if 'sampler' in texture:sampler_ids.add(_index(document,'samplers',texture['sampler']))
        for value in texture.get('extensions',{}).values():
            if isinstance(value,dict) and 'source' in value:image_ids.add(_index(document,'images',value['source']))
    image_map={old:new for new,old in enumerate(sorted(image_ids))}
    sampler_map={old:new for new,old in enumerate(sorted(sampler_ids))}
    images=[]
    for old in sorted(image_ids):
        value=copy.deepcopy(document['images'][old])
        if 'bufferView' in value:_view(document,value['bufferView'])
        payload,mime=image_bytes(old,value)
        if not isinstance(payload,bytes) or not payload:raise ValueError('Missing exact source image')
        value.pop('bufferView',None);value.pop('mimeType',None)
        value['uri']='data:'+mime+';base64,'+base64.b64encode(payload).decode('ascii')
        images.append(value)
    binary=bytearray();out_views=[];view_map={}
    for old in sorted(views):
        value=copy.deepcopy(_view(document,old));source=buffer_bytes(value.get('buffer',0))
        offset=value.get('byteOffset',0);length=value['byteLength']
        if (type(offset) is not int or type(length) is not int or offset<0 or length<0 or offset+length>len(source)):
            raise ValueError('Source buffer view is outside its verified buffer')
        while len(binary)%4:binary.append(0)
        value.update(buffer=0,byteOffset=len(binary));binary.extend(source[offset:offset+length])
        view_map[old]=len(out_views);out_views.append(value)
    for value in accessors:
        if 'bufferView' in value:value['bufferView']=view_map[value['bufferView']]
        for key in ('indices','values'):
            if key in value.get('sparse',{}):
                value['sparse'][key]['bufferView']=view_map[value['sparse'][key]['bufferView']]
    for primitive in mesh['primitives']:
        primitive['attributes']={key:accessor_map[i] for key,i in primitive['attributes'].items()}
        if 'indices' in primitive:primitive['indices']=accessor_map[primitive['indices']]
        if 'material' in primitive:primitive['material']=material_map[primitive['material']]
    for material in materials:
        for info in texture_infos(material):info['index']=texture_map[info['index']]
    for texture in selected_textures:
        if 'source' in texture:texture['source']=image_map[texture['source']]
        if 'sampler' in texture:texture['sampler']=sampler_map[texture['sampler']]
        for value in texture.get('extensions',{}).values():
            if isinstance(value,dict) and 'source' in value:value['source']=image_map[value['source']]
    result={'asset':{'version':'2.0','generator':'WZ09 exact static mesh slice v1'},
        'scene':0,'scenes':[{'nodes':[0]}],'nodes':[{'name':mesh.get('name',''),
        'mesh':0,'extras':{'iw4l':{'model':mesh.get('name','')}}}],
        'meshes':[mesh],'accessors':accessors,'bufferViews':out_views,
        'buffers':[{'uri':'mesh.bin','byteLength':len(binary)}]}
    for key,value in (('materials',materials),('textures',selected_textures),('images',images),
        ('samplers',[copy.deepcopy(document['samplers'][i]) for i in sorted(sampler_ids)])):
        if value:result[key]=value
    for key in ('extensionsUsed','extensionsRequired'):
        if key in document:result[key]=copy.deepcopy(document[key])
    return result,bytes(binary)


def capture_slice(entry,bundle,cache_root):
    entry=validate_asset_record(entry);document=verify_preview_bundle(bundle)
    actual={r['id']:r for r in catalog_from_document(document,bundle,entry['source_map'])['assets']}.get(entry['id'])
    if actual is None or any(entry.get(k)!=v for k,v in actual.items()):raise ValueError('Source asset differs from verified catalog')
    if any('skin' in document['nodes'][_index(document,'nodes',i)] for i in actual['source_node_indices']):
        raise ValueError('Skinned source asset requires Animation Studio; static slice would lose its rig')
    cache_root=Path(cache_root).resolve()
    target=cache_root/entry['source_sha256']/str(entry['mesh_index'])
    manifest=target/'slice.json';reference=target/'mesh.gltf'
    if manifest.exists():
        stored=json.loads(manifest.read_text(encoding='utf-8'))
        if stored.get('asset_id')!=entry['id'] or stored.get('kind')!='exact_static_mesh_slice_v1':
            raise ValueError('Asset slice cache identity differs')
        verify_preview_bundle(stored['slice_bundle']);return stored['slice_bundle']
    base=Path(bundle['reference_path']).resolve().parent
    if Path(bundle['reference_path']).suffix.lower()!='.gltf':raise ValueError('Lazy source slicing currently requires explicit glTF + verified buffers')
    buffers={}
    def original_bytes(uri):
        if uri.startswith('data:'):return base64.b64decode(uri.partition(',')[2],validate=True)
        from urllib.parse import unquote
        return (base/unquote(uri)).read_bytes()
    def buffer(index):
        _index(document,'buffers',index)
        if index not in buffers:buffers[index]=original_bytes(document['buffers'][index]['uri'])
        return buffers[index]
    def image(index,value):
        if 'uri' in value:
            mime=(value['uri'][5:].partition(';')[0] if value['uri'].startswith('data:')
                  else mimetypes.guess_type(value['uri'])[0]) or 'application/octet-stream'
            return original_bytes(value['uri']),mime
        view=_view(document,value['bufferView']);offset=view.get('byteOffset',0)
        return buffer(view.get('buffer',0))[offset:offset+view['byteLength']],value['mimeType']
    output,binary=slice_document(document,entry['mesh_index'],buffer,image)
    target.mkdir(parents=True,exist_ok=False)
    with (target/'mesh.bin').open('xb') as f:f.write(binary)
    with reference.open('x',encoding='utf-8') as f:json.dump(output,f,ensure_ascii=False,sort_keys=True)
    result=capture_gltf_bundle(reference,include_images=True)
    with manifest.open('x',encoding='utf-8') as f:json.dump({'kind':'exact_static_mesh_slice_v1',
        'asset_id':entry['id'],'original_bundle':bundle,'slice_bundle':result,
        'runtime_integrated':False,'collision_verified':False},f,indent=2)
    return result
