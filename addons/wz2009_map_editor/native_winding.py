"""Explicit hash-bound native winding plans and audited private Mesh copies.

No scene discovery, object assignment, migration, file access or automatic hook.
The caller verifies the native file/bundle SHA and supplies it explicitly. A
plan additionally binds that provenance to the actual in-memory mesh snapshot.
The only reversing implementation is preview_geometry.copy_repaired_mesh.
"""
from array import array
import hashlib
import json
import math
import re

try:
    from .preview_geometry import classify_triangle_winding,copy_repaired_mesh,_restore_corner_normals
    from .preview_render import _collection_array
except ImportError:
    from preview_geometry import classify_triangle_winding,copy_repaired_mesh,_restore_corner_normals
    from preview_render import _collection_array

NORMAL_TOLERANCE=5e-4
NORMAL_ANGLE_TOLERANCE_DEGREES=.05
_ATTRIBUTE_TYPES={'FLOAT':('value','f',1),'INT':('value','i',1),'INT8':('value','b',1),
    'BOOLEAN':('value','b',1),'FLOAT_VECTOR':('vector','f',3),'FLOAT2':('vector','f',2),
    'FLOAT_COLOR':('color','f',4),'BYTE_COLOR':('color','f',4)}


def _sha(value):
    if not isinstance(value,str) or re.fullmatch('[0-9a-f]{64}',value) is None:
        raise ValueError('An explicitly verified lowercase native source SHA256 is required')
    return value


def _key(value):
    return None if value is None else value.as_pointer() if hasattr(value,'as_pointer') else id(value)


def _read(data,field,code,width=1):
    result=_collection_array(data,field,code,width)
    if any(not math.isfinite(v) for v in result):raise ValueError('Native mesh attributes must be finite')
    return result


def _snapshot(mesh):
    if getattr(mesh,'shape_keys',None) is not None:raise ValueError('Native winding copy does not support shape-key meshes')
    if mesh.get('wz09_core_derived_preview'):raise ValueError('A cropped derivative cannot establish an original native winding source')
    if len(mesh.corner_normals)!=len(mesh.loops):raise ValueError('Native source requires one authored normal per mesh corner')
    uv={}
    for layer in mesh.uv_layers:
        if layer.name in uv:raise ValueError('Native mesh has duplicate UV layer identities')
        uv[layer.name]=dict(active_render=bool(layer.active_render),values=_read(layer.data,'uv','f',2))
    attributes={}
    for attr in getattr(mesh,'attributes',()):
        # Blender 5.2 exposes this reserved octahedral encoding as a named
        # attribute. Its bytes depend on face winding; the actual authored
        # corner-normal vectors are restored and audited separately below.
        if attr.name=='custom_normal':
            if attr.data_type=='INT16_2D' and attr.domain=='CORNER':continue
            raise ValueError('Unexpected reserved custom_normal storage format')
        # Internal topology/encoded-normal storage is derived from the winding.
        # Authored positions, normals, UVs and material bindings are audited below.
        if attr.name.startswith('.') or attr.name in uv or attr.name in {'position','material_index'}:continue
        if attr.name in attributes:raise ValueError('Native mesh has duplicate attribute identities')
        if attr.data_type not in _ATTRIBUTE_TYPES or attr.domain not in {'POINT','EDGE','FACE','CORNER'}:
            raise ValueError('Unsupported authored native mesh attribute: '+attr.name)
        field,code,width=_ATTRIBUTE_TYPES[attr.data_type]
        attributes[attr.name]=dict(domain=attr.domain,data_type=attr.data_type,field=field,code=code,width=width,
            values=_read(attr.data,field,code,width))
    result=dict(positions=_read(mesh.vertices,'co','f',3),vertices=_read(mesh.loops,'vertex_index','i'),
        starts=_read(mesh.polygons,'loop_start','i'),totals=_read(mesh.polygons,'loop_total','i'),
        materials=_read(mesh.polygons,'material_index','i'),smooth=_read(mesh.polygons,'use_smooth','b'),
        normals=_read(mesh.corner_normals,'vector','f',3),uv=uv,attributes=attributes,
        material_keys=tuple(_key(m) for m in mesh.materials),uv_active_index=getattr(mesh.uv_layers,'active_index',0))
    # The corner identity must be unambiguous before the shared repair core runs.
    _remap(result,result)
    return result


def _fingerprint(snapshot):
    digest=hashlib.sha256()
    def put(label,value):
        digest.update(label.encode('utf-8')+b'\0')
        if isinstance(value,array):digest.update(value.typecode.encode()+str(len(value)).encode()+b':'+value.tobytes())
        else:digest.update(json.dumps(value,sort_keys=True,separators=(',',':'),allow_nan=False).encode())
    for key in ('positions','vertices','starts','totals','materials','smooth','normals','material_keys','uv_active_index'):
        put(key,snapshot[key])
    for group in ('uv','attributes'):
        for name,item in sorted(snapshot[group].items()):
            put(group+':'+name,{k:v for k,v in item.items() if k!='values'});put('values',item['values'])
    return digest.hexdigest()


def _remap(before,after):
    if len(before['totals'])!=len(after['totals']) or len(before['vertices'])!=len(after['vertices']):
        raise ValueError('Winding repair changed face/corner counts')
    remap=array('i',[-1])*len(after['vertices'])
    for old_start,old_total,new_start,new_total in zip(before['starts'],before['totals'],after['starts'],after['totals']):
        old={before['vertices'][i]:i for i in range(old_start,old_start+old_total)}
        new_vertices=after['vertices'][new_start:new_start+new_total]
        if len(old)!=old_total or new_total!=old_total or len(set(new_vertices))!=new_total or set(new_vertices)!=set(old):
            raise ValueError('Native polygon/vertex corner identity is ambiguous or changed')
        for i in range(new_start,new_start+new_total):remap[i]=old[after['vertices'][i]]
    if any(i<0 for i in remap):raise ValueError('Winding repair left an unmatched corner')
    return remap


def _classify(mesh):
    mesh.calc_loop_triangles();counts=dict(positive=0,negative=0,ambiguous=0,degenerate=0)
    faces=set();minimum=1.;maximum=-1.
    for triangle in mesh.loop_triangles:
        vertices=[tuple(mesh.vertices[i].co) for i in triangle.vertices]
        normals=[tuple(mesh.corner_normals[i].vector) for i in triangle.loops]
        state=classify_triangle_winding(vertices,normals)
        if state=='degenerate':counts['degenerate']+=1;continue
        if state=='ambiguous':counts['ambiguous']+=1;continue
        a,b,c=vertices;u=[b[i]-a[i] for i in range(3)];v=[c[i]-a[i] for i in range(3)]
        cross=(u[1]*v[2]-u[2]*v[1],u[2]*v[0]-u[0]*v[2],u[0]*v[1]-u[1]*v[0])
        normal=[sum(n[i] for n in normals) for i in range(3)]
        dot=sum(x*y for x,y in zip(cross,normal))/math.sqrt(sum(x*x for x in cross)*sum(x*x for x in normal))
        dot=max(-1.,min(1.,dot));minimum=min(minimum,dot);maximum=max(maximum,dot)
        label='negative' if dot<=-.1 else 'positive' if dot>=.1 else 'ambiguous'
        counts[label]+=1
        if label=='negative':faces.add(triangle.polygon_index)
    if counts['ambiguous'] or counts['negative'] and counts['positive']:
        raise ValueError('Mixed or ambiguous native winding cannot be corrected automatically')
    if not counts['negative'] and not counts['positive']:raise ValueError('Native winding has no nondegenerate proven triangles')
    return dict(counts=counts,min_dot=minimum,max_dot=maximum,
        decision='COPY_REVERSE' if counts['negative'] else 'NOOP_ALIGNED',faces=sorted(faces))


def plan_native_winding(mesh,*,source_sha256):
    """Read only: bind a uniform native orientation to provenance and content."""
    source_sha256=_sha(source_sha256);return _plan(mesh,source_sha256,_snapshot(mesh))


def _plan(mesh,source_sha256,snapshot):
    classification=_classify(mesh)
    return dict(kind='wz09_native_winding_plan',version=1,source_sha256=source_sha256,
        mesh_snapshot_sha256=_fingerprint(snapshot),**classification)


def _write(data,field,values,width):
    if hasattr(data,'foreach_set'):data.foreach_set(field,values);return
    for i,item in enumerate(data):setattr(item,field,values[i] if width==1 else tuple(values[i*width:(i+1)*width]))


def _mapped(values,remap,width):
    return array(values.typecode,(values[old*width+j] for old in remap for j in range(width)))


def _restore_attributes(mesh,before,after,remap):
    missing=set(before['attributes'])-set(after['attributes'])
    extra=set(after['attributes'])-set(before['attributes'])
    if missing=={'sharp_face'} and not extra:
        # BMesh can omit this built-in layer when all faces remain smooth.
        # Recreate only the proved FACE/BOOLEAN representation of the same
        # polygon smooth flags, after face/vertex correspondence was checked.
        item=before['attributes']['sharp_face'];values=item['values']
        if (item['data_type']!='BOOLEAN' or item['domain']!='FACE'
                or len(values)!=len(before['smooth']) or before['smooth']!=after['smooth']
                or any(bool(value)==bool(smooth) for value,smooth in zip(values,before['smooth']))):
            raise ValueError('Missing sharp_face is not the validated polygon smooth representation')
        attr=mesh.attributes.new(name='sharp_face',type='BOOLEAN',domain='FACE')
        if attr.name!='sharp_face' or len(attr.data)!=len(values):raise ValueError('Cannot restore exact sharp_face attribute identity')
        _write(attr.data,'value',values,1)
        after=_snapshot(mesh)
    if set(before['uv'])!=set(after['uv']) or set(before['attributes'])!=set(after['attributes']):
        raise ValueError('Winding repair changed authored UV/attribute identities')
    for layer in mesh.uv_layers:_write(layer.data,'uv',_mapped(before['uv'][layer.name]['values'],remap,2),2)
    attributes={attr.name:attr for attr in getattr(mesh,'attributes',())}
    for name,item in before['attributes'].items():
        if item['domain']=='CORNER':_write(attributes[name].data,item['field'],_mapped(item['values'],remap,item['width']),item['width'])
    normals={}
    for face,(start,total) in enumerate(zip(before['starts'],before['totals'])):
        for i in range(start,start+total):normals[(face,before['vertices'][i])]=tuple(before['normals'][i*3:i*3+3])
    _restore_corner_normals(mesh,normals)


def _audit(before,after,remap):
    for key in ('positions','materials','smooth','material_keys','uv_active_index'):
        if before[key]!=after[key]:raise ValueError('Native winding changed protected '+key)
    for group in ('uv','attributes'):
        if set(before[group])!=set(after[group]):raise ValueError('Native winding changed attribute names')
        for name,item in before[group].items():
            other=after[group][name]
            if {k:v for k,v in item.items() if k!='values'}!={k:v for k,v in other.items() if k!='values'}:
                raise ValueError('Native winding changed attribute metadata: '+name)
            corner=group=='uv' or item['domain']=='CORNER';width=2 if group=='uv' else item['width']
            expected=_mapped(item['values'],remap,width) if corner else item['values']
            if other['values']!=expected:raise ValueError('Native winding changed authored attribute values: '+name)
    expected=_mapped(before['normals'],remap,3)
    delta=max((abs(a-b) for a,b in zip(expected,after['normals'])),default=0.)
    maximum_angle=0.
    for i in range(0,len(expected),3):
        first=expected[i:i+3];second=after['normals'][i:i+3]
        first_length=math.sqrt(sum(v*v for v in first));second_length=math.sqrt(sum(v*v for v in second))
        if first_length<=1e-12 or second_length<=1e-12:raise ValueError('Native winding has an unprovable zero corner normal direction')
        if first!=second:
            dot=sum(a*b for a,b in zip(first,second))/(first_length*second_length)
            maximum_angle=max(maximum_angle,math.degrees(math.acos(max(-1.,min(1.,dot)))))
    # Blender 5.2 re-encodes its INT16_2D custom normals in the reversed face
    # basis. Actual native QA measured 0.00038553 component / 0.027311 degrees;
    # require both bounded component error and preserved vector direction.
    if delta>NORMAL_TOLERANCE or maximum_angle>NORMAL_ANGLE_TOLERANCE_DEGREES:
        raise ValueError('Native winding changed authored corner normal directions')
    return dict(status='PASS',positions_exact=True,uv_corner_values_exact=True,material_bindings_exact=True,
        polygon_material_indices_exact=True,polygon_smooth_exact=True,authored_attributes_exact=True,
        corner_identity='polygon_index_and_vertex_index',normal_vectors_exact=delta==0,
        normal_max_component_error=delta,normal_encoding_tolerance=NORMAL_TOLERANCE,
        normal_max_angle_error_degrees=maximum_angle,
        normal_encoding_angle_tolerance_degrees=NORMAL_ANGLE_TOLERANCE_DEGREES)


def corrected_mesh_copy(mesh,*,source_sha256,bpy_module=None,bmesh_module=None,plan=None):
    """Return ``(private_corrected_mesh, report)``; aligned input returns itself.

    No caller object is assigned. Shared/source meshes and original files stay
    unchanged. Failure removes only this call's private derived Mesh.
    """
    source_sha256=_sha(source_sha256);before=_snapshot(mesh);current=_plan(mesh,source_sha256,before)
    if plan is not None and plan!=current:raise ValueError('Native winding plan is stale or belongs to another source')
    report=dict(kind='wz09_native_winding_copy',version=1,source_sha256=source_sha256,
        source_snapshot_sha256=current['mesh_snapshot_sha256'],before={k:v for k,v in current.items() if k in {'counts','min_dot','max_dot','decision'}},
        copied=False,source_mesh_unchanged=True,original_files_written=False,runtime_integrated=False)
    if current['decision']=='NOOP_ALIGNED':
        report.update(status='NOOP_ALIGNED',after=report['before'],attribute_audit=_audit(before,before,_remap(before,before)))
        return mesh,report
    if bpy_module is None:
        import bpy as bpy_module
    repaired=None
    try:
        repaired=copy_repaired_mesh(mesh,current['faces'],bpy_module=bpy_module,bmesh_module=bmesh_module)
        if _key(repaired)==_key(mesh):raise ValueError('Native repair must return a separate private mesh')
        intermediate=_snapshot(repaired);remap=_remap(before,intermediate)
        _restore_attributes(repaired,before,intermediate,remap)
        after=_snapshot(repaired);audit=_audit(before,after,_remap(before,after));post=_classify(repaired)
        if post['decision']!='NOOP_ALIGNED' or post['counts']['positive']!=current['counts']['negative'] or post['counts']['degenerate']!=current['counts']['degenerate']:
            raise ValueError('Native winding copy failed its positive postcondition')
        if _fingerprint(_snapshot(mesh))!=current['mesh_snapshot_sha256']:raise ValueError('Original native mesh changed during repair')
        repaired['wz09_native_winding_source_sha256']=source_sha256
        repaired['wz09_native_winding_source_snapshot_sha256']=current['mesh_snapshot_sha256']
        repaired['wz09_native_winding_version']=1
        report.update(status='PASS_CORRECTED_COPY',copied=True,faces_reversed=len(current['faces']),
            after={k:v for k,v in post.items() if k in {'counts','min_dot','max_dot','decision'}},attribute_audit=audit,
            corrected_snapshot_sha256=_fingerprint(after),repair_core='preview_geometry.copy_repaired_mesh')
        return repaired,report
    except Exception:
        if repaired is not None and _key(repaired)!=_key(mesh):bpy_module.data.meshes.remove(repaired,do_unlink=True)
        raise
