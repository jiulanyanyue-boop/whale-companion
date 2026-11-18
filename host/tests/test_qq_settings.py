import json
import pytest
from astrbot_whale_device.qq_settings import QQSettings
from astrbot_whale_device.host_gui import HostWindow,FormState,AppEntry
from astrbot_whale_device.gui_secrets import StoredSecrets


@pytest.fixture(autouse=True)
def isolated_gui_secret_environment(monkeypatch):
    for name in ('WHALE_SIGNING_SECRET','WHALE_MODEL_KEY','NAPCAT_TOKEN'):
        monkeypatch.setenv(name,'')


def test_qq_settings_round_trip_and_secrets_separate(tmp_path):
    path=tmp_path/'host.json'
    QQSettings('ws://127.0.0.1:3001',True,True).save(path)
    StoredSecrets(onebot_token='test-token-secret').save(path)
    config=QQSettings.load(path)
    assert config.auto_reply and config.auto_connect
    assert 'test-token-secret' not in (tmp_path/'qq.json').read_text()
    assert StoredSecrets.load(path).onebot_token=='test-token-secret'


def test_native_login_settings_preserve_management_secret(tmp_path):
    path=tmp_path/'host.json'
    QQSettings(webui_url='http://127.0.0.1:6099',launcher_path='C:/NapCat/launcher.bat',managed_login=True).save(path)
    StoredSecrets(onebot_token='message-secret',napcat_webui_token='management-secret').save(path)
    assert QQSettings.load(path).managed_login
    assert QQSettings.load(path).launcher_path=='C:/NapCat/launcher.bat'
    assert StoredSecrets.load(path).napcat_webui_token=='management-secret'
    assert 'management-secret' not in (tmp_path/'qq.json').read_text()


def test_qq_page_logs_are_live_bounded_and_hide_credentials(tmp_path):
    w=HostWindow(str(tmp_path/'host.json'));w.root.withdraw()
    try:
        w.qq_webui_token_var.set('private-management-token')
        w.qq_token_var.set('private-message-token')
        w._log('check private-management-token and private-message-token')
        rendered=w.qq_log_text.get('1.0','end')
        assert 'private-management-token' not in rendered
        assert 'private-message-token' not in rendered
        assert '[已隐藏]' in rendered
        w._log('\n'.join(f'line {i}' for i in range(1100)))
        assert int(w.qq_log_text.index('end-1c').split('.')[0])<=1001
        w._on_qq_clear_logs()
        assert not w.qq_log_text.get('1.0','end').strip()
        assert 'line 1099' in w.log_text.get('1.0','end')
    finally:w.root.destroy()


@pytest.mark.parametrize('url',['https://example.com','ws://example.com:3001','ws://127.0.0.1:3/?token=secret','ws://user:pass@localhost:3','ws://localhost:999999'])
def test_qq_settings_rejects_invalid_or_credential_urls(url):
    with pytest.raises(ValueError):QQSettings(url)


def test_gui_saving_preserves_tls_and_other_connection_configuration(tmp_path):
    path=tmp_path/'host.json'
    data=FormState(owner_user_id='12345678',device_id='phone-test',model_base_url='https://example.test',model_id='test').to_mapping()
    data['device_server'].update(certfile='existing-cert.pem',keyfile='existing-key.pem')
    data['onebot']={'http_url':'http://127.0.0.1:3000','access_token_env':'NAPCAT_TOKEN'}
    path.write_text(json.dumps(data))
    w=HostWindow(str(path));w.root.withdraw()
    try:
        w.owner_var.set('87654321')
        assert w._on_save_quiet()
        saved=json.loads(path.read_text())
        assert saved['device_server']==data['device_server']
        assert saved['onebot']==data['onebot']
        assert saved['owner_user_id']=='87654321'
    finally:w.root.destroy()


def test_gui_qq_constructor_failure_keeps_reconnect_available(tmp_path,monkeypatch):
    from astrbot_whale_device import qq_connection
    w=HostWindow(str(tmp_path/'host.json'));w.root.withdraw()
    try:
        w.host_obj=object()
        monkeypatch.setattr(qq_connection,'QQConnection',lambda *a,**k:(_ for _ in ()).throw(RuntimeError('failure')))
        w._on_qq_connect()
        assert w.qq_connection is None
        w._poll_qq()
        assert w.qq_connect_btn.cget('state')=='normal'
        assert '无法启动' in w.qq_status_label.cget('text')
    finally:w.root.destroy()


def test_close_hides_to_tray_and_show_event_restores_window(tmp_path,monkeypatch):
    import queue
    from types import SimpleNamespace
    from astrbot_whale_device import desktop_tray
    events=queue.Queue();stopped=[]
    monkeypatch.setattr(desktop_tray,'DesktopTray',lambda:SimpleNamespace(events=events,start=lambda:None,stop=lambda:stopped.append(True)))
    w=HostWindow(str(tmp_path/'host.json'));w.root.withdraw()
    try:
        w._hide_to_tray()
        assert w.root.state()=='withdrawn'
        events.put('show');w._poll_qq();w.root.update()
        assert w.root.state()!='withdrawn'
        events.put('exit');w._poll_qq()
        assert stopped==[True] and w._closing
    finally:
        if not w._closing:w.root.destroy()


@pytest.mark.skipif(__import__('os').name != 'nt', reason='Windows work-area geometry')
def test_initial_window_stays_inside_work_area_with_display_scaling(tmp_path):
    import ctypes
    from ctypes import wintypes
    user32=ctypes.windll.user32
    window=HostWindow(str(tmp_path/'host.json'))
    try:
        window.root.update()
        area=wintypes.RECT()
        bounds=wintypes.RECT()
        assert user32.SystemParametersInfoW(48,0,ctypes.byref(area),0)
        hwnd=user32.GetAncestor(window.root.winfo_id(),2)
        assert user32.GetWindowRect(hwnd,ctypes.byref(bounds))
        assert area.left <= bounds.left < bounds.right <= area.right
        assert area.top <= bounds.top < bounds.bottom <= area.bottom
        assert window.root.winfo_width() >= min(860,(area.right-area.left)//2)
        assert window.root.winfo_height() >= min(560,(area.bottom-area.top)//2)
    finally:
        window.root.destroy()
