import threading
import time
from types import SimpleNamespace

import pytest

from astrbot_whale_device.napcat_webui import NapCatTokenError, NapCatTwoFactorRequired
from astrbot_whale_device.qq_onboarding import QQOnboarding, OnboardingError


class Service:
    def __init__(self, installed=True):
        self.installed=installed;self.ready=False;self.started=0;self.stopped=0;self.prepared=0
    def snapshot(self): return {'installed':self.installed,'error':''}
    def prepare_install(self, **kwargs):
        self.prepared+=1;self.installed=True
        kwargs['progress']({'downloaded':1,'total':2})
        return {'ok':True}
    def start(self, **kwargs): self.started+=1;self.ready=True;return True
    def stop(self): self.stopped+=1
    def get_webui_token(self): return 'private-admin-token'
    def poll_events(self): return []


class Client:
    def __init__(self):
        self.state={'logged_in':False,'core_ready':False,'qr_url':'https://example.test/qr-private',
            'login_phase':'waiting_qrcode'}
        self.auth=[];self.configured=[];self.refreshes=0
    def authenticate(self, token, **kwargs): self.auth.append((token,kwargs))
    def login_status(self): return dict(self.state)
    def refresh_qrcode(self): self.refreshes+=1;return {'qr_url':'https://example.test/refreshed-private'}
    def ensure_websocket(self, **kwargs): self.configured.append(kwargs);return {'port':3002}


def start(controller, **kwargs):
    controller.start(webui_url='http://127.0.0.1:6099',onebot_token='private-message-token',**kwargs)


def until(controller, predicate, timeout=4):
    events=[];deadline=time.monotonic()+timeout
    while time.monotonic()<deadline:
        events.extend(controller.poll_events())
        if predicate(events): return events
        time.sleep(.01)
    raise AssertionError(events)


def test_prepare_qr_scan_configure_once_and_emit_actual_connection(tmp_path):
    service=Service(installed=False);client=Client()
    controller=QQOnboarding(tmp_path/'host.json',service=service,client_factory=lambda url:client,
        probe=lambda url:service.ready,poll_interval=.02)
    try:
        start(controller)
        initial=until(controller,lambda e:any(x['type']=='qrcode' for x in e))
        assert service.prepared==service.started==1
        assert client.auth[0][0]=='private-admin-token'
        assert not client.configured
        controller.refresh_qrcode()
        until(controller,lambda e:any(x.get('url','').endswith('refreshed-private') for x in e))
        client.state={'logged_in':False,'core_ready':False,'login_phase':'qrcode_scanned','qr_url':''}
        until(controller,lambda e:any(x.get('phase')=='qrcode_scanned' for x in e))
        client.state={'logged_in':True,'core_ready':True,'qr_url':''}
        connected=until(controller,lambda e:any(x['type']=='configured' for x in e))
        assert next(x for x in connected if x['type']=='configured')['ws_url']=='ws://127.0.0.1:3002'
        assert client.configured==[{'port':3001,'onebot_token':'private-message-token'}]
        assert all('private-admin-token' not in str(x) and 'private-message-token' not in str(x) for x in initial+connected)
        assert service.stopped==0
    finally: controller.stop(stop_service=True)
    assert service.stopped==1


@pytest.mark.parametrize('error',[NapCatTokenError('需要管理 Token'),NapCatTwoFactorRequired()])
def test_existing_service_authentication_failure_is_actionable(tmp_path,error):
    service=Service();client=Client()
    def auth(*args,**kwargs): raise error
    client.authenticate=auth
    controller=QQOnboarding(tmp_path/'host.json',service=service,client_factory=lambda url:client,probe=lambda url:True)
    start(controller,webui_token='typed-private-token',totp_code='123456')
    events=until(controller,lambda e:any(x['type']=='error' for x in e))
    assert next(x for x in events if x['type']=='error')['auth_required']
    assert service.prepared==service.started==0
    assert 'typed-private-token' not in str(events) and '123456' not in str(events)
    controller.stop()
    assert service.stopped==0


def test_cancel_during_status_read_cannot_configure_or_publish_late_qr(tmp_path):
    entered=threading.Event();release=threading.Event();service=Service();client=Client()
    def delayed():
        entered.set();release.wait(3)
        return {'logged_in':True,'core_ready':True,'qr_url':'late-private-qr'}
    client.login_status=delayed
    controller=QQOnboarding(tmp_path/'host.json',service=service,client_factory=lambda url:client,probe=lambda url:True)
    start(controller)
    assert entered.wait(2)
    controller.stop(stop_service=False);release.set()
    controller._thread.join(2)
    assert not client.configured and not controller.poll_events()
    assert service.stopped==0


def test_cancel_install_does_not_launch_service(tmp_path):
    entered=threading.Event();service=Service(installed=False)
    def prepare(**kwargs):
        entered.set();assert kwargs['cancel_event'].wait(3);return {'ok':False}
    service.prepare_install=prepare
    controller=QQOnboarding(tmp_path/'host.json',service=service,probe=lambda url:False)
    start(controller)
    assert entered.wait(2)
    controller.stop(stop_service=True);controller._thread.join(2)
    assert service.started==0 and not controller.poll_events()


def test_native_login_rejects_nonlocal_message_service(tmp_path):
    controller=QQOnboarding(tmp_path/'host.json',service=Service())
    with pytest.raises(OnboardingError):
        controller.start(webui_url='http://127.0.0.1:6099',onebot_url='wss://example.test/onebot')


def test_gui_qr_and_configured_events_update_the_same_page(tmp_path,monkeypatch):
    from astrbot_whale_device.host_gui import HostWindow
    events=[{'type':'phase','phase':'waiting_qrcode','detail':'请扫码'},
        {'type':'qrcode','url':'https://example.test/qr-private'}]
    window=HostWindow(str(tmp_path/'host.json'));window.root.withdraw()
    window.qq_onboarding=SimpleNamespace(poll_events=lambda:events[:],running=True)
    try:
        window.navigate('QQ接入');window._poll_qq_onboarding()
        assert window._qq_last_qrcode.endswith('qr-private')
        assert window.qq_qr_refresh_btn.cget('state')=='normal'
        assert len(window.qq_qr_canvas.find_all())>50
        assert 'qr-private' not in window.qq_log_text.get('1.0','end')
        connections=[]
        monkeypatch.setattr(window,'_on_qq_connect',lambda:connections.append(window.qq_url_var.get()))
        events[:]=[{'type':'configured','ws_url':'ws://127.0.0.1:3002'}]
        window._poll_qq_onboarding()
        assert connections==['ws://127.0.0.1:3002']
        assert not window._qq_last_qrcode
        assert window.qq_tabs.get()=='登录助手'
    finally: window.root.destroy()
