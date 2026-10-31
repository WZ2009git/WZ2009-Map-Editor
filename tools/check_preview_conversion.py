"""Pure checks for mixed native attribute formats in derived map previews."""
import importlib.util
from pathlib import Path
import unittest
from unittest.mock import patch
from types import SimpleNamespace
import numpy as np

ROOT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('preview_cache_checks',ROOT/'addons/wz2009_map_editor/city_view_cache.py')
cache=importlib.util.module_from_spec(spec);spec.loader.exec_module(cache)
spec=importlib.util.spec_from_file_location('preview_residency_checks',ROOT/'addons/wz2009_map_editor/map_residency.py')
residency=importlib.util.module_from_spec(spec);spec.loader.exec_module(residency)

def capture(domain='POINT',kind='BYTE_COLOR',name='Color'):
    values=np.arange(12,dtype=np.float32).reshape(3,4)/12
    return dict(co=np.zeros((3,3)),vertices=np.array([2,0,1]),totals=np.array([3]),
                attributes=[(name,domain,kind,'color',values)])

class PreviewAttributeTests(unittest.TestCase):
    def test_switching_maps_never_reads_evicted_detail_handles(self):
        class Object(dict):
            dead=False
            def get(self,*args):
                if self.dead:raise ReferenceError('StructRNA of type Object has been removed')
                return super().get(*args)
        current=Object(wz09_type='map_instance',wz09_id='current')
        target=Object(wz09_type='map_instance',wz09_id='target')
        detail=Object(wz09_id='detail');ctx=SimpleNamespace(scene=SimpleNamespace(objects=[current,detail,target]))
        def unload(ctx,obj):detail.dead=True
        with patch.object(residency,'resident',return_value=True),patch.object(residency,'unload',side_effect=unload) as saved:
            residency.unload_other_maps(ctx,target)
        saved.assert_called_once_with(ctx,current)
    def test_mixed_colour_formats_promote_to_linear_float_corners(self):
        a,b=capture(),capture('CORNER','FLOAT_COLOR')
        plan=cache.preview_attribute_plan([a,b])
        self.assertEqual(plan['Color']['schema'],('CORNER','FLOAT_COLOR','color',4))
        np.testing.assert_array_equal(cache.preview_attribute_values(a,plan['Color'],np),a['attributes'][0][4][[2,0,1]])
        np.testing.assert_array_equal(cache.preview_attribute_values(b,plan['Color'],np),b['attributes'][0][4])
    def test_missing_colour_has_correct_corner_length(self):
        a,b=capture(),capture('CORNER','FLOAT_COLOR')
        missing=dict(a,attributes=[],vertices=np.array([0,1,2,0,2,1]),totals=np.array([3,3]))
        values=cache.preview_attribute_values(missing,cache.preview_attribute_plan([a,b])['Color'],np)
        self.assertEqual(values.shape,(6,4));self.assertFalse(values.any())
    def test_missing_boolean_channel_uses_a_boolean_buffer(self):
        data=capture();data['attributes']=[('Mask','FACE','BOOLEAN','value',np.ones((1,1),dtype=np.bool_))]
        plan=cache.preview_attribute_plan([data])
        values=cache.preview_attribute_values(dict(data,attributes=[]),plan['Mask'],np)
        self.assertEqual(values.dtype,np.bool_);self.assertEqual(values.shape,(1,1));self.assertFalse(values.any())
    def test_homogeneous_attribute_layout_is_preserved(self):
        a=capture();plan=cache.preview_attribute_plan([a])
        self.assertEqual(plan['Color']['schema'],('POINT','BYTE_COLOR','color',4))
        self.assertIs(cache.preview_attribute_values(a,plan['Color'],np),a['attributes'][0][4])
    def test_unrelated_incompatible_channels_keep_both_variants(self):
        a=capture('FACE','FLOAT_COLOR');b=capture('CORNER','FLOAT_COLOR')
        plan=cache.preview_attribute_plan([a,b])
        self.assertEqual(len(plan),2)
        self.assertEqual({row['schema'][0] for row in plan.values()},{'FACE','CORNER'})
    def test_generated_alias_does_not_overwrite_a_real_attribute(self):
        a,b=capture('FACE','FLOAT_COLOR'),capture('CORNER','FLOAT_COLOR')
        c=capture(name='Color__FACE_FLOAT_COLOR')
        plan=cache.preview_attribute_plan([a,b,c])
        self.assertEqual(len(plan),3);self.assertIn('Color__FACE_FLOAT_COLOR_',plan)
    def test_source_colour_buffers_are_unchanged(self):
        a,b=capture(),capture('CORNER','FLOAT_COLOR');before=a['attributes'][0][4].copy()
        plan=cache.preview_attribute_plan([a,b]);cache.preview_attribute_values(a,plan['Color'],np)
        np.testing.assert_array_equal(a['attributes'][0][4],before)

if __name__=='__main__':unittest.main(verbosity=2)
