import importlib.util
import json
from pathlib import Path
import sys
from types import SimpleNamespace

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
spec=importlib.util.spec_from_file_location('qq_runtime_broker',Path(__file__).resolve().parents[2]/'tools/qq-runtime-broker.py')
module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
QQRuntimeBroker=module.QQRuntimeBroker

class Service:
    def __init__(self):self.starts=0;self.stops=0
    def start(self):self.starts+=1;return True
    def stop(self):self.stops+=1

class StopAfter:
    def __init__(self,iterations):self.left=iterations
    def is_set(self):return self.left<=0
    def wait(self,timeout):self.left-=1

def test_prepare_disables_napcat_independent_auto_account(tmp_path):
    path=tmp_path/'qq-data/NapCat/config/webui.json';path.parent.mkdir(parents=True)
    path.write_text(json.dumps({'autoLoginAccount':'cached-user','token':'kept-private'}))
    service=Service();broker=QQRuntimeBroker(tmp_path/'host.json',service,lambda:None)
    broker._prepare_start()
    saved=json.loads(path.read_text())
    assert saved=={'autoLoginAccount':'','token':'kept-private'}
    assert service.starts==1

def test_logout_only_stops_managed_login_and_preserves_preference(tmp_path):
    service=Service();broker=QQRuntimeBroker(tmp_path/'host.json',service,lambda:None)
    broker.state_path.write_text(json.dumps({'command_id':'offline-1','auto_login':True,'logout_requested':True}))
    broker._stop=StopAfter(2)
    def unavailable():raise OSError('offline')
    broker._client=unavailable
    broker._run()
    assert service.stops==1 and service.starts==0
    assert json.loads(broker.state_path.read_text())['auto_login'] is True
    assert json.loads(broker.runtime_path.read_text())['online'] is False

def test_auto_login_once_and_toggle_alone_does_not_trigger(tmp_path):
    service=Service();broker=QQRuntimeBroker(tmp_path/'host.json',service,lambda:None)
    broker.state_path.write_text(json.dumps({'auto_login':True}))
    seen=[]
    def request(endpoint,payload=None):
        seen.append(endpoint)
        return ['only-cached-account'] if endpoint.endswith('GetQuickLoginList') else None
    broker._client=lambda:SimpleNamespace(login_status=lambda:{'logged_in':False},_request=request)
    broker._stop=StopAfter(1);broker._run()
    assert seen==[]
    broker._want_login=True;broker._stop=StopAfter(3);broker._run()
    assert seen==['/QQLogin/GetQuickLoginList','/QQLogin/SetQuickLogin']

def test_login_command_starts_service_without_touching_other_services(tmp_path):
    service=Service();broker=QQRuntimeBroker(tmp_path/'host.json',service,lambda:None)
    broker.state_path.write_text(json.dumps({'command_id':'login-1','logout_requested':False}))
    broker._client=lambda:SimpleNamespace(login_status=lambda:{'logged_in':False},_request=lambda *a:[])
    broker._stop=StopAfter(2);broker._run()
    assert service.starts==1 and service.stops==0

def test_prepare_saves_napcat_auto_account_and_fallbacks_unique_onebot_config(tmp_path):
    config = tmp_path / "qq-data/NapCat/config"
    config.mkdir(parents=True)
    (config / "webui.json").write_text(json.dumps({"autoLoginAccount":"123456","token":"kept"}))
    (config / "onebot11_123456.json").write_text("{}")
    service = Service(); broker = QQRuntimeBroker(tmp_path / "host.json", service, lambda: None)
    broker._prepare_start()
    state = json.loads(broker.state_path.read_text())
    assert state["last_login_account"] == "123456"
    seen = []
    client = SimpleNamespace(login_status=lambda: {"logged_in":False}, _request=lambda endpoint, payload=None: seen.append((endpoint,payload)) or [])
    broker._client = lambda: client
    broker._want_login = True
    broker._stop = StopAfter(1)
    broker._run()
    assert ("/QQLogin/SetQuickLogin", {"uin":"123456"}) in seen

def test_fallback_does_not_guess_multiple_onebot_configs(tmp_path):
    config = tmp_path / "qq-data/NapCat/config"
    config.mkdir(parents=True)
    (config / "onebot11_111.json").write_text("{}")
    (config / "onebot11_222.json").write_text("{}")
    service = Service(); broker = QQRuntimeBroker(tmp_path / "host.json", service, lambda: None)
    seen=[]
    broker._client=lambda:SimpleNamespace(login_status=lambda:{"logged_in":False},_request=lambda endpoint,payload=None: seen.append(endpoint) or [])
    broker._want_login=True; broker._stop=StopAfter(1); broker._run()
    assert "/QQLogin/SetQuickLogin" not in seen

def test_explicit_login_refreshes_qr_instead_of_reusing_failed_cached_account(tmp_path):
    service=Service(); broker=QQRuntimeBroker(tmp_path/'host.json',service,lambda:None)
    broker.state_path.write_text(json.dumps({'command_id':'retry-qr','logout_requested':False}))
    seen=[]
    broker._client=lambda:SimpleNamespace(login_status=lambda:{'logged_in':False},refresh_qrcode=lambda:seen.append('refresh'),_request=lambda *args:seen.append('quick-login'))
    broker._stop=StopAfter(2);broker._run()
    assert seen==['refresh']
    assert service.starts==1
