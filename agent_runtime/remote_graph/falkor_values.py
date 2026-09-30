"""Falkor compact values retain entity types, native IDs and edge direction."""
import json
import math
import re

from .falkor_protocol import reject


def parameter_prefix(parameters):
    def encode(value, depth=0):
        if depth > 32:
            reject()
        if value is None or type(value) in (str, bool, int, float):
            if isinstance(value, float) and not math.isfinite(value):
                reject()
            if type(value) is int and not -(2**63) <= value < 2**63:
                reject()
            return json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(',', ':'))
        if isinstance(value, list):
            return '[' + ','.join(encode(item, depth+1) for item in value) + ']'
        if isinstance(value, dict):
            return '{' + ','.join('`' + str(key).replace('`','``') + '`:' + encode(item,depth+1)
                                  for key,item in value.items()) + '}'
        reject()
    parts = []
    for name,value in parameters.items():
        if not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*',name):
            reject()
        parts.append(name + '=' + encode(value))
    result = 'CYPHER ' + ' '.join(parts) + ' ' if parts else ''
    if len(result.encode('utf-8')) > 262144:
        reject()
    return result


def text(value):
    if not isinstance(value, bytes):
        reject()
    return value.decode('utf-8')


def decode_rows(reply, mappings):
    def name(group, index):
        if type(index) is not int or index < 0 or index >= len(mappings[group]):
            reject()
        return mappings[group][index]

    def properties(values):
        return {name('properties',key):decode([tag,value]) for key,tag,value in values}

    def decode(cell):
        tag,value=cell
        if tag == 1: return None
        if tag == 2: return text(value)
        if tag == 3: return int(value)
        if tag == 4:
            if value not in (b'true',b'false'): reject()
            return value == b'true'
        if tag == 5: return float(value)
        if tag == 6: return [decode(item) for item in value]
        if tag == 7:
            identity,kind,start,end,props=value
            return dict(type='relationship',element_id=str(identity),relationship_type=name('relationships',kind),
                        start_node=str(start),end_node=str(end),properties=properties(props))
        if tag == 8:
            identity,labels,props=value
            return dict(type='node',element_id=str(identity),labels=[name('labels',label) for label in labels],properties=properties(props))
        if tag == 9:
            nodes,edges=value
            return dict(type='path',nodes=decode(nodes),relationships=decode(edges))
        # Maps, points and future protocol tags need their own verified contract.
        reject()

    try:
        return [text(column[1]) for column in reply[0]], [[decode(cell) for cell in row] for row in reply[1]]
    except (ValueError, TypeError, IndexError, KeyError, UnicodeError):
        reject()


def has_entities(reply):
    def visit(cell):
        tag,value=cell
        return tag in (7,8,9) or (tag == 6 and any(visit(item) for item in value))
    return any(visit(cell) for row in reply[1] for cell in row)
