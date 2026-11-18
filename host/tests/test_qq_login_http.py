"""Drive the complete page coordinator over real HTTP with official API shapes."""
import hashlib
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from astrbot_whale_device.qq_onboarding import QQOnboarding


def test_native_login_polls_real_http_and_configures_json_string_payload(tmp_path):
    confirmed=threading.Event()
    observed=[]
    written=[]
    class API(BaseHTTPRequestHandler):
        def log_message(self,*args): pass
        def do_POST(self):
            body=json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            observed.append((self.path,body,self.headers.get('Authorization')))
            if self.path=='/api/auth/login':
                assert body['hash']==hashlib.sha256(b'local-admin-secret.napcat').hexdigest()
                data={'Credential':'test-session-credential'}
            else:
                assert self.headers.get('Authorization')=='Bearer test-session-credential'
                if self.path=='/api/QQLogin/CheckLoginStatus':
                    data={'isLogin':confirmed.is_set(),'coreReady':confirmed.is_set(),
                        'loginPhase':'ready' if confirmed.is_set() else 'waiting_qrcode',
                        'qrcodeurl':'' if confirmed.is_set() else 'https://example.test/private-qr'}
                elif self.path=='/api/QQLogin/GetQQLoginInfo':data={'uin':'12345678'}
                elif self.path=='/api/OB11Config/GetConfig':
                    data={'network':{'websocketServers':[{'name':'Other','host':'0.0.0.0','port':3001,'token':'keep-this'}]},'other':'preserve'}
                elif self.path=='/api/OB11Config/SetConfig':
                    assert isinstance(body['config'],str)
                    written.append(json.loads(body['config']));data=None
                else:raise AssertionError(self.path)
            payload=json.dumps({'code':0,'message':'success','data':data}).encode()
            self.send_response(200);self.send_header('Content-Type','application/json')
            self.send_header('Content-Length',str(len(payload)));self.end_headers();self.wfile.write(payload)
    server=ThreadingHTTPServer(('127.0.0.1',0),API)
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    class ExistingService:
        def poll_events(self):return []
        def get_webui_token(self):return 'local-admin-secret'
        def stop(self):raise AssertionError('External service should be left running')
    controller=QQOnboarding(tmp_path/'host.json',service=ExistingService(),probe=lambda url:True,poll_interval=.03)
    events=[]
    try:
        controller.start(webui_url=f'http://127.0.0.1:{server.server_port}',onebot_token='private-onebot-token')
        deadline=time.monotonic()+5
        while not any(e['type']=='qrcode' for e in events) and time.monotonic()<deadline:
            events.extend(controller.poll_events());time.sleep(.01)
        assert any(e['type']=='qrcode' for e in events)
        assert not written
        confirmed.set()
        while not any(e['type']=='configured' for e in events) and time.monotonic()<deadline:
            events.extend(controller.poll_events());time.sleep(.01)
        configured=next(e for e in events if e['type']=='configured')
        assert configured['ws_url']=='ws://127.0.0.1:3002'
        assert written[0]['network']['websocketServers'][0]['token']=='keep-this'
        managed=written[0]['network']['websocketServers'][1]
        assert managed['name']=='Whale Desktop' and managed['token']=='private-onebot-token'
        assert managed['reportSelfMessage'] is False and managed['messagePostFormat']=='array'
        assert written[0]['other']=='preserve'
    finally:
        controller.stop(stop_service=False)
        server.shutdown();server.server_close();thread.join(2)
