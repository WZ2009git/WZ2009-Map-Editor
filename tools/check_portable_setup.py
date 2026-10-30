"""Synthetic portable-setup checks; never loads Blender or real game files."""
import hashlib
import io
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import types
import unittest
import uuid
import zipfile
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
PACKAGE='portable_setup_checks'
package=types.ModuleType(PACKAGE)
package.__path__=[str(ROOT/'addons/wz2009_map_editor')]
sys.modules[PACKAGE]=package
def module(name):
    spec=importlib.util.spec_from_file_location(PACKAGE+'.'+name,Path(package.__path__[0])/(name+'.py'))
    value=importlib.util.module_from_spec(spec);sys.modules[spec.name]=value;spec.loader.exec_module(value)
    return value
original=module('original_assets');setup=module('installation_setup')
workspace=module('public_workspace')

class PortableSetupTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)/'editor';self.install=Path(self.temp.name)/'game'
        zone=self.install/'zone/english';zone.mkdir(parents=True)
        self.source=zone/'mp_rust.ff';self.source.write_bytes(b'synthetic source, not game data')
        self.document=dict(kind='wz09-public-project-v1',sources=[dict(source_map='mp_rust',native_sha256=setup.digest(self.source))],
                           maps=[dict(id=str(uuid.uuid4()),source_map='mp_rust',matrix_world=[[int(r==c) for c in range(4)] for r in range(4)])])
        self.write_manifest()
    def write_manifest(self): setup.atomic_json(self.root/'project/manifest.json',self.document)
    def test_select_and_reverify(self):
        selected=setup.select_installation(self.install,self.root)
        self.assertEqual(selected,setup.profile(self.root,verify=True))
        self.assertEqual(self.source.read_bytes(),b'synthetic source, not game data')
    def test_mismatch_leaves_no_profile(self):
        self.source.write_bytes(b'wrong edition')
        with self.assertRaises(original.OriginalAssetMismatch): setup.select_installation(self.install,self.root)
        self.assertFalse((self.root/'.local/installation.json').exists())
    def test_missing_source(self):
        self.source.unlink()
        with self.assertRaises(original.OriginalAssetMissing): setup.select_installation(self.install,self.root)
    def test_project_change_requires_reverification(self):
        setup.select_installation(self.install,self.root)
        self.document['maps'][0]['name']='Changed';self.write_manifest()
        with self.assertRaisesRegex(ValueError,'Project changed'): setup.profile(self.root)
    def test_path_containment(self):
        with self.assertRaises(ValueError): setup.local_path(self.root,'../outside')
        with self.assertRaises(ValueError): setup.select_installation(self.root.parent,self.root)
    def test_duplicate_ids_and_nonfinite_matrix(self):
        self.document['maps'].append(dict(self.document['maps'][0]));self.write_manifest()
        with self.assertRaisesRegex(ValueError,'Map IDs'): setup.manifest(self.root)
        self.document['maps'].pop();self.document['maps'][0]['matrix_world'][0][0]='NaN';self.write_manifest()
        with self.assertRaisesRegex(ValueError,'invalid coordinates'): setup.manifest(self.root)
    def test_recipe_hash_binding(self):
        path=self.root/'project/maps/one.json';setup.atomic_json(path,dict(objects=[]))
        self.document['maps'][0].update(recipe='maps/one.json',recipe_sha256=setup.digest(path));self.write_manifest()
        self.assertEqual(setup.manifest(self.root)['maps'][0]['objects'],[])
        setup.atomic_json(path,dict(objects=[{}]))
        with self.assertRaisesRegex(ValueError,'checksum'): setup.manifest(self.root)
    def test_cached_tool_tampering(self):
        expected=hashlib.sha256(b'verified').hexdigest()
        setup.atomic_json(self.root/'project/backend.json',dict(executable_sha256=expected))
        path=self.root/'.local/tools'/expected/'wz09-asset-tool.exe';path.parent.mkdir(parents=True);path.write_bytes(b'changed')
        with self.assertRaisesRegex(ValueError,'converter changed'): setup.backend(self.root)
    def test_untrusted_download_origin(self):
        setup.atomic_json(self.root/'project/backend.json',dict(executable_sha256='a'*64,archive_url='https://example.com/tool.zip'))
        with self.assertRaisesRegex(ValueError,'release repository'): setup.backend(self.root,download=True)
    def test_archive_rejects_extra_paths(self):
        data=io.BytesIO()
        with zipfile.ZipFile(data,'w') as archive: archive.writestr('../outside.exe',b'synthetic')
        raw=data.getvalue()
        setup.atomic_json(self.root/'project/backend.json',dict(executable_sha256='a'*64,
            archive_sha256=hashlib.sha256(raw).hexdigest(),archive_url='https://github.com/'+setup.REPOSITORY+'/releases/download/test/tool.zip'))
        with patch.object(setup.urllib.request,'urlopen',return_value=io.BytesIO(raw)):
            with self.assertRaisesRegex(ValueError,'unexpected files'): setup.backend(self.root,download=True)
        self.assertFalse((self.root.parent/'outside.exe').exists())
        self.assertFalse(list((self.root/'.local/downloads').glob('*.zip')))
    def test_atomic_failure_preserves_file(self):
        path=self.root/'existing.json';setup.atomic_json(path,dict(valid=True))
        with self.assertRaises(ValueError): setup.atomic_json(path,dict(invalid=float('nan')))
        self.assertEqual(json.loads(path.read_text()),dict(valid=True))
        self.assertFalse(list(path.parent.glob('*.tmp')))
    def test_eviction_preserves_live_and_foreign_dependencies(self):
        class ID:
            def __init__(self): self.use_fake_user=True
            def as_pointer(self): return id(self)
        scene=ID();scene.collection=ID()
        foreign=ID();foreign.node_tree=ID()
        proxy=ID();mesh=ID();unused_mesh=ID();unused_image=ID();foreign_image=ID()
        values=[scene.collection,foreign,proxy,mesh,unused_mesh,unused_image,foreign_image]
        users={proxy:{scene.collection},mesh:{proxy},foreign_image:{foreign.node_tree}}
        removed=[]
        bpy=types.SimpleNamespace(data=types.SimpleNamespace(user_map=lambda:users,batch_remove=lambda *,ids:removed.extend(ids)))
        residency=types.ModuleType(PACKAGE+'.map_residency');residency.all_ids=lambda:iter(values)
        with patch.dict(sys.modules,{'bpy':bpy,PACKAGE+'.map_residency':residency}):
            workspace._evict_temporary_data({foreign.as_pointer()},scene)
        self.assertEqual(set(removed),{unused_mesh,unused_image})
        self.assertFalse(unused_image.use_fake_user)
        self.assertTrue(foreign_image.use_fake_user)
    def _export_fixture(self,tamper=False):
        common=self.source.with_name('common_mp.ff');common.write_bytes(b'synthetic common')
        self.document['sources'].append(dict(source_map='common_mp',native_sha256=setup.digest(common)))
        self.write_manifest();setup.select_installation(self.install,self.root)
        pipeline=types.ModuleType(PACKAGE+'.pipeline_workflow')
        source_bundle=types.ModuleType(PACKAGE+'.source_bundle')
        source_bundle.capture_gltf_bundle=lambda path,**kw:dict(reference_path=str(path))
        calls=[]
        def run(command,cwd,environment,log,**kwargs):
            calls.append((command,cwd))
            output=Path(command[4])/'exports/mp_rust/p1a-synthetic/scene.gltf'
            output.parent.mkdir(parents=True);output.write_text('{}')
            if tamper:self.source.write_bytes(b'changed while converting')
        pipeline._run=run;pipeline._environment=lambda *args:{}
        with patch.dict(sys.modules,{PACKAGE+'.pipeline_workflow':pipeline,PACKAGE+'.source_bundle':source_bundle}),patch.object(setup,'backend',return_value=self.root/'verified-tool.exe'):
            result=setup.export_map(self.root,'mp_rust',self.install)
        return result,calls
    def test_public_export_uses_asset_only_command_and_shared_local_cache(self):
        result,calls=self._export_fixture()
        command,cwd=calls[0]
        self.assertEqual(command[1],'map')
        self.assertEqual(Path(command[2]),self.source)
        self.assertEqual(cwd,self.root/'.local/converter')
        self.assertTrue(Path(command[4]).is_relative_to(self.root/'.local'))
        self.assertFalse(result['game_started'])
    def test_source_change_during_conversion_is_rejected(self):
        with self.assertRaisesRegex(ValueError,'changed during local preparation'):self._export_fixture(tamper=True)

if __name__=='__main__': unittest.main(verbosity=2)
