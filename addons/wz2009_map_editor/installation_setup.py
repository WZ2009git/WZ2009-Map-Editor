"""Portable first-run installation selection and verified local asset cache.

Only editor-local files are written. Original FastFiles are read-only. Game
assets never come from a web download; the optional offline tool is hash-bound.
"""
import hashlib
import json
import math
import os
from pathlib import Path
import shutil
import subprocess
import threading
import time
import urllib.request
import uuid
import zipfile

PROJECT='WZ2009-Map-Editor'
REPOSITORY='WZ2009git/'+PROJECT
SOCIAL='https://x.com/WZ_2009'
_CLASSES=[]
_JOB=None


def _refresh():
    import bpy
    for window in bpy.context.window_manager.windows:
        for area in window.screen.areas:
            if area.type=='VIEW_3D': area.tag_redraw()
    return .5 if _JOB is not None and not _JOB.get('finished',False) else None


def project_root(): return Path(__file__).resolve().parents[2]


def is_public_project(root=None):
    return ((Path(root) if root else project_root())/'project/manifest.json').is_file()


def read_json(path):
    path=Path(path)
    if path.stat().st_size>32*1024*1024: raise ValueError('Setup metadata exceeds 32 MiB')
    return json.loads(path.read_text(encoding='utf-8-sig'))


def digest(path):
    h=hashlib.sha256()
    with Path(path).open('rb') as stream:
        for chunk in iter(lambda:stream.read(1048576),b''): h.update(chunk)
    return h.hexdigest()


def local_path(root,relative):
    root=Path(root).resolve(); path=(root/relative).resolve()
    if not path.is_relative_to(root) or path==root: raise ValueError('Setup path escapes the editor folder')
    return path


def atomic_json(path,value):
    path=Path(path); path.parent.mkdir(parents=True,exist_ok=True)
    temporary=path.with_name(path.name+'.'+uuid.uuid4().hex+'.tmp')
    try:
        with temporary.open('x',encoding='utf-8') as stream: json.dump(value,stream,indent=2,allow_nan=False)
        os.replace(temporary,path)
    finally:
        if temporary.exists(): temporary.unlink()


def manifest(root=None):
    root=Path(root or project_root()).resolve()
    value=read_json(local_path(root,'project/manifest.json'))
    if value.get('kind')!='wz09-public-project-v1': raise ValueError('Unsupported public project manifest')
    sources=value.get('sources')
    if not isinstance(sources,list) or not sources: raise ValueError('Project has no original asset requirements')
    from .original_assets import _requirement
    for row in sources: _requirement(row)
    if len({row['source_map'] for row in sources})!=len(sources): raise ValueError('Duplicate original asset requirement')
    maps=value.get('maps')
    if not isinstance(maps,list) or not maps: raise ValueError('Project has no map instances')
    identities=set()
    for row in maps:
        identity=row.get('id')
        try: valid=str(uuid.UUID(identity))==identity
        except (TypeError,ValueError,AttributeError): valid=False
        if not valid or identity in identities: raise ValueError('Map IDs must be distinct canonical UUIDs')
        identities.add(identity)
        if row.get('source_map') not in {s['source_map'] for s in sources}: raise ValueError('Map references an undeclared original source')
        matrix=row.get('matrix_world')
        if not isinstance(matrix,list) or len(matrix)!=4 or any(not isinstance(r,list) or len(r)!=4 for r in matrix): raise ValueError('Map placement must be a 4 by 4 matrix')
        if any(type(n) not in (int,float) or not math.isfinite(n) for r in matrix for n in r): raise ValueError('Map placement contains invalid coordinates')
        if row.get('recipe'):
            path=local_path(root/'project',row['recipe'])
            if digest(path)!=row['recipe_sha256']: raise ValueError('Map reconstruction metadata checksum mismatch')
            row['objects']=read_json(path)['objects']
    return value


def select_installation(directory,root=None):
    from .original_assets import verify_requirements
    root=Path(root or project_root()).resolve(); install=Path(directory).expanduser().resolve()
    if root==install or root.is_relative_to(install):
        raise ValueError('Keep the editor folder outside the MW2009 installation; game files remain read-only')
    recipe=manifest(root)
    verified=verify_requirements(install,recipe['sources'])
    profile=dict(kind='wz09-local-install-v1',installation=str(install),project_sha256=digest(root/'project/manifest.json'),
                 sources={code:dict(path=str(path),sha256=digest(path)) for code,path in verified.items()})
    atomic_json(local_path(root,'.local/installation.json'),profile)
    return profile


def profile(root=None,verify=False):
    root=Path(root or project_root()).resolve()
    value=read_json(local_path(root,'.local/installation.json'))
    if value.get('kind')!='wz09-local-install-v1': raise ValueError('Unsupported installation profile')
    if value.get('project_sha256')!=digest(root/'project/manifest.json'): raise ValueError('Project changed; select and verify the installation again')
    if verify: return select_installation(value['installation'],root)
    return value


def backend(root=None,*,download=False):
    root=Path(root or project_root()).resolve(); lock=read_json(root/'project/backend.json')
    expected=lock['executable_sha256']
    if len(expected)!=64 or any(c not in '0123456789abcdef' for c in expected): raise ValueError('Invalid offline tool hash')
    executable=local_path(root,'.local/tools/'+expected+'/wz09-asset-tool.exe')
    if executable.exists():
        if digest(executable)!=expected: raise ValueError('Cached offline converter changed; remove this tool cache and run setup again')
        return executable
    bundled=root/'tools/bin/wz09-asset-tool.exe'
    if bundled.is_file() and digest(bundled)==expected:
        executable.parent.mkdir(parents=True,exist_ok=True)
        if not executable.exists():
            with bundled.open('rb') as source,executable.open('xb') as target: shutil.copyfileobj(source,target)
        return executable
    if not download: raise FileNotFoundError('The offline conversion tool has not been downloaded yet')
    url=lock['archive_url']
    if not url.startswith('https://github.com/'+REPOSITORY+'/releases/download/'):
        raise ValueError('Offline tool download is not from the project release repository')
    temporary=local_path(root,'.local/downloads/'+uuid.uuid4().hex+'.zip')
    temporary.parent.mkdir(parents=True,exist_ok=True)
    try:
        with urllib.request.urlopen(urllib.request.Request(url,headers={'User-Agent':PROJECT}),timeout=90) as response,temporary.open('xb') as target:
            size=0
            while chunk:=response.read(1048576):
                size+=len(chunk)
                if size>256*1024*1024: raise ValueError('Offline tool download exceeds 256 MiB')
                target.write(chunk)
        if digest(temporary)!=lock['archive_sha256']: raise ValueError('Offline tool archive checksum mismatch')
        with zipfile.ZipFile(temporary) as archive:
            allowed={'bin/wz09-asset-tool.exe','LICENSE','NOTICE','PROVENANCE.json'}
            if set(archive.namelist())!=allowed: raise ValueError('Offline tool archive contains unexpected files')
            for name,target,expected_hash in (('bin/wz09-asset-tool.exe',executable,expected),):
                member=archive.getinfo(name)
                if member.file_size>256*1024*1024: raise ValueError('Offline converter exceeds the allowed size')
                if target.exists():
                    if digest(target)!=expected_hash: raise ValueError('Cached conversion tool checksum mismatch')
                    continue
                target.parent.mkdir(parents=True,exist_ok=True)
                with archive.open(member) as source,target.open('xb') as sink: shutil.copyfileobj(source,sink)
                if digest(target)!=expected_hash:
                    target.unlink(); raise ValueError('Offline conversion tool checksum mismatch')
        if digest(executable)!=expected:
            executable.unlink(); raise ValueError('Offline converter checksum mismatch')
        return executable
    finally:
        if temporary.exists(): temporary.unlink()


def native_source(root,asset_root=None):
    """Portable equivalent of the existing machine-specific exporter audit."""
    root=Path(root).resolve(); data=profile(root)
    install=Path(asset_root or data['installation']).resolve()
    if install!=Path(data['installation']).resolve(): raise ValueError('Selected installation differs from the verified setup profile')
    executable=backend(root)
    lock=read_json(root/'project/backend.json')
    return dict(executable=str(executable),sha256=lock['executable_sha256'],asset_root=str(install),
                backend_version=lock['version'],source_sha256=lock.get('source_sha256',{}),
                evidence_path=str(root/'project/backend.json'),evidence_sha256=digest(root/'project/backend.json'))


def prepare(root=None,status=None,cancel=None):
    """Run the existing converter sequentially; no Blender calls on a worker."""
    from . import pipeline_workflow,source_bundle
    root=Path(root or project_root()).resolve()
    notify=status or (lambda **values:None)
    selected=profile(root,verify=True); project=manifest(root)
    notify(phase='TOOL',message='Preparing the verified offline conversion tool')
    backend(root,download=True)
    tool_hash=read_json(root/'project/backend.json')['executable_sha256']
    expected_references={row['source_map']:row['calibration']['registration']['reference_sha256'] for row in project['maps']}
    results={}
    index_path=local_path(root,'.local/assets.json')
    if index_path.exists(): results=read_json(index_path).get('maps',{})
    from .scene_io import MAPS
    maps=[r for r in project['sources'] if r['source_map'] in MAPS]
    for number,row in enumerate(maps,1):
        if cancel and cancel.is_set(): raise InterruptedError('Setup cancelled between maps; verified cached maps are retained')
        code=row['source_map']; notify(phase='CONVERT',map=code,done=number-1,total=len(maps),message='Preparing '+code)
        cached=results.get(code)
        if cached:
            try:
                source_bundle.verify_preview_bundle(cached['preview_bundle'])
                source_bundle.verify_preview_bundle(cached['supplemental_bundle'])
                if (cached.get('native_sha256')==row['native_sha256'] and cached.get('tool_sha256')==tool_hash
                        and cached['preview_bundle']['reference_sha256']==expected_references[code]): continue
            except (OSError,ValueError,KeyError): pass
        report=export_map(root,code,selected['installation'],_verified_profile=selected)
        if report['preview_bundle']['reference_sha256']!=expected_references[code]:
            raise ValueError('Converted '+code+' differs from the project reference; this source/converter combination is unsupported')
        supplemental=prepare_semantic_visuals(root,code,selected)
        results[code]=dict(native_sha256=row['native_sha256'],tool_sha256=tool_hash,preview_bundle=report['preview_bundle'],supplemental_bundle=supplemental)
        atomic_json(index_path,dict(kind='wz09-local-assets-v1',maps=results))
    return index_path


def prepare_semantic_visuals(root,code,selected):
    from . import pipeline_workflow,source_bundle
    executable=backend(root)
    job=local_path(root,'.local/semantic-'+code+'-'+uuid.uuid4().hex)
    job.mkdir(); temporary=job/'tmp'; temporary.mkdir()
    source=Path(selected['sources'][code]['path']); common=Path(selected['sources']['common_mp']['path'])
    command=[str(executable),'models',str(source),str(common),str(job/'bundle'),selected['sources'][code]['sha256']]
    pipeline_workflow._run(command,job,pipeline_workflow._environment(temporary,selected['installation']),job/'convert.log',timeout=300)
    for key,path in ((code,source),('common_mp',common)):
        if digest(path)!=selected['sources'][key]['sha256']: raise ValueError('Original game file changed during local preparation')
    return source_bundle.capture_gltf_bundle(job/'bundle/scene.gltf',include_images=True)


def export_map(root,code,asset_root=None,*,_verified_profile=None):
    """Convert verified local FastFiles with the standalone asset-only tool."""
    from . import pipeline_workflow,source_bundle
    root=Path(root).resolve()
    selected=_verified_profile or profile(root,verify=True)
    if asset_root and Path(asset_root).resolve()!=Path(selected['installation']).resolve():
        raise ValueError('Selected installation differs from the verified setup profile')
    executable=backend(root)
    job=local_path(root,'.local/map-'+code+'-'+uuid.uuid4().hex)
    job.mkdir(); temporary=job/'tmp'; temporary.mkdir()
    source=Path(selected['sources'][code]['path']); common=Path(selected['sources']['common_mp']['path'])
    command=[str(executable),'map',str(source),str(common),str(job/'bundle'),selected['sources'][code]['sha256']]
    pipeline_workflow._run(command,job,pipeline_workflow._environment(temporary,selected['installation']),job/'convert.log',timeout=300)
    for key,path in ((code,source),('common_mp',common)):
        if digest(path)!=selected['sources'][key]['sha256']: raise ValueError('Original game file changed during local preparation')
    scenes=list((job/'bundle/exports'/code).glob('p1a-*/scene.gltf'))
    if len(scenes)!=1: raise ValueError('Offline converter did not produce exactly one map scene')
    return dict(status='PASS',game_started=False,reference_path=str(scenes[0]),
                preview_bundle=source_bundle.capture_gltf_bundle(scenes[0],include_images=True))


def draw(layout,ctx):
    box=layout.box(); box.label(text='Welcome to WZ2009 Map Editor',icon='WORLD')
    box.label(text='1. Select your MW2009 installation.')
    box.label(text='2. Prepare assets locally.')
    box.label(text='3. Open the current map layout.')
    box.operator('wz09.select_mw2009_install',text='Select MW2009 folder',icon='FILE_FOLDER')
    if _JOB:
        import textwrap
        width=max(22,int((getattr(ctx.region,'width',320)-50)/7))
        for line in textwrap.wrap(_JOB.get('message','Preparing assets ...'),width=width): box.label(text=line)
    box.operator('wz09.prepare_local_assets',text='Prepare local assets',icon='IMPORT')
    if (project_root()/'.local/ready.json').is_file(): box.operator('wz09.open_prepared_workspace',text='Open prepared workspace',icon='FILE_BLEND')
    box.label(text='First setup may take several minutes.')
    box.label(text='Verified conversions are reused on retry.')
    box.operator('wm.url_open',text='Project updates: @WZ_2009',icon='URL').url=SOCIAL


def register():
    import bpy
    from bpy.props import StringProperty
    class WZ09_OT_select_install(bpy.types.Operator):
        bl_idname='wz09.select_mw2009_install';bl_label='Select MW2009 installation'
        bl_description='Choose the game folder containing zone and main. Verify required original files without modifying the installation'
        directory:StringProperty(subtype='DIR_PATH')
        filter_folder:bpy.props.BoolProperty(default=True,options={'HIDDEN'})
        def invoke(self,ctx,event): ctx.window_manager.fileselect_add(self); return {'RUNNING_MODAL'}
        def execute(self,ctx):
            try:
                selected=select_installation(self.directory)
                ctx.scene.wz09_asset_root=selected['installation']
                self.report({'INFO'},'Installation verified. Prepare local assets next.'); return {'FINISHED'}
            except (OSError,ValueError) as exc:
                self.report({'ERROR'},str(exc)[:230]); return {'CANCELLED'}
    class WZ09_OT_prepare_assets(bpy.types.Operator):
        bl_idname='wz09.prepare_local_assets';bl_label='Prepare local assets'
        bl_description='Convert required assets from your verified installation into an editor-local cache. The game is not started'
        @classmethod
        def poll(cls,ctx): return _JOB is None or _JOB.get('finished',False)
        def execute(self,ctx):
            global _JOB
            _JOB={'message':'Starting local setup ...','finished':False}
            blender=bpy.app.binary_path
            def worker():
                try:
                    path=prepare(status=lambda **values:_JOB.update(values))
                    output=local_path(project_root(),'.local/workspaces/'+uuid.uuid4().hex)
                    command=[blender,'-b','--factory-startup','--python-exit-code','2','--python',str(project_root()/'tools/public_reconstruct.py'),'--','--output',str(output)]
                    _JOB.update(message='Reconstructing your local map workspace ...')
                    with (project_root()/'.local/reconstruction.log').open('w',encoding='utf-8') as log:
                        process=subprocess.Popen(command,cwd=project_root(),stdout=log,stderr=subprocess.STDOUT,
                                                 creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
                        while process.poll() is None:
                            progress=project_root()/'.local/progress.json'
                            if progress.is_file():
                                try: _JOB.update(read_json(progress))
                                except (OSError,ValueError): pass
                            time.sleep(.5)
                        if process.returncode: raise ValueError('Workspace reconstruction failed. See .local/reconstruction.log; verified assets were retained')
                    _JOB.update(message='Ready. Open the prepared workspace.',assets=str(path),finished=True)
                except Exception as exc: _JOB.update(message=str(exc)[:220],finished=True,error=True)
            threading.Thread(target=worker,daemon=True,name='WZ2009-local-asset-setup').start()
            if not bpy.app.timers.is_registered(_refresh): bpy.app.timers.register(_refresh,first_interval=.2)
            return {'FINISHED'}
    class WZ09_OT_open_workspace(bpy.types.Operator):
        bl_idname='wz09.open_prepared_workspace';bl_label='Open prepared workspace'
        bl_description='Open the locally reconstructed map with its asset browser and per-map detail libraries'
        def execute(self,ctx):
            try:
                if bpy.data.filepath and bpy.data.is_dirty: raise ValueError('Save your current Blender file before opening the prepared workspace')
                ready=read_json(project_root()/'.local/ready.json')
                if ready['project_sha256']!=digest(project_root()/'project/manifest.json'): raise ValueError('Project recipe changed; run asset setup again')
                path=local_path(project_root(),ready['path'])
                if path.suffix!='.blend' or not path.is_file(): raise ValueError('Prepared Blender workspace is missing')
                bpy.ops.wm.open_mainfile(filepath=str(path))
                from .map_residency import initialize_workshop_view
                initialize_workshop_view(bpy.context)
                return {'FINISHED'}
            except (OSError,ValueError,KeyError) as exc:
                self.report({'ERROR'},str(exc)[:230]); return {'CANCELLED'}
    class WZ09_OT_browse_assets(bpy.types.Operator):
        bl_idname='wz09.browse_local_assets';bl_label='Browse local assets'
        bl_description='Open the native asset catalog. Meshes and textures are loaded from your verified local cache only when needed'
        def execute(self,ctx):
            ctx.scene.wz09_map_advanced=True;ctx.scene.wz09_workspace='ASSETS'
            return {'FINISHED'}
    for cls in (WZ09_OT_select_install,WZ09_OT_prepare_assets,WZ09_OT_open_workspace,WZ09_OT_browse_assets): bpy.utils.register_class(cls); _CLASSES.append(cls)


def unregister():
    import bpy
    if bpy.app.timers.is_registered(_refresh): bpy.app.timers.unregister(_refresh)
    for cls in reversed(_CLASSES): bpy.utils.unregister_class(cls)
    _CLASSES.clear()
