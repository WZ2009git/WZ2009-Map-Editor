"""Shared search/paging for authored records; Blender-free and non-mutating.

Names are display/search text. Selection and ownership continue to use exact
persistent IDs. Input order is preserved so the caller controls presentation.
"""
import unicodedata


def _text(value):
    return unicodedata.normalize('NFKC',value).casefold() if isinstance(value,str) else ''


def _dependency_text(value):
    if isinstance(value,str):
        return _text(value)
    if isinstance(value,(list,tuple)):
        return ' '.join(_dependency_text(item) for item in value)
    if isinstance(value,dict):
        return ' '.join(_dependency_text(item) for item in value.values())
    return ''


def filter_page(records,query='',owner_map_id=None,kind='ALL',page=1,page_size=12):
    """Filter all query tokens, exact owner and kind, then clamp a one-based page.

    Query covers name/kind/alias_ref/behavior_ref/dependencies, never object names
    as identity or source IDs as behavior. Empty results use page 1 of 1. Results
    retain original dictionaries and no record, list or nested value is modified.
    """
    if not isinstance(query,str) or not isinstance(kind,str):
        raise ValueError('Authoring query and kind must be text')
    if type(page_size) is not int or page_size<=0:
        raise ValueError('Authoring page_size must be a positive integer')
    if type(page) is not int:
        raise ValueError('Authoring page must be an integer')
    tokens=_text(query).split()
    selected_kind=_text(kind).strip()
    filtered=[]
    for record in records:
        if not isinstance(record,dict):
            raise ValueError('Authoring records must be dictionaries')
        if owner_map_id is not None and record.get('map_instance_id')!=owner_map_id:
            continue
        if selected_kind not in {'','all'} and _text(record.get('kind',''))!=selected_kind:
            continue
        searchable=' '.join(_text(record.get(key,'')) for key in ('name','kind','alias_ref','behavior_ref'))
        searchable+=' '+_dependency_text(record.get('dependencies',[]))
        if all(token in searchable for token in tokens):
            filtered.append(record)
    total=len(filtered)
    pages=max(1,(total+page_size-1)//page_size)
    current=min(pages,max(1,page))
    start=(current-1)*page_size
    return {'items':filtered[start:start+page_size],'total':total,'pages':pages,'page':current}
