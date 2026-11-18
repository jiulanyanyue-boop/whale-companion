"""Real loopback HTTP model -> persistent host -> OneBot WebSocket reply."""
import json
import threading
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
from types import SimpleNamespace
from test_qq_connection import NapCat,event,wait_for,service
from astrbot_whale_device.host_config import HostConfig
from astrbot_whale_device.host_gui import FormState
from astrbot_whale_device.standalone import StandaloneHost
from astrbot_whale_device.storage import TaskStore


def test_actual_model_http_qq_reply_and_restart_dedup(tmp_path, monkeypatch):
    calls=[]
    class Model(BaseHTTPRequestHandler):
        def do_POST(self):
            payload=json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            calls.append(payload)
            body=json.dumps({'choices':[{'message':{'content':json.dumps({'mode':'REPLY','text':'收到你的消息了'},ensure_ascii=False)}}]},ensure_ascii=False).encode()
            self.send_response(200);self.send_header('Content-Type','application/json');self.send_header('Content-Length',str(len(body)));self.end_headers();self.wfile.write(body)
        def log_message(self,*_):pass
    import ssl, urllib.request
    from astrbot_whale_device.tls_cert import ensure_self_signed_certificate
    cert, key = tmp_path/'cert.pem', tmp_path/'key.pem'
    ensure_self_signed_certificate(certfile=cert,keyfile=key,include_local_addresses=False)
    context=ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER);context.load_cert_chain(cert,key)
    original=urllib.request.build_opener
    monkeypatch.setattr(urllib.request,'build_opener',lambda *handlers:original(*handlers,
        urllib.request.HTTPSHandler(context=ssl.create_default_context(cafile=str(cert)))))
    model=ThreadingHTTPServer(('127.0.0.1',0),Model)
    model.socket=context.wrap_socket(model.socket,server_side=True)
    thread=threading.Thread(target=model.serve_forever,daemon=True);thread.start()
    data=FormState(owner_user_id='12345678',device_id='phone-test',model_base_url=f'https://localhost:{model.server_port}',model_id='local-test').to_mapping()
    data['device_server']={'host':'127.0.0.1','port':0}
    config=HostConfig.from_mapping(data)
    db=tmp_path/'runtime.sqlite3'
    try:
        with NapCat() as napcat:
            host=StandaloneHost(config,environ={'WHALE_SIGNING_SECRET':'unit-test-key','WHALE_MODEL_KEY':'test-model-key'},store=TaskStore(db))
            first=service(host,napcat);first.start()
            try:
                wait_for(lambda:first.snapshot()['state']=='connected')
                napcat.push(event(501,user=12345678));wait_for(lambda:len(napcat.sent())==1)
                assert napcat.sent()[0]['params']['message'][0]['data']['text']=='收到你的消息了'
                assert len(calls)==1
            finally:first.stop()
            fresh=StandaloneHost(config,environ={'WHALE_SIGNING_SECRET':'unit-test-key','WHALE_MODEL_KEY':'test-model-key'},store=TaskStore(db))
            second=service(fresh,napcat);second.start()
            try:
                wait_for(lambda:second.snapshot()['state']=='connected')
                napcat.push(event(501,user=12345678))
                wait_for(lambda:second.snapshot()['received']==1)
                wait_for(lambda:second._events.unfinished_tasks==0)
                assert len(calls)==1 and len(napcat.sent())==1
                napcat.push(event(502,group=True,user=87654321));wait_for(lambda:len(napcat.sent())==2)
                assert napcat.sent()[1]['action']=='send_group_msg'
            finally:second.stop()
    finally:model.shutdown();model.server_close();thread.join(2)
