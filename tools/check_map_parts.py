"""Pure cache binding and geometry eligibility checks for prepared map parts."""
import hashlib
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import types
import unittest

ROOT=Path(__file__).resolve().parents[1]
package=types.ModuleType('map_parts_checks');package.__path__=[str(ROOT/'addons/wz2009_map_editor')];sys.modules[package.__name__]=package
def load(name):
    spec=importlib.util.spec_from_file_location(package.__name__+'.'+name,Path(package.__path__[0])/(name+'.py'))
    module=importlib.util.module_from_spec(spec);sys.modules[spec.name]=module;spec.loader.exec_module(module);return module
city=load('city_editing');residency=load('map_residency');parts=load('map_parts')

class Object(dict):
    def __init__(self,**properties):
        super().__init__(properties);self.type='MESH';self.modifiers=[];self.data=types.SimpleNamespace(polygons=[1])

class MapPartsTests(unittest.TestCase):
    def test_only_coincident_nested_empty_wrappers_receive_new_identity(self):
        root=Object(wz09_id='map',wz09_type='map_instance');root.type='EMPTY';root.parent=None
        outer=Object(wz09_id='map',wz09_library_object_id='legacy');inner=Object(wz09_id='map',wz09_library_object_id='legacy')
        matrix=[[1,0,0,0],[0,1,0,0],[0,0,1,0],[0,0,0,1]]
        for obj in (outer,inner):obj.type='EMPTY';obj.matrix_world=matrix;obj.constraints=[];obj.animation_data=None
        outer.parent=root;inner.parent=outer
        self.assertEqual(parts.repair_legacy_wrappers(root,[outer,inner]),1)
        self.assertEqual(inner['wz09_library_object_id'],'legacy')
        self.assertNotEqual(outer['wz09_library_object_id'],'legacy');self.assertIs(inner.parent,outer)
    def test_duplicate_mesh_identity_is_never_silently_reassigned(self):
        root=Object(wz09_id='map');a=Object(wz09_library_object_id='duplicate');b=Object(wz09_library_object_id='duplicate')
        with self.assertRaises(ValueError):parts.repair_legacy_wrappers(root,[a,b])
        self.assertEqual(a['wz09_library_object_id'],b['wz09_library_object_id'])
    def test_world_geometry_is_eligible_without_display_name_matching(self):
        source=Object(wz09_preview_only=True);source.name='Misleading display label'
        self.assertEqual(parts.geometry_sources([source]),[source])
    def test_props_existing_parts_and_overlays_are_not_split_as_world(self):
        objects=[Object(wz09_preview_only=True,iw4l={'model':'vehicle'}),Object(wz09_preview_only=True,wz09_city_edit_part_id='part'),Object(wz09_preview_only=True,wz09_editor_overlay=True)]
        self.assertFalse(parts.geometry_sources(objects))
    def test_empty_source_markers_are_not_split_again(self):
        source=Object(wz09_city_edit_source_id='retained');source.data.polygons=[]
        self.assertFalse(parts.geometry_sources([source]))
    def test_prepared_flag_is_versioned_and_invalid_json_is_not_accepted(self):
        self.assertTrue(parts.ready({parts.READY:json.dumps({'format':parts.FORMAT})}))
        self.assertFalse(parts.ready({parts.READY:'broken'}));self.assertFalse(parts.ready({parts.READY:json.dumps({'format':'old'})}))
    def test_editing_one_shared_asset_makes_only_its_mesh_unique(self):
        class Mesh:
            users=2
            def copy(self):return Mesh()
        a,b=Object(iw4l={'model':'vehicle'}),Object(iw4l={'model':'vehicle'})
        shared=Mesh();a.data=shared;b.data=shared
        self.assertTrue(parts.make_asset_mesh_unique(a))
        self.assertIsNot(a.data,b.data);self.assertIs(b.data,shared)
        self.assertFalse(a.data.use_fake_user)
    def records(self,root):
        path=root/'prepared.blend';path.write_bytes(b'fixture library')
        source=dict(map_id='map-a',source_map='mp_invasion',layer='core',sha256='a'*64)
        prepared=dict(source,path=str(path),sha256=hashlib.sha256(path.read_bytes()).hexdigest(),editable_parts_format=parts.FORMAT)
        return source,prepared
    def test_exact_source_revision_reuses_cached_preparation(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary);source,prepared=self.records(root)
            parts.publish_cache(source,prepared,root)
            self.assertEqual(parts.resolve(source,root),prepared)
            self.assertEqual(parts.resolve(dict(source,sha256='b'*64),root),dict(source,sha256='b'*64))
    def test_two_map_instances_cannot_share_object_identity_cache(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary);source,prepared=self.records(root);parts.publish_cache(source,prepared,root)
            other=dict(source,map_id='map-b');self.assertEqual(parts.resolve(other,root),other)
    def test_changed_cache_is_rejected_and_missing_cache_can_be_rebuilt(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary);source,prepared=self.records(root);parts.publish_cache(source,prepared,root)
            path=Path(prepared['path']);path.write_bytes(b'changed')
            with self.assertRaises(ValueError):parts.resolve(source,root)
            path.unlink();self.assertEqual(parts.resolve(source,root),source)
    def test_cache_rejects_cross_map_record(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary);source,prepared=self.records(root)
            with self.assertRaises(ValueError):parts.publish_cache(source,dict(prepared,map_id='map-b'),root)

if __name__=='__main__':unittest.main(verbosity=2)
