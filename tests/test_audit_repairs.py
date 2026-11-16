"""Offline regression checks: extract source AST; no production imports or network."""
import ast
import os
import asyncio
import base64
import concurrent.futures
from contextlib import asynccontextmanager
import json
import logging
from pathlib import Path
import re
import sqlite3
import tempfile
import threading
import time
from types import SimpleNamespace as NS
from typing import Any, Dict, List, Optional
import unittest

ROOT = Path(os.environ['DEEPSLEEP_WORKSPACE']).resolve()
(ROOT / 'out').mkdir(exist_ok=True)
CORE = ROOT / 'MaiBot'
PLUGIN = CORE / 'plugins/Rabbit-Jia-Er_MaiTrace'

def load(path, names, env=None):
    tree = ast.parse(path.read_text(encoding='utf-8'))
    nodes = []
    for name in names:
        node = next(n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and n.name == name)
        if not isinstance(node, ast.ClassDef):
            node.decorator_list = [d for d in node.decorator_list if isinstance(d, ast.Name) and d.id == 'asynccontextmanager']
        nodes.append(node)
    ns = dict(globals())
    ns['logger'] = logging.getLogger('offline')
    ns.update(env or {})
    tree = ast.fix_missing_locations(ast.Module(body=[ast.ImportFrom(module='__future__', names=[ast.alias(name='annotations')], level=0), *nodes], type_ignores=[]))
    exec(compile(tree, str(path), 'exec'), ns)
    return ns

class Regression(unittest.TestCase):
    def test_caller_snapshot(self):
        ns = load(CORE/'src/maisaka/reasoning_engine.py', ['_speaker_ids_from_messages', '_current_speaker_ids', '_build_tool_execution_context'], {'ToolExecutionContext':lambda **kw:NS(**kw)})
        msg=lambda uid:NS(message_info=NS(user_info=NS(user_id=uid)),is_notify=False)
        pick=ns['_speaker_ids_from_messages']
        self.assertEqual(pick([msg('guest'),msg('owner'),msg('guest')]), ['guest','owner'])
        self.assertEqual(pick([msg('owner'),msg('')]), [])
        engine=NS(_tool_caller_ids=['guest'], _runtime=NS(message_cache=[msg('owner')],session_id='s',chat_stream=NS(is_group_session=True,group_id='g',platform='qq')))
        engine._current_speaker_ids=lambda runtime: ns['_current_speaker_ids'](engine,runtime)
        self.assertEqual(ns['_build_tool_execution_context'](engine,'').user_id,'guest')
        engine._tool_caller_ids=['owner','guest']
        self.assertEqual(ns['_build_tool_execution_context'](engine,'').user_id,'')
        engine._tool_caller_ids=[]
        self.assertEqual(ns['_build_tool_execution_context'](engine,'').user_id,'')

    def test_plugin_identity_not_model_or_anchor(self):
        ns=load(CORE/'src/plugin_runtime/component_query.py',['_build_tool_context_payload','_build_tool_invocation_payload'])
        cls=NS(_build_tool_context_payload=ns['_build_tool_context_payload'])
        ns['ComponentQueryService']=cls
        ctx=NS(stream_id='s',session_id='s',user_id='',group_id='g',platform='qq',is_group_chat=True,metadata={'anchor_message':NS(message_info=NS(user_info=NS(user_id='owner')))},reasoning='')
        entry=NS(invoke_method='plugin.invoke_tool',plugin_id='Rabbit-Jia-Er.MaiTrace',name='send_feed')
        invocation=NS(arguments={'user_id':'owner','stream_id':'other'},stream_id='s',reasoning='')
        result=ns['_build_tool_invocation_payload'](entry,invocation,ctx)
        self.assertEqual(result['user_id'],'')
        self.assertEqual(result['stream_id'],'s')

    def test_sleep_storage_without_runtime(self):
        stored=[]; registered=[]
        async def store(m): stored.append(m)
        async def runtime(s):
            registered.append(s)
            return NS(register_message=store)
        async def memory(m): pass
        # Only dependency imported inside the extracted function is replaced temporarily.
        import sys
        from unittest.mock import patch
        ns=load(CORE/'src/chat/heart_flow/heartflow_message_processor.py',['HeartFCMessageReceiver'], {'MessageUtils':NS(store_message_to_db_async=store),'heartflow_manager':NS(get_or_create_heartflow_chat=runtime,heartflow_chat_list={}),'Person':NS(register_person=lambda **kw:None),'traceback':__import__('traceback')})
        m=NS(message_info=NS(user_info=NS(user_id='u',user_nickname='u'),group_info=None,additional_config={'store_without_reply':True}),platform='qq',session_id='s',processed_plain_text='test')
        with patch.dict(sys.modules, {'src.services.memory_flow_service':NS(memory_automation_service=NS(on_incoming_message=memory))}):
            asyncio.run(ns['HeartFCMessageReceiver']().process_message(m))
        self.assertEqual(len(stored),1)
        self.assertEqual(registered,[])

    def test_silent_history_is_not_authorization(self):
        async def reconcile(owner): pass
        async def build(message): return message.message_id
        ns=load(CORE/'src/maisaka/runtime.py',['register_silent_message','register_message'],{'focus_mode_manager':NS(reconcile=reconcile)})
        async def check():
            active=[]
            async def register(message): active.append(message.message_id)
            owner=NS(_message_ingest_lock=asyncio.Lock(),_max_context_size=30,_running=True,_reasoning_engine=NS(_build_history_message=build),_chat_history=[],_register_message=register)
            await ns['register_silent_message'](owner,NS(message_id='asleep'))
            await ns['register_silent_message'](owner,NS(message_id='asleep'))
            self.assertEqual(active,[])
            await ns['register_message'](owner,NS(message_id='awake'))
            self.assertEqual(owner._chat_history,['asleep'])
            self.assertEqual(active,['awake'])
            self.assertEqual(owner._silent_messages,[])
        asyncio.run(check())

    def test_limiter_cross_loops_and_cancel(self):
        provider=NS(name='p',base_url='',max_concurrent=1)
        ns=load(CORE/'src/llm_models/utils_model.py',['_provider_slot'], {'_PROVIDER_ACTIVE':{},'_PROVIDER_SLOT_LOCK':threading.Lock(),'config_manager':NS(get_model_config=lambda:NS(api_providers=[provider]))})
        peak=[0]; entered=[0]; guard=threading.Lock()
        async def request():
            async with ns['_provider_slot'](provider):
                with guard:
                    entered[0]+=1; peak[0]=max(peak[0],entered[0])
                await asyncio.sleep(.02)
                with guard: entered[0]-=1
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            futures=[pool.submit(asyncio.run,request()) for _ in range(6)]
            for f in futures: f.result(timeout=5)
        self.assertEqual(peak[0],1)
        async def cancel_waiter():
            async with ns['_provider_slot'](provider):
                with self.assertRaises(asyncio.TimeoutError):
                    await asyncio.wait_for(request(),.01)
                self.assertEqual(ns['_PROVIDER_ACTIVE']['p'],1)
        asyncio.run(cancel_waiter())
        self.assertEqual(ns['_PROVIDER_ACTIVE']['p'],0)

    def test_limiter_hot_change_keeps_active(self):
        provider=NS(name='p',base_url='',max_concurrent=0)
        ns=load(CORE/'src/llm_models/utils_model.py',['_provider_slot'], {'_PROVIDER_ACTIVE':{},'_PROVIDER_SLOT_LOCK':threading.Lock(),'config_manager':NS(get_model_config=lambda:NS(api_providers=[provider]))})
        async def check():
            async with ns['_provider_slot'](provider):
                async with ns['_provider_slot'](provider):
                    provider.max_concurrent=1
                    self.assertEqual(ns['_PROVIDER_ACTIVE']['p'],2)
                    with self.assertRaises(asyncio.TimeoutError):
                        await asyncio.wait_for(ns['_provider_slot'](provider).__aenter__(),.01)
            self.assertEqual(ns['_PROVIDER_ACTIVE']['p'],0)
        asyncio.run(check())

    def test_emoji_envelope(self):
        ns=load(PLUGIN/'services/feed_image.py',['get_emoji_image'])
        async def get(**kw): return {'success':True,'emoji':{'base64':base64.b64encode(b'fake-image').decode()}}
        plugin=NS(ctx=NS(emoji=NS(get_by_description=get)))
        self.assertEqual(asyncio.run(ns['get_emoji_image'](plugin,'test')),b'fake-image')
        async def empty(**kw): return {'success':True,'emoji':None}
        plugin.ctx.emoji.get_by_description=empty
        self.assertIsNone(asyncio.run(ns['get_emoji_image'](plugin,'test')))

    def test_silent_blocks_post_and_browse(self):
        ns=load(PLUGIN/'services/routine.py',['_hard_rules_block'],{'is_in_silent_period':lambda *a,**kw:(True,False,False),'datetime':__import__('datetime'),'ZoneInfo':__import__('zoneinfo').ZoneInfo})
        cfg=NS(routine=NS(respect_silent_hours=True,post_blocked_activities=[],browse_blocked_activities=[]),monitor=NS(silent_hours='22:00-07:00'),schedule=NS(timezone='Asia/Shanghai'))
        owner=NS(plugin=NS(config=cfg))
        for action in ('post','browse'):
            self.assertIsNotNone(ns['_hard_rules_block'](owner,NS(activity_type=NS(value='relax')),action))

    def test_placeholder_and_distinct_facts(self):
        ns=load(CORE/'src/maisaka/memory/heuristic_injector.py',['_looks_like_placeholder','_is_near_duplicate'],{'_PLACEHOLDER_MAX_CHARS':120})
        other=load(CORE/'src/A_memorix/core/utils/summary_importer.py',['_is_placeholder_summary'],{'_PLACEHOLDER_SUMMARY_MAX_CHARS':120})
        for f in (ns['_looks_like_placeholder'],other['_is_placeholder_summary']):
            self.assertTrue(f('本轮没有新增长期记忆。'))
            self.assertFalse(f('本轮没有新增项目，但测试甲已改为每周一远程办公。'))
        self.assertFalse(ns['_is_near_duplicate']('测试甲喜欢每天早晨喝咖啡','测试乙不喜欢每天早晨喝咖啡'))
        self.assertTrue(ns['_is_near_duplicate']('测试甲 喜欢咖啡','测试甲喜欢咖啡'))

    def test_private_memory_source_boundary(self):
        sessions={'privateA':NS(is_group_session=False),'privateB':NS(is_group_session=False),'group':NS(is_group_session=True)}
        ns=load(CORE/'src/maisaka/memory/heuristic_injector.py',['_is_chat_memory_allowed'],{'global_config':NS(a_memorix=NS(integration=NS(heuristic_memory_cross_chat_enabled=True,heuristic_memory_group_to_private_enabled=True,heuristic_memory_private_to_group_enabled=True))),'chat_manager':NS(get_existing_session_by_session_id=sessions.get)})
        f=ns['_is_chat_memory_allowed']
        self.assertFalse(f(None,'privateA',NS(session_id='privateB',is_group_session=False)))
        self.assertFalse(f(None,'privateA',NS(session_id='group',is_group_session=True)))
        self.assertTrue(f(None,'privateA',NS(session_id='privateA',is_group_session=False)))

    def test_feedback_forgery_scope_and_replay(self):
        ns=load(CORE/'src/learners/expression_feedback.py',['ExpressionFeedbackService'],{'_MAPPING_TTL_DAYS':3,'_NEGATIVE_PATTERN':re.compile('闭嘴')})
        with tempfile.TemporaryDirectory(dir=ROOT/'out') as temp:
            svc=ns['ExpressionFeedbackService'](Path(temp)/'feedback.db')
            svc.record_reply('s',['target'],[1])
            calls=[]
            def delta(ids,value): calls.append(value); return [{'id':1}],[]
            svc._apply_count_delta=delta
            msg=NS(session_id='s',message_id='event1',reply_to='',processed_plain_text='[事件-群消息表情回应] 对消息(ID:target)',is_notify=False,message_info=NS(additional_config={}))
            self.assertIsNone(svc._observe_sync(msg))
            msg.is_notify=True
            msg.message_info.additional_config={'napcat_notice_type':'group_msg_emoji_like','napcat_notice_payload':{'message_id':'target','user_id':'reactor'}}
            msg.session_id='other'; self.assertIsNone(svc._observe_sync(msg))
            msg.session_id='s'; self.assertIsNotNone(svc._observe_sync(msg))
            msg.message_id='new-wrapper'; self.assertIsNone(svc._observe_sync(msg))
            self.assertEqual(calls,[1])

    def test_guardian_cooldown_resumes(self):
        ns=load(ROOT/'guardian.py',['run_guardian'],{'SERVICES':{'test':{'port':1,'launch':'none'}},'REPORT_INTERVAL':999999,'FAILS_NEEDED':2,'RELAUNCH_COOLDOWN':240,'MAX_CONSECUTIVE_RELAUNCH':3,'GIVE_UP_SECONDS':1800,'CHECK_INTERVAL':180})
        clock=[10000]; launches=[]
        def sleep(n):
            clock[0]+=n
            if clock[0]>15000: raise StopIteration
        with tempfile.TemporaryDirectory(dir=ROOT/'out') as temp:
            ns.update(PIDFILE=str(Path(temp)/'pid'),os=__import__('os'),time=NS(time=lambda:clock[0],sleep=sleep),log=lambda s:None,run_health_report=lambda:None,port_up=lambda p:False,launch=lambda cmd:launches.append(clock[0]))
            with self.assertRaises(StopIteration): ns['run_guardian']()
        self.assertGreaterEqual(len(launches),4)
        self.assertGreater(launches[3]-launches[2],1800)

    def test_typing_speed_zero_single_character(self):
        ns=load(CORE/'src/chat/utils/utils.py',['calculate_typing_time'],{'global_config':NS(response_post_process=NS(typing_speed=0))})
        self.assertEqual(ns['calculate_typing_time']('嗯'),0)

    def test_typing_speed_scales_single_character(self):
        ns=load(CORE/'src/chat/utils/utils.py',['calculate_typing_time'],{'global_config':NS(response_post_process=NS(typing_speed=2)),'_typing_jitter':lambda:1})
        self.assertAlmostEqual(ns['calculate_typing_time']('嗯'),2.4)

    def test_embedding_slots_bound_requests(self):
        class HTTPError(Exception):
            def __init__(self, **kw): self.status_code=kw['status_code']
        active=[0]; peak=[0]; lock=threading.Lock()
        def embed(texts):
            with lock:
                active[0]+=1; peak[0]=max(peak[0],active[0])
            try:
                time.sleep(.02)
                if texts==['fail']: raise RuntimeError('synthetic failure')
                return [NS(tolist=lambda:[0.1,0.2]) for _ in texts]
            finally:
                with lock: active[0]-=1
        slots=threading.BoundedSemaphore(2)
        ns=load(ROOT/'embedding_server/app.py',['embeddings'],{'HTTPException':HTTPError,'get_model':lambda:NS(embed=embed),'_inference_slots':slots,'MAX_BATCH':64,'MODEL_NAME':'test'})
        with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
            results=list(pool.map(lambda _:ns['embeddings'](NS(input='hello')),range(10)))
        self.assertEqual(peak[0],2)
        self.assertEqual(len(results),10)
        with self.assertRaises(HTTPError): ns['embeddings'](NS(input='fail'))
        self.assertTrue(slots.acquire(blocking=False)); self.assertTrue(slots.acquire(blocking=False))
        self.assertFalse(slots.acquire(blocking=False)); slots.release(); slots.release()

    def test_final_memory_accounting_survives_write_error(self):
        recorded=[]; reinforced=[]
        def record(value, kind):
            recorded.append(value)
            if value=='bad': raise sqlite3.OperationalError('synthetic busy')
        async def reinforce(values): reinforced.extend(values)
        ns=load(CORE/'src/A_memorix/core/runtime/services/memory_search_service.py',['_finalize_search_response'])
        owner=NS(metadata_store=NS(record_access=record),_runtime_facade=NS(reinforce_access=reinforce),_summary=lambda hits:'ok',_search_status_fields=lambda:{})
        hits=[{'type':'paragraph','hash':'bad'},{'type':'paragraph','hash':'good'},{'type':'paragraph','hash':'good'},{'type':'relation','hash':'r'}]
        with self.assertLogs('offline',level='WARNING'):
            result=asyncio.run(ns['_finalize_search_response'](owner,hits))
        self.assertEqual(recorded,['bad','good']); self.assertEqual(reinforced,['r']); self.assertEqual(result['hits'],hits)

    def test_dedup_selects_ready_paragraph_pool(self):
        ns=load(CORE/'src/A_memorix/core/utils/summary_importer.py',['_paragraph_vector_store'])
        owner=NS(_dual_vector_pools_enabled=lambda:True,plugin_config={'paragraph_vector_store':'paragraph'},vector_store='legacy')
        self.assertEqual(ns['_paragraph_vector_store'](owner),'paragraph')
        owner._dual_vector_pools_enabled=lambda:False
        self.assertEqual(ns['_paragraph_vector_store'](owner),'legacy')

    def test_sleep_send_gate_keeps_unanchored_service_messages(self):
        from copy import deepcopy
        import uuid
        ns=load(PLUGIN/'services/messaging/controller.py',['on_send_before'],{'deepcopy':deepcopy,'uuid':uuid})
        owner=NS(_running=True,plugin=NS(config=NS(messaging=NS(enabled=True,turn_expire_seconds=300))),_prune=lambda:None,_send_anchor=lambda message,kw:kw.get('reply_message_id',''),_turns={},_sources={},_anchors={},_bindings={},_receipts={},life=NS(cancelled_private_task=lambda *a:False),_is_sleeping=lambda:True)
        message={'platform':'qq','session_id':'s','message_info':{}}
        self.assertEqual(asyncio.run(ns['on_send_before'](owner,message=message))['action'],'continue')
        self.assertEqual(asyncio.run(ns['on_send_before'](owner,message=message,reply_message_id='old'))['action'],'abort')
        owner._sources[('s','wake')]='t'; owner._turns['t']=NS(token='t',cancelled=False,wake_candidate=True,wake_confirmed=False)
        self.assertEqual(asyncio.run(ns['on_send_before'](owner,message=message,reply_message_id='wake'))['action'],'continue')
        self.assertEqual(len(owner._receipts),1)

    def test_sleep_state_rest_is_not_sleeping(self):
        ns=load(PLUGIN/'services/messaging/controller.py',['_is_sleeping'],{'time':NS(time=lambda:100),'logger':logging.getLogger('test')})
        life=NS(peek_state=lambda:{'enabled':True,'activity_kind':'rest','sleep_phase':'awake','awake_grace_until':0})
        owner=NS(plugin=NS(config=NS(messaging=NS(rest_gate_enabled=True))),life=life)
        self.assertFalse(ns['_is_sleeping'](owner))
        life.peek_state=lambda:{'enabled':True,'activity_kind':'sleep','sleep_phase':'deep_sleep','awake_grace_until':0}
        self.assertTrue(ns['_is_sleeping'](owner))

if __name__=='__main__': unittest.main(verbosity=2)
