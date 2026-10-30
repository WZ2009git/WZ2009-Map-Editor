"""Explicit editor audition of proven original PCM; never starts a game.

Only the chosen alias variant is played. Streamed/unsupported native formats
remain unresolved. The source FF, captured manifest and actual WAV are checked
before audition; no guessed aliases or synthesized replacements are used.
"""
import json
import os
from pathlib import Path
import re
import tempfile
import wave
import zipfile

try:
    from . import native_semantics as ns
    from .authoring_helpers import resolve_owner_map
except ImportError:
    import native_semantics as ns
    from authoring_helpers import resolve_owner_map

_CLASSES=[]
_device=None
_handle=None


def _contained(base,value):
    path=Path(value)
    path=(base/path).resolve() if not path.is_absolute() else path.resolve()
    if not path.is_relative_to(base):raise ValueError('Audio payload escapes its native export folder')
    return path


def _audition_path(path,digest,visual_path):
    """Return verified original bytes at a short editor-local playback path.

    Python reads long Windows filenames via ``file_io_path``. Native audio
    readers need a short ordinary filename, so only that case creates a
    content-addressed derivative. No source or manifest is overwritten.
    """
    path=Path(path).resolve()
    if os.name!='nt' or len(str(path))<248:return path
    if not re.fullmatch('[0-9a-f]{64}',str(digest)):
        raise ValueError('Invalid original audio payload SHA-256')
    visual_path=Path(visual_path).resolve()
    root=next((parent for parent in visual_path.parents
        if (parent/'AGENTS.md').is_file() and (parent/'schema'/'world-v1.schema.json').is_file()),None)
    if root is None or not path.is_relative_to(root):
        raise ValueError('Long-path audio preview requires the standalone editor export folder')
    if path.suffix.lower() not in {'.wav','.mp3'}:
        raise ValueError('Unsupported original audition file extension')
    # Recheck the source even on a cache hit. Cache bytes never replace the
    # original payload's evidence, archive or source hash checks.
    if ns.file_sha256(path)!=digest:
        raise ValueError('Original audio payload changed before audition caching')
    directory=root/'exports'/'audio-preview-cache'
    if not directory.resolve().is_relative_to(root):
        raise ValueError('Audio preview cache escapes the standalone editor')
    directory.mkdir(parents=True,exist_ok=True)
    if directory.is_symlink() or not directory.resolve().is_relative_to(root):
        raise ValueError('Audio preview cache must be local to the standalone editor')
    target=directory/(digest+path.suffix.lower())
    if len(str(target))>=248:
        raise ValueError('Editor folder is too long for a native audio preview cache')
    def verify_target():
        if target.is_symlink() or not target.resolve().is_relative_to(root):
            raise ValueError('Audio preview cache entry is not local')
        if ns.file_sha256(target)!=digest:
            raise ValueError('Audio preview cache hash changed; remove the damaged cache entry explicitly')
        return target
    if target.exists():return verify_target()
    fd,temporary=tempfile.mkstemp(prefix='.'+digest+'-',suffix='.tmp',dir=directory)
    temporary=Path(temporary)
    try:
        import hashlib
        hashed=hashlib.sha256();size=0
        with os.fdopen(fd,'wb') as destination,open(ns.file_io_path(path),'rb') as source:
            for chunk in iter(lambda:source.read(1024*1024),b''):
                size+=len(chunk)
                if size>256*1024*1024:raise ValueError('Original audio exceeds the audition cache limit')
                hashed.update(chunk);destination.write(chunk)
            destination.flush();os.fsync(destination.fileno())
        if hashed.hexdigest()!=digest:
            raise ValueError('Original audio changed while copying to the audition cache')
        # A same-directory hard link publishes the fully written file
        # atomically and exclusively. Concurrent readers never see a partial
        # cache entry, and an existing file can never be replaced.
        try:os.link(temporary,target)
        except FileExistsError:pass
        return verify_target()
    finally:
        temporary.unlink(missing_ok=True)


def resolve_payload(visual_path,visual_sha,alias,variant,ff_sha):
    visual_path=Path(visual_path).resolve()
    if ns.file_sha256(visual_path)!=visual_sha:raise ValueError('Native visual/audio manifest changed')
    visual=json.loads(visual_path.read_text(encoding='utf-8'))
    if (visual.get('kind')!='offline_native_semantic_visual_export' or visual.get('status')!='PASS'
            or visual.get('game_started') is not False or visual.get('game_workspace_written') is not False
            or visual.get('native_source_sha256')!=ff_sha):
        raise ValueError('Audio export belongs to another original FF or is not a passing offline capture')
    for source in visual.get('audio_source_files',[]):
        target=Path(source.get('path',''))
        if not target.is_absolute() or ns.file_sha256(target)!=source.get('sha256'):
            raise ValueError('Original shared sound-bank FF is missing or changed')
    audio_path=_contained(visual_path.parent,visual.get('native_audio_manifest',''))
    if ns.file_sha256(audio_path)!=visual.get('native_audio_manifest_sha256'):
        raise ValueError('Native PCM manifest changed')
    data=json.loads(audio_path.read_text(encoding='utf-8'))
    if (data.get('kind')!='wz09_native_used_alias_pcm' or data.get('version')!=1
            or data.get('source_map')!=visual.get('source_map') or data.get('native_source_sha256')!=ff_sha):
        raise ValueError('Native PCM source identity differs')
    if type(variant) is not int or variant<0:raise ValueError('Explicit nonnegative alias variant required')
    found=[r for r in data.get('records',[]) if r.get('alias_ref')==alias and r.get('variant')==variant]
    if len(found)!=1:raise ValueError('This alias variant was not captured')
    record=found[0]
    if record.get('status') not in {'exact_pcm_wav','exact_iwd_pcm_wav','exact_iwd_mp3'}:
        raise ValueError('No verified PCM preview: '+str(record.get('status','unresolved')))
    path=_contained(audio_path.parent,record.get('payload',''))
    bound=[r for r in visual.get('audio_payloads',[]) if _contained(visual_path.parent,r.get('path',''))==path]
    if len(bound)!=1 or ns.file_sha256(path)!=bound[0].get('sha256'):
        raise ValueError('Native WAV payload is missing or changed')
    if record['status'] in {'exact_iwd_pcm_wav','exact_iwd_mp3'}:
        provenance=record.get('source_iwd',{})
        archive=Path(provenance.get('path',''))
        if not archive.is_absolute() or ns.file_sha256(archive)!=provenance.get('sha256'):
            raise ValueError('Original sound IWD archive is missing or changed')
        with zipfile.ZipFile(archive) as z:
            info=z.getinfo(provenance['entry'])
            crc=provenance.get('entry_crc')
            if isinstance(crc,str):
                import re
                if not re.fullmatch('[0-9a-f]{8}',crc):raise ValueError('Invalid original IWD entry CRC')
                crc=int(crc,16)
            if info.CRC!=crc or info.file_size>256*1024*1024:
                raise ValueError('Native IWD sound entry differs')
            import hashlib
            digest=hashlib.sha256(z.read(info)).hexdigest()
            if digest!=provenance.get('entry_sha256') or digest!=bound[0]['sha256']:
                raise ValueError('WAV is not the exact original IWD entry')
    if record['status']=='exact_iwd_mp3':
        import math
        duration=record.get('duration_s')
        if (record.get('codec')!='mp3' or record.get('channels') not in {1,2}
                or type(record.get('sample_rate')) is not int or not 8000<=record['sample_rate']<=192000
                or type(duration) not in (int,float) or not math.isfinite(duration) or duration<=0):
            raise ValueError('Invalid captured MP3 metadata; no codec parameters are inferred')
        return _audition_path(path,bound[0]['sha256'],visual_path),record
    with open(ns.file_io_path(path),'rb') as stream:
        with wave.open(stream,'rb') as wav:
            if (wav.getnchannels()!=record.get('channels') or wav.getframerate()!=record.get('sample_rate')
                    or wav.getsampwidth()*8!=record.get('bits') or wav.getnframes()!=record.get('frames')
                    or wav.getcomptype()!='NONE'):
                raise ValueError('Native WAV header differs from captured PCM metadata')
    return _audition_path(path,bound[0]['sha256'],visual_path),record


def stop():
    global _handle
    if _handle is not None:
        try:_handle.stop()
        except (RuntimeError,ReferenceError):pass
    _handle=None


def draw(layout,context):
    owner=resolve_owner_map(context.active_object)
    if owner is None:return
    index=json.loads(context.scene.get('wz09_native_visual_index_json','{}'))
    if str(owner['wz09_id']) not in index:return
    box=layout.box();box.label(text='Preview source audio',icon='SOUND')
    box.prop(context.scene,'wz09_audio_preview_variant')
    box.prop(context.scene,'wz09_audio_preview_gain')
    row=box.row(align=True)
    row.operator('wz09.native_audio_play',text='Play',icon='PLAY')
    row.operator('wz09.native_audio_stop',text='Stop',icon='PAUSE')
    box.label(text='Selected alias - verified WAV / MP3',icon='INFO')
    info=json.loads(context.scene.get('wz09_audio_preview_info_json','{}'))
    if info:
        box.label(text=info['alias_ref'])
        box.label(text=f"{info['duration_s']:.2f} s · {info['sample_rate']} Hz · {info['channels']} channels")


def register():
    import bpy
    from bpy.props import IntProperty,FloatProperty
    class WZ09_OT_native_audio_play(bpy.types.Operator):
        bl_idname='wz09.native_audio_play';bl_label='Preview original sound'
        def execute(self,context):
            global _device,_handle
            try:
                from . import audio_zones
                owner=resolve_owner_map(context.active_object)
                if owner is None:raise ValueError('Select a map or sound zone')
                owner_id=str(owner['wz09_id'])
                inventory=json.loads(context.scene.get('wz09_native_semantics_index_json','{}'))[owner_id]
                source=ns.read_semantics(inventory['path'])
                if ns.file_sha256(inventory['path'])!=inventory['sha256']:raise ValueError('Native alias evidence changed')
                reference=json.loads(context.scene.get('wz09_native_visual_index_json','{}'))[owner_id]
                zone=audio_zones._selected(context)
                alias=(json.loads(zone['wz09_audio_json'])['alias_ref'] if zone is not None
                       else context.scene.wz09_audio_settings.alias_ref)
                path,record=resolve_payload(reference['path'],reference['sha256'],alias,
                    context.scene.wz09_audio_preview_variant,source['source_sha256'])
                import aud
                stop()
                if _device is None:_device=aud.Device()
                _handle=_device.play(aud.Sound(str(path)))
                _handle.volume=context.scene.wz09_audio_preview_gain
                duration=record.get('duration_s',record.get('frames',0)/record['sample_rate'])
                context.scene['wz09_audio_preview_info_json']=json.dumps({'alias_ref':alias,
                    'variant':record['variant'],'duration_s':duration,'sample_rate':record['sample_rate'],
                    'channels':record['channels'],'codec':record.get('codec','pcm')},sort_keys=True)
                self.report({'INFO'},alias+' · Variante '+str(record['variant'])+' - original WAV/MP3 preview')
                return {'FINISHED'}
            except (ValueError,OSError,RuntimeError,KeyError,TypeError,ImportError,wave.Error) as exc:
                self.report({'ERROR'},str(exc)[:220]);return {'CANCELLED'}
    class WZ09_OT_native_audio_stop(bpy.types.Operator):
        bl_idname='wz09.native_audio_stop';bl_label='Stop sound preview'
        def execute(self,context):stop();return {'FINISHED'}
    _CLASSES[:]=[WZ09_OT_native_audio_play,WZ09_OT_native_audio_stop]
    for cls in _CLASSES:bpy.utils.register_class(cls)
    bpy.types.Scene.wz09_audio_preview_variant=IntProperty(name='Alias variant',default=0,min=0,max=10000)
    bpy.types.Scene.wz09_audio_preview_gain=FloatProperty(name='Preview volume',default=.25,min=0,max=1,subtype='FACTOR')


def unregister():
    global _device
    import bpy
    stop();_device=None
    if hasattr(bpy.types.Scene,'wz09_audio_preview_variant'):del bpy.types.Scene.wz09_audio_preview_variant
    if hasattr(bpy.types.Scene,'wz09_audio_preview_gain'):del bpy.types.Scene.wz09_audio_preview_gain
    for cls in reversed(_CLASSES):bpy.utils.unregister_class(cls)
    _CLASSES.clear()
