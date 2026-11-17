"""Offline memory optimization regressions. Never import the live application."""
import ast
import os
import asyncio
from collections import OrderedDict
from copy import deepcopy
from dataclasses import dataclass, field, replace
import logging
from pathlib import Path
import re
import secrets
import time
from types import SimpleNamespace as NS
from typing import Any, Dict, List, Optional, Sequence
import unittest

ROOT=Path(os.environ['DEEPSLEEP_WORKSPACE']).resolve()/'MaiBot'

def load(path,names,env=None):
    tree=ast.parse(path.read_text(encoding='utf-8-sig'))
    nodes=[n for n in tree.body if isinstance(n,(ast.FunctionDef,ast.AsyncFunctionDef,ast.ClassDef)) and n.name in names]
    ns=dict(globals()); ns['logger']=logging.getLogger('memory-test'); ns.update(env or {})
    prefix=ast.ImportFrom(module='__future__',names=[ast.alias(name='annotations')],level=0)
    exec(compile(ast.fix_missing_locations(ast.Module(body=[prefix,*nodes],type_ignores=[])),str(path),'exec'),ns)
    return ns

H=ROOT/'src/maisaka/memory/heuristic_injector.py'

def injector_env():
    ns=load(H,['HeuristicMemoryInjector','HeuristicMemoryContext','HeuristicMemoryRecallState','_looks_like_placeholder','_is_near_duplicate'],{
        'HEURISTIC_MEMORY_REFERENCE_MARKER':'【启发式记忆-内部参考】','_PLACEHOLDER_MAX_CHARS':120,'_CURRENT_SESSION_QUOTA':2,
        '_SOURCE_CHAT_SUMMARY_PREFIX':'chat_summary:','_SOURCE_PERSON_FACT_PREFIX':'person_fact:',
        'LLMServiceClient':lambda **kw:None,'time':time.time})
    return ns

def search_service():
    base=load(ROOT/'src/A_memorix/core/runtime/services/base.py',['KernelServiceBase'])['KernelServiceBase']
    request_cls=load(ROOT/'src/A_memorix/core/runtime/models.py',['KernelSearchRequest'])['KernelSearchRequest']
    ns=load(ROOT/'src/A_memorix/core/runtime/services/memory_search_service.py',['MemorySearchService'],{'KernelServiceBase':base,'KernelSearchRequest':request_cls})
    class HitService:
        def _resolve_retrieval_scope(self,chat_id,shared_ids): return (chat_id,tuple(shared_ids))
    recorded=[]; reinforced=[]; rows={}
    def paragraphs(ids): return {h:deepcopy(rows[('paragraph',h)]) for h in ids if ('paragraph',h) in rows and not rows[('paragraph',h)].get('is_deleted')}
    def relations(ids,include_inactive=True): return {h:deepcopy(rows[('relation',h)]) for h in ids if ('relation',h) in rows and (include_inactive or not rows[('relation',h)].get('is_inactive'))}
    async def reinforce(ids): reinforced.extend(ids)
    kernel=NS(metadata_store=NS(get_paragraphs_by_hashes=paragraphs,get_relations_by_hashes=relations,record_access=lambda h,t:recorded.append((t,h))),
        _runtime_facade=NS(reinforce_access=reinforce),_get_search_hit_service=lambda:HitService(),
        _is_chat_filtered=lambda **kw:False,_filter_user_visible_hits=lambda hits:[h for h in hits if not h.get('metadata',{}).get('blocked')],
        _filter_hits_by_retrieval_scope=lambda hits,scope:hits,_filter_hits_by_retrieval_type_scope=lambda hits,**kw:hits,
        _summary=lambda hits:'summary',_search_status_fields=lambda:{'retrieval_ready':True})
    service=ns['MemorySearchService'](kernel)
    return service,request_cls,rows,recorded,reinforced,kernel

class MemoryTests(unittest.TestCase):
    def test_reference_has_only_whole_items_and_hard_budget(self):
        ns=injector_env(); cls=ns['HeuristicMemoryInjector']
        texts=['record-'+str(i)+':'+str(i)*190+'-END' for i in range(5)]
        output=cls._format_reference([NS(content=t) for t in texts],max_chars=900)
        self.assertLessEqual(len(output),900)
        for line in output.splitlines():
            if re.match(r'^\d+\. ',line): self.assertIn(line.split('. ',1)[1],texts)

    def test_tiny_budget_does_not_truncate_instructions(self):
        cls=injector_env()['HeuristicMemoryInjector']
        self.assertEqual(cls._format_reference([NS(content='test')],max_chars=12),'')

    def test_deferred_access_counts_only_adopted_once(self):
        svc,Req,rows,recorded,relations,kernel=search_service()
        hits=[{'type':'paragraph','hash':'a'},{'type':'paragraph','hash':'b'},{'type':'relation','hash':'r'}]
        for h in hits: rows[(h['type'],h['hash'])]={'metadata':{}}
        async def run():
            result=await svc._finalize_with_telemetry(hits,mode='search',started=time.perf_counter(),request=Req(defer_access=True,access_session_id='s'))
            self.assertEqual(recorded,[]); self.assertEqual(relations,[])
            token=result['access_token']
            self.assertFalse((await svc.confirm_memory_access(access_token=token,session_id='other',adopted=hits))['success'])
            self.assertFalse((await svc.confirm_memory_access(access_token=token,session_id='s',adopted=[{'type':'paragraph','hash':'unknown'}]))['success'])
            result=await svc.confirm_memory_access(access_token=token,session_id='s',adopted=[hits[0],hits[0],hits[2]])
            self.assertEqual(result['accepted'],2); self.assertEqual(recorded,[('paragraph','a')]); self.assertEqual(relations,['r'])
            self.assertFalse((await svc.confirm_memory_access(access_token=token,session_id='s',adopted=hits))['success'])
        asyncio.run(run())

    def test_removed_expired_and_inactive_not_counted(self):
        svc,Req,rows,recorded,relations,kernel=search_service()
        hits=[{'type':'paragraph','hash':'deleted'},{'type':'paragraph','hash':'expired'},{'type':'paragraph','hash':'blocked'},{'type':'relation','hash':'inactive'}]
        rows[('paragraph','deleted')]={'is_deleted':True}
        rows[('paragraph','expired')]={'expires_at':time.time()-1}
        rows[('paragraph','blocked')]={'metadata':{'blocked':True}}
        rows[('relation','inactive')]={'is_inactive':True}
        async def run():
            token=svc._defer_memory_access(hits,Req(defer_access=True,access_session_id='s'))
            result=await svc.confirm_memory_access(access_token=token,session_id='s',adopted=hits)
            self.assertEqual(result['accepted'],0)
        asyncio.run(run()); self.assertEqual(recorded,[]); self.assertEqual(relations,[])

    def test_receipt_expiry_and_default_immediate_access(self):
        svc,Req,rows,recorded,relations,kernel=search_service()
        hits=[{'type':'paragraph','hash':'a'}]; rows[('paragraph','a')]={}
        async def run():
            await svc._finalize_with_telemetry(hits,mode='search',started=time.perf_counter(),request=Req())
            self.assertEqual(recorded,[('paragraph','a')])
            token=svc._defer_memory_access(hits,Req(defer_access=True,access_session_id='s'))
            _,req,candidates=kernel._pending_memory_access[token]
            kernel._pending_memory_access[token]=(0,req,candidates)
            self.assertFalse((await svc.confirm_memory_access(access_token=token,session_id='s',adopted=hits))['success'])
            self.assertEqual(len(recorded),1)
        asyncio.run(run())

    def test_invalidated_candidate_never_reaches_reference(self):
        svc,Req,rows,recorded,relations,kernel=search_service()
        ns=injector_env(); obj=ns['HeuristicMemoryInjector']()
        ns['global_config']=NS(a_memorix=NS(integration=NS(
            heuristic_memory_recall_limit=5,heuristic_memory_cross_chat_enabled=True,
            heuristic_memory_recall_max_chars=900)))
        hits=[NS(content='deleted before adoption',hash_value='deleted',hit_type='paragraph'),
              NS(content='still valid',hash_value='valid',hit_type='paragraph')]
        rows[('paragraph','deleted')]={'is_deleted':True}
        rows[('paragraph','valid')]={'metadata':{}}
        token=svc._defer_memory_access([{'type':h.hit_type,'hash':h.hash_value,'content':h.content} for h in hits],
            Req(defer_access=True,access_session_id='s'))
        async def search(*a,**kw): return NS(success=True,filtered=False,hits=hits,access_token=token)
        async def confirm(**kw):
            return await svc.confirm_memory_access(access_token=kw['access_token'],session_id=kw['session_id'],
                adopted=[{'type':h.hit_type,'hash':h.hash_value} for h in kw['hits']])
        async def sources(hits): return {}
        ns['memory_service']=NS(search=search,confirm_access=confirm)
        obj._resolve_hit_sources_by_hash=sources
        obj._is_hit_allowed=lambda *a,**kw:True
        obj._is_current_session_hit=lambda *a,**kw:True
        selected=asyncio.run(obj._search_related_memory('synthetic',NS(session=NS(session_id='s',user_id='u',group_id=''))))
        self.assertEqual([h.hash_value for h in selected],['valid'])
        self.assertNotIn('deleted before adoption',obj._format_reference(selected,max_chars=900))
        self.assertEqual(recorded,[('paragraph','valid')])

    def test_service_host_defer_contract_and_confirm_round_trip(self):
        import sys
        from unittest.mock import patch
        svc,Req,rows,recorded,relations,kernel=search_service()
        rows[('paragraph','a')]={'metadata':{}}
        requests=[]
        def method(path,name):
            tree=ast.parse(path.read_text(encoding='utf-8-sig'))
            node=next(n for n in ast.walk(tree) if isinstance(n,ast.AsyncFunctionDef) and n.name==name)
            prefix=ast.ImportFrom(module='__future__',names=[ast.alias(name='annotations')],level=0)
            ns={'__package__':'src.A_memorix'}
            exec(compile(ast.fix_missing_locations(ast.Module(body=[prefix,node],type_ignores=[])),str(path),'exec'),ns)
            return ns[name]
        dispatch=method(ROOT/'src/A_memorix/host_service.py','_invoke')
        async def search(request):
            requests.append(request)
            return await svc._finalize_with_telemetry([{'type':'paragraph','hash':'a','content':'synthetic'}],
                mode='search',started=time.perf_counter(),request=request)
        host=NS(is_enabled=lambda:True,_runtime_state='ready',_kernel=NS(search_memory=search,confirm_memory_access=svc.confirm_memory_access),
            _read_config=lambda:{'global_memory_sharing_enabled':True})
        async def invoke(name,args,**kw): return await dispatch(host,name,args)
        ns=load(ROOT/'src/services/memory_service.py',['MemoryHit','MemorySearchResult','MemoryService'],
            {'a_memorix_host_service':NS(invoke=invoke)})
        api=ns['MemoryService']()
        async def run():
            normal=await api.search('synthetic',chat_id='s')
            self.assertTrue(normal.success); self.assertEqual(recorded,[('paragraph','a')])
            self.assertFalse(requests[-1].defer_access)
            result=await api.search('synthetic',chat_id='s',defer_access=True,access_session_id='s')
            self.assertTrue(result.access_token); self.assertEqual(requests[-1].chat_id,'')
            self.assertEqual(requests[-1].access_session_id,'s'); self.assertEqual(len(recorded),1)
            confirmed=await api.confirm_access(access_token=result.access_token,session_id='s',hits=result.hits)
            self.assertEqual(confirmed['accepted_items'],[{'type':'paragraph','hash':'a'}])
            self.assertEqual(len(recorded),2)
            with self.assertRaises(ValueError): await api.search('synthetic',defer_access='false',access_session_id='s')
        with patch.dict(sys.modules,{'src.A_memorix.core.runtime.sdk_memory_kernel':NS(KernelSearchRequest=Req)}):
            asyncio.run(run())

    def test_budget_selection_and_confirmation_same_items(self):
        ns=injector_env(); cls=ns['HeuristicMemoryInjector']; obj=cls()
        config=NS(heuristic_memory_recall_limit=5,heuristic_memory_cross_chat_enabled=True,heuristic_memory_recall_max_chars=900)
        ns['global_config']=NS(a_memorix=NS(integration=config))
        hits=[NS(content='foreign-'+str(i)+':'+str(i)*170,hash_value='f'+str(i),hit_type='paragraph') for i in range(3)]
        hits += [NS(content='current-'+str(i)+':'+str(i)*170,hash_value='c'+str(i),hit_type='paragraph') for i in range(2)]
        confirmed=[]
        async def search(*a,**kw):
            self.assertTrue(kw['defer_access']); self.assertEqual(kw['access_session_id'],'s')
            return NS(success=True,filtered=False,hits=hits,access_token='test')
        async def confirm(**kw):
            confirmed.extend(kw['hits'])
            return {'success':True,'accepted_items':[{'type':h.hit_type,'hash':h.hash_value} for h in kw['hits']]}
        async def sources(hits): return {}
        ns['memory_service']=NS(search=search,confirm_access=confirm)
        obj._resolve_hit_sources_by_hash=sources
        obj._is_hit_allowed=lambda *a,**kw:True
        obj._is_current_session_hit=lambda hit,*a,**kw:hit.hash_value.startswith('c')
        context=NS(session=NS(session_id='s',user_id='u',group_id=''))
        selected=asyncio.run(obj._search_related_memory('synthetic query',context))
        reference=obj._format_reference(selected,max_chars=900)
        self.assertLessEqual(len(reference),900)
        self.assertEqual([h.hash_value for h in selected],[h.hash_value for h in confirmed])
        self.assertTrue({'c0','c1'} <= {h.hash_value for h in selected})
        for hit in selected: self.assertIn(hit.content,reference)
        self.assertEqual(len([line for line in reference.splitlines() if re.match(r'^\d+\. ',line)]),len(selected))

if __name__=='__main__': unittest.main(verbosity=2)
