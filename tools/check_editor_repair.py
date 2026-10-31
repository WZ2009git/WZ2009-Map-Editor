"""Regression checks for stable map labels and safe Blender panel registration."""
import importlib.util
from pathlib import Path
import sys
import types
import unittest
from unittest.mock import patch
ROOT=Path(__file__).resolve().parents[1]
package=types.ModuleType('repair_checks');package.__path__=[str(ROOT/'addons/wz2009_map_editor')];sys.modules[package.__name__]=package
def load(name):
    spec=importlib.util.spec_from_file_location(package.__name__+'.'+name,Path(package.__path__[0])/(name+'.py'))
    module=importlib.util.module_from_spec(spec);sys.modules[spec.name]=module;spec.loader.exec_module(module);return module
load('overview_preview');residency=load('map_residency')
class Object(dict):
    def __init__(self,name,kind='EMPTY',faces=0,**properties):
        super().__init__(properties);self.name=name;self.type=kind;self.data=types.SimpleNamespace(polygons=[None]*faces)
def root(name,identity,**props):return Object(name,wz09_type='map_instance',wz09_id=identity,**props)
def proxy(name,identity,faces):return Object(name,'MESH',faces,wz09_overview_map_id=identity)
class RepairTests(unittest.TestCase):
    def test_file_open_context_recovers_an_existing_window(self):
        overlay=types.SimpleNamespace(show_extras=True,show_relationship_lines=True,show_text=True)
        area=types.SimpleNamespace(type='VIEW_3D',spaces=types.SimpleNamespace(active=types.SimpleNamespace(overlay=overlay)))
        scene={'wz09_library_workspace':True,'wz09_workshop_view_initialized':True}
        ctx=types.SimpleNamespace(scene=scene,window=None,window_manager=types.SimpleNamespace(windows=[types.SimpleNamespace(screen=types.SimpleNamespace(areas=[area]))]))
        with patch.object(residency,'prepare_workshop_labels'):residency.initialize_workshop_view(ctx)
        self.assertFalse(overlay.show_text);self.assertFalse(overlay.show_extras)
        self.assertEqual(area.spaces.active.clip_end,20000)
    def test_saved_initialized_view_still_hides_legacy_object_names(self):
        overlay=types.SimpleNamespace(show_extras=True,show_relationship_lines=True,show_text=True)
        area=types.SimpleNamespace(type='VIEW_3D',spaces=types.SimpleNamespace(active=types.SimpleNamespace(overlay=overlay)))
        scene={'wz09_library_workspace':True,'wz09_workshop_view_initialized':True}
        ctx=types.SimpleNamespace(scene=scene,window=types.SimpleNamespace(screen=types.SimpleNamespace(areas=[area])))
        with patch.object(residency,'prepare_workshop_labels'):
            residency.initialize_workshop_view(ctx)
        self.assertFalse(overlay.show_extras);self.assertFalse(overlay.show_relationship_lines);self.assertFalse(overlay.show_text)
        self.assertEqual(area.spaces.active.clip_end,20000)
        area.spaces.active.clip_end=50000
        with patch.object(residency,'prepare_workshop_labels'):
            residency.initialize_workshop_view(ctx)
        self.assertEqual(area.spaces.active.clip_end,50000)
    def test_labels_follow_ids_even_when_object_names_are_misleading(self):
        favela=root('Rust','favela-id');rust=root('Favela','rust-id')
        a=proxy('unrelated A','rust-id',100);b=proxy('unrelated B','favela-id',200)
        result=residency.workshop_label_items(types.SimpleNamespace(objects=[favela,rust,a,b]))
        self.assertIs(result[0][1],b);self.assertIs(result[1][1],a)
    def test_copied_fragments_do_not_duplicate_map_labels(self):
        main=root('Favela','main');fragment=root('Favela.001','copy',wz09_original_map_instance_id='main')
        result=residency.workshop_label_items(types.SimpleNamespace(objects=[fragment,main,proxy('copy','copy',1),proxy('main','main',100)]))
        self.assertEqual(len(result),1);self.assertIs(result[0][0],main)
    def test_largest_proxy_wins_independent_of_object_order(self):
        owner=root('Map','id');small=proxy('small','id',2);large=proxy('large','id',1000)
        for order in ([owner,large,small],[small,owner,large]):
            self.assertIs(residency.workshop_label_items(types.SimpleNamespace(objects=order))[0][1],large)
    def test_register_does_not_rewrite_registered_rna_panel_callbacks(self):
        class Panel:
            @classmethod
            def __subclasses__(cls):raise AssertionError('Registered RNA panel introspection is unsafe here')
            @classmethod
            def poll(cls,ctx):return True
        original=Panel.__dict__['poll'];registered=[]
        bpy=types.ModuleType('bpy');props=types.ModuleType('bpy.props');props.BoolProperty=lambda **kwargs:False
        bpy.props=props;bpy.types=types.SimpleNamespace(Operator=type('Operator',(),{}),Scene=type('Scene',(),{}),Panel=Panel)
        bpy.app=types.SimpleNamespace(background=True)
        bpy.utils=types.SimpleNamespace(register_class=registered.append,unregister_class=registered.remove)
        with patch.dict(sys.modules,{'bpy':bpy,'bpy.props':props}):
            residency.register();self.assertEqual(len(registered),3);residency.unregister()
        self.assertEqual(registered,[]);self.assertIs(Panel.__dict__['poll'],original)
if __name__=='__main__':unittest.main(verbosity=2)
