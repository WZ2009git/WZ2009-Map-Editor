"""Pure regression checks for scoped dependency copies and undo isolation."""
import importlib.util
import json
from pathlib import Path
import sys
import types
import unittest
from unittest.mock import patch
ROOT=Path(__file__).resolve().parents[1]
pkg=types.ModuleType('local_copy_checks');pkg.__path__=[str(ROOT/'addons/wz2009_map_editor')];sys.modules[pkg.__name__]=pkg
def load(name):
    spec=importlib.util.spec_from_file_location(pkg.__name__+'.'+name,Path(pkg.__path__[0])/(name+'.py'))
    module=importlib.util.module_from_spec(spec);sys.modules[spec.name]=module;spec.loader.exec_module(module);return module
copy=load('library_local_copy');city=load('city_editing');residency=load('map_residency');palette=load('material_palette')

class Material(dict):
    def __bool__(self):return True
    def __init__(self,name,original=None):
        super().__init__();self.name_full=name;self.name=name
        if original is not None:self['wz09_fingerprint_material_name']=original
class Items:
    def __init__(self,**values):self.values=values
    def __len__(self):return len(next(iter(self.values.values())))
    def foreach_get(self,field,target):
        values=self.values[field];flat=[v for row in values for v in row] if values and isinstance(values[0],tuple) else values
        for i,v in enumerate(flat):target[i]=v
def mesh(material,z=0):
    return types.SimpleNamespace(vertices=Items(co=[(0,0,z),(1,0,0),(0,1,0)]),loops=Items(vertex_index=[0,1,2]),
        polygons=Items(loop_total=[3],material_index=[0]),uv_layers=[],attributes={},materials=[material])

class CopyTests(unittest.TestCase):
    def test_compact_palette_preserves_legacy_fingerprint(self):
        class Mesh(dict):pass
        mats=[Material('a'),Material('b'),Material('c')]
        original=mesh(mats[0]);original.materials=mats;original.polygons=Items(loop_total=[3],material_index=[2])
        compact=Mesh();compact.__dict__.update(mesh(Material('c.001','c')).__dict__)
        compact[palette.PALETTE]=types.SimpleNamespace(materials=mats);compact[palette.SLOTS]=[2]
        self.assertEqual(city.mesh_digest(original),city.mesh_digest(compact))
        compact.materials=[Material('different')]
        self.assertNotEqual(city.mesh_digest(original),city.mesh_digest(compact))
    def test_bulk_remap_only_accepts_private_temporary_dependencies(self):
        collection,obj,material=(object() for _ in range(3));closure={collection,obj,material}
        users={obj:{collection},material:{obj}}
        self.assertEqual(copy.private_temporary_ids(closure,users,set(),set()),closure)
    def test_preexisting_dependency_is_never_bulk_remapped(self):
        material=object()
        self.assertEqual(copy.private_temporary_ids({material},{},set(),{material}),set())
    def test_foreign_collection_owner_protects_indirect_materials(self):
        collection,obj,material,foreign=(object() for _ in range(4));closure={collection,obj,material}
        users={collection:{foreign},obj:{collection},material:{obj}}
        self.assertEqual(copy.private_temporary_ids(closure,users,set(),set()),set())
    def test_dependency_closure_excludes_foreign_users(self):
        collection,obj,data,material,image,foreign=(object() for _ in range(6))
        users={obj:{collection},data:{obj},material:{data,foreign},image:{material}}
        self.assertEqual(copy.dependency_closure([collection],users),{collection,obj,data,material,image})
    def test_instancing_prototype_dependencies_are_included(self):
        collection,host,prototype,meshdata=(object() for _ in range(4))
        users={host:{collection},prototype:{host},meshdata:{prototype}}
        self.assertIn(meshdata,copy.dependency_closure([collection],users))
    def test_cycles_and_unreferenced_explicit_baselines(self):
        a,b,baseline=(object() for _ in range(3))
        self.assertEqual(copy.dependency_closure([a,baseline],{a:{b},b:{a}}),{a,b,baseline})
    def test_material_namespace_suffix_does_not_invalidate_geometry(self):
        self.assertEqual(city.mesh_digest(mesh(Material('native material'))),
                         city.mesh_digest(mesh(Material('native material.042','native material'))))
    def test_linked_library_display_suffix_does_not_change_fingerprint(self):
        linked=Material('native [library.blend]');linked.name='native';linked.library=object()
        self.assertEqual(city.mesh_digest(mesh(Material('native'))),city.mesh_digest(mesh(linked)))
    def test_real_geometry_edits_still_change_fingerprint(self):
        self.assertNotEqual(city.mesh_digest(mesh(Material('native'))),city.mesh_digest(mesh(Material('native.001','native'),.125)))
    def test_undo_baseline_is_scoped_to_resident_map(self):
        local={'wz09_city_baseline_id':'source','wz09_residency_token':'current'}
        foreign={'wz09_city_baseline_id':'source','wz09_residency_token':'foreign'}
        class Scene(dict):pass
        scene=Scene({city.REGISTRY:json.dumps(dict(meshes={'source':{'owner_map':'map'}},parts={},models={}))})
        scene.collection=types.SimpleNamespace(children_recursive=[{'wz09_resident_map_id':'map','wz09_resident_layer':'core','wz09_residency_token':'current'}])
        with patch.dict(sys.modules,{'bpy':types.SimpleNamespace(data=types.SimpleNamespace(meshes=[foreign,local]))}):
            result=city.retained_baselines(scene,'source')
        self.assertEqual(len(result),1);self.assertIs(result[0],local)
        self.assertEqual(foreign['wz09_residency_token'],'foreign')
        with patch.dict(sys.modules,{'bpy':types.SimpleNamespace(data=types.SimpleNamespace(meshes=[foreign]))}):
            self.assertEqual(city.retained_baselines(scene,'source'),[])
    def test_classic_nonresident_baseline_is_still_supported(self):
        baseline={'wz09_city_baseline_id':'source'}
        with patch.dict(sys.modules,{'bpy':types.SimpleNamespace(data=types.SimpleNamespace(meshes=[baseline]))}):
            self.assertEqual(city.retained_baselines({},'source'),[baseline])

    def test_core_and_outskirts_registry_are_saved_separately(self):
        records=dict(meshes={'core':dict(owner_map='map'),'outer':dict(owner_map='map')},
                     parts={'a':dict(source_mesh_id='core'),'b':dict(source_mesh_id='outer')},
                     models={'car':dict(owner_map='map'),'tree':dict(owner_map='map')})
        scene={city.REGISTRY:json.dumps(records)}
        core=[{city.SOURCE:'core'},{'wz09_tracked_native_model':'car'}]
        outer=[{'wz09_city_part_source':'outer'},{'wz09_tracked_native_model':'tree'}]
        a=residency._local_registry(scene,'map',core);b=residency._local_registry(scene,'map',outer)
        self.assertEqual(set(a['meshes']),{'core'});self.assertEqual(set(b['meshes']),{'outer'})
        self.assertEqual(set(a['parts']),{'a'});self.assertEqual(set(b['parts']),{'b'})
        self.assertEqual(set(a['models']),{'car'});self.assertEqual(set(b['models']),{'tree'})

    def test_outskirts_baseline_uses_its_own_residency_token(self):
        class Collection(dict):pass
        class Scene(dict):pass
        core=Collection(wz09_resident_map_id='map',wz09_resident_layer='core',wz09_residency_token='core-token')
        outer=Collection(wz09_resident_map_id='map',wz09_resident_layer='outer',wz09_residency_token='outer-token')
        core.all_objects=[{city.SOURCE:'core-source'}];outer.all_objects=[{city.SOURCE:'outer-source'}]
        scene=Scene({city.REGISTRY:json.dumps(dict(meshes={'outer-source':dict(owner_map='map')},parts={},models={}))})
        scene.collection=types.SimpleNamespace(children_recursive=[core,outer])
        expected={'wz09_city_baseline_id':'outer-source','wz09_residency_token':'outer-token'}
        foreign={'wz09_city_baseline_id':'outer-source','wz09_residency_token':'foreign'}
        with patch.dict(sys.modules,{'bpy':types.SimpleNamespace(data=types.SimpleNamespace(meshes=[foreign,expected]))}):
            result=city.retained_baselines(scene,'outer-source')
        self.assertEqual(len(result),1);self.assertIs(result[0],expected)

if __name__=='__main__':unittest.main(verbosity=2)
