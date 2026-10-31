"""One reusable isolated Blender process for WATER-HEIGHT-01 verification."""
import json
from pathlib import Path
import runpy
import time
import traceback
ROOT=Path(__file__).resolve().parents[1]
previous=None
deadline=time.monotonic()+7200
while time.monotonic()<deadline:
    request=json.loads((ROOT/'worklog/WATER_HEIGHT_REQUEST.json').read_text(encoding='utf-8-sig'))
    if request.get('stop'):break
    if request['id']==previous:time.sleep(.5);continue
    previous=request['id']
    try:
        result=runpy.run_path(str(ROOT/'tools/check_water_height_blender.py'))['main'](request)
        result.update(id=previous,status='PASS')
    except Exception:result=dict(id=previous,status='ERROR',traceback=traceback.format_exc())
    (ROOT/'worklog/WATER_HEIGHT_RESULT.json').write_text(json.dumps(result,indent=2),encoding='utf-8')
    print(json.dumps(result),flush=True)
