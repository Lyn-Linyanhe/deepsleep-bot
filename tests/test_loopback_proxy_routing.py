"""Deterministic loopback proxy routing check with the real OpenAI client constructor.
A local synthetic HTTP server replaces inference. No application import or credentials.
"""
import ast
import asyncio
import ipaddress
import json
import os
from pathlib import Path
import socket
import ssl
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace as NS
from urllib.parse import urlparse
from unittest.mock import patch
from openai import AsyncOpenAI, DefaultAsyncHttpxClient

SOURCE = Path(os.environ['DEEPSLEEP_WORKSPACE']).resolve()/'MaiBot/src/llm_models/model_client/openai_client.py'

class EmbeddingFixture(BaseHTTPRequestHandler):
    def do_POST(self):
        self.rfile.read(int(self.headers.get('Content-Length','0')))
        body=json.dumps({'object':'list','model':'local-test','data':[{'object':'embedding','index':0,'embedding':[0.1,0.2]}],'usage':{'prompt_tokens':1,'total_tokens':1}}).encode()
        self.send_response(200)
        self.send_header('Content-Type','application/json')
        self.send_header('Content-Length',str(len(body)))
        self.end_headers()
        self.wfile.write(body)
    def log_message(self,*args): pass


def client_class():
    tree=ast.parse(SOURCE.read_text(encoding='utf-8-sig'))
    cls=next(n for n in tree.body if isinstance(n,ast.ClassDef) and n.name=='OpenaiClient')
    init=next(n for n in cls.body if isinstance(n,ast.FunctionDef) and n.name=='__init__')
    helpers=[n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name=='_trust_environment_proxy']
    class Base:
        def __init__(self,provider): pass
    ns={'AdapterClient':Base,'APIProvider':object,'AsyncOpenAI':AsyncOpenAI,'DefaultAsyncHttpxClient':DefaultAsyncHttpxClient,
        'get_shared_ssl_context':ssl.create_default_context,
        'build_openai_compatible_client_config':lambda p:NS(api_key='test',base_url=p.base_url,default_headers={},default_query={}),
        '_normalize_reasoning_parse_mode':lambda v:v,'_build_reasoning_key':lambda p:'',
        '_normalize_tool_argument_parse_mode':lambda v:v,'_canonicalize_authorization_header':lambda v:v,
        '_AUTHORIZATION_HEADER':'Authorization','_SDK_AUTH_PLACEHOLDER_API_KEY':'test','ipaddress':ipaddress,'urlparse':urlparse}
    isolated=ast.ClassDef(name='OpenaiClient',bases=[ast.Name(id='AdapterClient',ctx=ast.Load())],keywords=[],body=[init],decorator_list=[])
    module=ast.fix_missing_locations(ast.Module(body=[ast.ImportFrom(module='__future__',names=[ast.alias(name='annotations')],level=0),*helpers,isolated],type_ignores=[]))
    exec(compile(module,str(SOURCE),'exec'),ns)
    return ns

async def check(base_url,proxy_url):
    ns=client_class()
    cfg=NS(base_url=base_url,organization=None,project=None,timeout=5,reasoning_parse_mode='auto',tool_argument_parse_mode='auto')
    env={key:'' for key in ('NO_PROXY','no_proxy')}
    env.update({key:proxy_url for key in ('HTTP_PROXY','HTTPS_PROXY','ALL_PROXY','http_proxy','https_proxy','all_proxy')})
    with patch.dict(os.environ,env):
        client=ns['OpenaiClient'](cfg).client
        try:
            result=await client.embeddings.create(model='local-test',input='synthetic',encoding_format='float')
            assert result.data[0].embedding==[0.1,0.2]
        finally:
            await client.close()
    f=ns['_trust_environment_proxy']
    for url in ('http://localhost:8100/v1','http://127.0.0.2:8100/v1','http://[::1]:8100/v1'):
        assert f(url) is False
    for url in ('https://example.com/v1','http://192.168.1.2/v1','https://localhost.example.com/v1'):
        assert f(url) is True
    print('PASS: real client bypasses unusable proxy for loopback; external proxy policy unchanged')

if __name__=='__main__':
    server=ThreadingHTTPServer(('127.0.0.1',0),EmbeddingFixture)
    server.daemon_threads=True
    worker=threading.Thread(target=server.serve_forever,daemon=True); worker.start()
    # Bind without listening so the synthetic proxy is guaranteed to reject connections.
    with socket.socket() as dead_proxy:
        dead_proxy.bind(('127.0.0.1',0))
        try:
            asyncio.run(check(f'http://127.0.0.1:{server.server_port}/v1',f'http://127.0.0.1:{dead_proxy.getsockname()[1]}'))
        finally:
            server.shutdown(); server.server_close(); worker.join()
