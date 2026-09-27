"""Admitted FalkorDB 4.20.7 subset, exclusively via GRAPH.RO_QUERY."""
import re
from contextlib import asynccontextmanager

from agent_runtime.execution_store import ControlError
from .adapters import AdapterRows
from .contracts import QueryRequest
from .falkor_protocol import open_connection, reject, remaining
from .falkor_values import decode_rows, has_entities, parameter_prefix
from .policy import _TOKENS
from .results import RowBudget

# Deliberately excludes eager allocators (range, split, string/list builders).
# Collect is admitted under the verified server memory cap + bounded wire reader.
FUNCTIONS = {'count','sum','avg','min','max','collect','size','id','labels','type',
             'exists','coalesce','abs','ceil','floor','round','sign','tointeger','tofloat','toboolean'}
PROCEDURES = {'db.labels','db.relationshiptypes','db.propertykeys'}
SYNTAX = {'match','optional','where','with','return','in','and','or','xor','not','when','then','else'}


def check_query(query):
    tokens=[]
    quoted_identifiers=set()
    for match in _TOKENS.finditer(query):
        value=match.group()
        if value.isspace() or value.startswith(('//','/*')): continue
        if value.startswith('`'):
            quoted_identifiers.add(len(tokens))
        tokens.append('<literal>' if value.startswith(("'",'"')) else value.strip('`').replace('``','`').lower())
    if tokens and tokens[-1]==';': tokens.pop()
    if not tokens or tokens[0] not in {'match','optional','with','return','unwind','call'} or ';' in tokens:
        reject()
    brackets = 0
    for i,token in enumerate(tokens):
        # A list comprehension starts with a variable binding, even when its
        # WHERE predicate and projection pipe are both omitted. Comments have
        # already been removed; quoted identifiers remain distinct from IN.
        if (token == '[' and i not in quoted_identifiers
                and i + 2 < len(tokens)
                and (i + 1 in quoted_identifiers
                     or re.fullmatch(r'[^\W\d]\w*', tokens[i + 1]))
                and tokens[i + 2] == 'in' and i + 2 not in quoted_identifiers):
            reject()
        brackets += (token == '[') - (token == ']')
        if brackets > 0 and token in {'*', '|'}:
            reject()
        previous=tokens[i-1] if i else None
        if token in {'load','use','foreach','reduce'} and previous not in {'.',':','$'}: reject()
        if token=='call' and previous not in {'.',':','$'}:
            j=i+1
            while j<len(tokens) and tokens[j]!='(': j+=1
            if ''.join(tokens[i+1:j]) not in PROCEDURES: reject()
        if token=='(' and i and (i-1 in quoted_identifiers or tokens[i-1]=='<literal>'
                                  or re.fullmatch(r'[^\W\d]\w*',tokens[i-1])):
            j=i-1
            while j>=2 and tokens[j-1]=='.': j-=2
            name=''.join(tokens[j:i])
            procedure=j>0 and tokens[j-1]=='call' and name in PROCEDURES
            if name not in FUNCTIONS | SYNTAX and not procedure: reject()


@asynccontextmanager
async def connect(ctx):
    async with open_connection(ctx) as connection:
        modules=await connection.command('MODULE','LIST')
        versions=[dict(zip(item[::2],item[1::2])) for item in modules]
        if not any(item.get(b'name')==b'graph' and item.get(b'ver')==42007 for item in versions):
            raise ControlError('REMOTE_GRAPH_INVALID_CONFIG',422)
        limits={}
        for key,maximum in [('TIMEOUT_MAX',3000),('TIMEOUT_DEFAULT',1000),('RESULTSET_SIZE',128),('QUERY_MEM_CAPACITY',8388608)]:
            result=await connection.command('GRAPH.CONFIG','GET',key)
            value=result[1]
            if type(value) is not int or not 0<value<=maximum:
                raise ControlError('REMOTE_GRAPH_INVALID_CONFIG',422)
            limits[key]=value
        yield connection,limits


class FalkorDBAdapter:
    @staticmethod
    def error(exc):
        return ('REMOTE_GRAPH_UNREACHABLE',None) if isinstance(exc,OSError) else ('REMOTE_GRAPH_QUERY_ERROR',None)

    async def _query(self,ctx,connection,limits,query,parameters=None):
        timeout=min(limits['TIMEOUT_MAX'],max(1,int(remaining(ctx.deadline)*1000)))
        return await connection.command('GRAPH.RO_QUERY',ctx.connection.public.database,
            parameter_prefix(parameters or {})+query,'--compact','TIMEOUT',timeout,
            max_bytes=ctx.max_bytes,max_rows=limits['RESULTSET_SIZE'])

    async def _mappings(self,ctx,connection,limits):
        mappings={}
        for key,procedure in [('labels','db.labels'),('relationships','db.relationshipTypes'),('properties','db.propertyKeys')]:
            reply=await self._query(ctx,connection,limits,'CALL '+procedure+'()')
            if len(reply[1])>=limits['RESULTSET_SIZE']: reject()
            _,rows=decode_rows(reply,{})
            mappings[key]=[row[0] for row in rows]
        return mappings

    async def execute(self,ctx,request):
        check_query(request.query)
        async with connect(ctx) as (connection,limits):
            reply=await self._query(ctx,connection,limits,request.query,request.parameters)
            mappings=await self._mappings(ctx,connection,limits) if has_entities(reply) else {}
            columns,rows=decode_rows(reply,mappings)
            budget=RowBudget(ctx.max_rows,ctx.max_bytes)
            for row in rows:
                if not budget.add(row): break
            return AdapterRows(columns,budget.rows,budget.returned_rows,
                               budget.truncated or len(rows)>=limits['RESULTSET_SIZE'])

    async def test(self,ctx):
        # RO_QUERY on a missing graph fails; unlike QUERY it never creates a key.
        await self.execute(ctx,QueryRequest(query='MATCH (n) RETURN count(n)'))
        return dict(success=True,connection_usable=True,database_read_only='unknown',
                    database=ctx.connection.public.database,server_version='4.20.7')

    async def describe(self,ctx,labels=None,relationship_types=None):
        async with connect(ctx) as (connection,limits):
            mappings=await self._mappings(ctx,connection,limits)
            properties=[]
            truncated=False
            # Filtering happens server-side before the sample limit.
            nodes='MATCH (n) WHERE $labels IS NULL OR any(label IN labels(n) WHERE label IN $labels) RETURN n LIMIT 32'
            edges='MATCH ()-[r]->() WHERE $types IS NULL OR type(r) IN $types RETURN r LIMIT 32'
            for query,owner in [(nodes,'node_labels'),(edges,'relationship_type')]:
                reply=await self._query(ctx,connection,limits,query,{'labels':labels,'types':relationship_types})
                _,rows=decode_rows(reply,mappings)
                truncated |= len(rows)>=min(32,limits['RESULTSET_SIZE'])
                for row in rows:
                    entity=row[0]
                    for key,value in entity['properties'].items():
                        record={owner:entity['labels'] if owner=='node_labels' else entity['relationship_type'],
                                'name':key,'types':[{str:'String',bool:'Boolean',int:'Integer',float:'Float',list:'Array',type(None):'Null'}.get(type(value),'Unknown')]}
                        if record not in properties: properties.append(record)
            return dict(success=True,labels=[v for v in mappings['labels'] if labels is None or v in labels],
                        relationship_types=[v for v in mappings['relationships'] if relationship_types is None or v in relationship_types],
                        properties=properties,coverage=dict(sampled=True,truncated=truncated))
