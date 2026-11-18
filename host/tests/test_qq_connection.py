"""Local mock OneBot WebSocket tests; never contacts real QQ accounts."""
import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
import pytest
from websockets.sync.server import serve
from astrbot_whale_device.astrbot_star import reply_text_for
from astrbot_whale_device.plugin import PluginConfig, WhaleDevicePlugin
from astrbot_whale_device.qq_connection import QQConnection, validate_ws_url
from astrbot_whale_device.storage import TaskStore


def wait_for(predicate, timeout=4):
    deadline=time.monotonic()+timeout
    while time.monotonic()<deadline:
        if predicate():return
        threading.Event().wait(.01)
    assert predicate(), 'condition timed out'


def event(ident=1, group=False, user=123):
    value=dict(post_type='message',message_type='group' if group else 'private',message_id=ident,
               user_id=user,self_id=999,message='hello')
    if group:value['group_id']=456
    return value


class NapCat:
    def __init__(self,online=True,reject=False):
        self.online=online;self.reject=reject;self.socket=None;self.connections=0;self.calls=[];self.auth=[]
        self.server=serve(self.handler,'127.0.0.1',0)
        self.port=self.server.socket.getsockname()[1]
        self.thread=threading.Thread(target=self.server.serve_forever,daemon=True);self.thread.start()
    def __enter__(self):return self
    def __exit__(self,*_):
        if self.socket:self.socket.close()
        self.server.shutdown();self.thread.join(2)
    def handler(self,socket):
        self.socket=socket;self.connections+=1;self.auth.append(socket.request.headers.get('Authorization'))
        try:
            for raw in socket:
                request=json.loads(raw);self.calls.append(request);action=request['action']
                data={'user_id':999,'nickname':'Whale test'} if action=='get_login_info' else {'online':self.online} if action=='get_status' else {'message_id':7000} if action.startswith('send_') else request['params']
                socket.send(json.dumps(dict(echo=request['echo'],status='failed' if self.reject else 'ok',retcode=100 if self.reject else 0,data=data,message='secret-token-do-not-log')))
        except Exception:pass
    def push(self,value):self.socket.send(json.dumps(value))
    def sent(self):return [c for c in self.calls if c['action'] in {'send_private_msg','send_group_msg'}]


def host_for(store=None,model=None):
    plugin=WhaleDevicePlugin(PluginConfig('123','phone-test',set(),set(),b'signing'),
        model or (lambda *_:'{"mode":"REPLY","text":"你好"}'),
        SimpleNamespace(request=lambda _:pytest.fail('No device request expected')),store=store or TaskStore())
    return SimpleNamespace(plugin=plugin,reply_text=reply_text_for)


def service(host,napcat,**kwargs):
    return QQConnection(host,f'ws://127.0.0.1:{napcat.port}',request_timeout=.5,reconnect_delays=(.03,),status_interval=.1,**kwargs)


def test_private_group_duplicate_and_self_events():
    models=[]
    def model(*args):models.append(args);return '{"mode":"REPLY","text":"你好"}'
    with NapCat() as n:
        c=service(host_for(model=model),n,access_token='test-token');c.start()
        try:
            wait_for(lambda:c.snapshot()['state']=='connected')
            assert c.snapshot()['account_id']=='999' and c.snapshot()['nickname']=='Whale test'
            assert n.auth==['Bearer test-token']
            for value in [event(),event(),event(2,True),event(3,user=999)]:n.push(value)
            wait_for(lambda:len(n.sent())==2)
            assert len(models)==2
            private,group=n.sent()
            assert private['action']=='send_private_msg' and private['params']['user_id']=='123'
            assert group['action']=='send_group_msg' and group['params']['group_id']=='456'
            assert group['params']['message']==[{'type':'text','data':{'text':'你好'}}]
        finally:c.stop()
        assert c.snapshot()['state']=='stopped'


def test_api_receiving_is_independent_of_model_worker():
    entered,release=threading.Event(),threading.Event()
    def model(*_):entered.set();assert release.wait(3);return '{"mode":"REPLY","text":"ok"}'
    with NapCat() as n:
        c=service(host_for(model=model),n);c.start()
        try:
            wait_for(lambda:c.snapshot()['state']=='connected');n.push(event());assert entered.wait(2)
            with ThreadPoolExecutor(max_workers=3) as pool:
                replies=list(pool.map(lambda i:c.call_api('echo_test',{'value':i}),range(8)))
            assert [r['data']['value'] for r in replies]==list(range(8))
            release.set();wait_for(lambda:len(n.sent())==1)
        finally:release.set();c.stop()


def test_pause_discards_events_and_cancels_inflight_reply():
    entered,release=threading.Event(),threading.Event()
    def model(*_):entered.set();assert release.wait(3);return '{"mode":"REPLY","text":"late"}'
    with NapCat() as n:
        c=service(host_for(model=model),n,auto_reply=False);c.start()
        try:
            wait_for(lambda:c.snapshot()['state']=='connected');n.push(event());c.call_api('barrier');assert not entered.is_set()
            c.set_auto_reply(True);n.push(event(2));assert entered.wait(2)
            c.set_auto_reply(False);release.set();wait_for(lambda:c._events.unfinished_tasks==0)
            assert n.sent()==[]
            c.set_auto_reply(True);c.call_api('barrier');assert n.sent()==[]
        finally:release.set();c.stop()


def test_reconnect_and_qq_online_detection():
    with NapCat(online=False) as n:
        c=service(host_for(),n);c.start()
        try:
            wait_for(lambda:c.snapshot()['state']=='offline');n.online=True
            wait_for(lambda:c.snapshot()['state']=='connected');n.socket.close()
            wait_for(lambda:n.connections>=2 and c.snapshot()['state']=='connected')
            n.online=False;n.push({'post_type':'meta_event','status':{'online':False}})
            wait_for(lambda:c.snapshot()['state']=='offline')
        finally:c.stop()


def test_persistent_dedup_survives_connection_instance(tmp_path):
    store=TaskStore(tmp_path/'state.sqlite3');models=[]
    def model(*_):models.append(1);return '{"mode":"REPLY","text":"once"}'
    with NapCat() as n:
        host=host_for(store,model);first=service(host,n);first.start()
        wait_for(lambda:first.snapshot()['state']=='connected');n.push(event());wait_for(lambda:len(n.sent())==1);first.stop()
        second=service(host,n);second.start()
        try:
            wait_for(lambda:second.snapshot()['state']=='connected');n.push(event())
            wait_for(lambda:second.snapshot()['received']==1);wait_for(lambda:second._events.unfinished_tasks==0)
            assert models==[1] and len(n.sent())==1
        finally:second.stop()


def test_errors_and_snapshots_never_expose_token():
    snapshots=[]
    with NapCat(reject=True) as n:
        c=service(host_for(),n,access_token='secret-token-do-not-log',on_change=snapshots.append);c.start()
        try:
            wait_for(lambda:any(s['last_error'] for s in snapshots))
            assert 'secret-token-do-not-log' not in json.dumps(snapshots)
            assert all(s['state']!='connected' for s in snapshots)
        finally:c.stop()


@pytest.mark.parametrize('url',['http://localhost:3001','ws://user:pass@localhost:3001','ws://localhost:3001?token=secret','ws://localhost:99999'])
def test_reject_invalid_and_credential_bearing_urls(url):
    with pytest.raises(ValueError):validate_ws_url(url)

def test_pause_during_model_prevents_delayed_device_execution():
    entered,release=threading.Event(),threading.Event()
    def model(*_):
        entered.set();assert release.wait(3)
        return '{"mode":"DEVICE_TASK","device_plan":{"commands":[{"command":"device.app.list","payload":{}}]}}'
    with NapCat() as n:
        host=host_for(model=model);c=service(host,n);c.start()
        try:
            wait_for(lambda:c.snapshot()['state']=='connected')
            n.push(event());assert entered.wait(2)
            c.set_auto_reply(False);release.set()
            wait_for(lambda:c._events.unfinished_tasks==0)
            record=json.loads(host.plugin.store.effect('message:1'))
            assert record['kind']=='failed' and record['reason']=='message_processing_stopped'
            assert n.sent()==[]
        finally:release.set();c.stop()


def test_disconnect_during_model_cancels_delayed_reply():
    entered,release=threading.Event(),threading.Event()
    def model(*_):
        entered.set();assert release.wait(3)
        return '{"mode":"REPLY","text":"late"}'
    with NapCat() as n:
        c=service(host_for(model=model),n);c.start()
        try:
            wait_for(lambda:c.snapshot()['state']=='connected')
            n.push(event());assert entered.wait(2)
            n.socket.close();wait_for(lambda:c._connection_generation>0)
            release.set();wait_for(lambda:c._events.unfinished_tasks==0)
            assert n.sent()==[]
        finally:release.set();c.stop()
