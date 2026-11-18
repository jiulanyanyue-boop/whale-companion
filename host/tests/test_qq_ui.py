"""QQ setup is an explicit, truthful UI flow, with no network side effects."""
from types import SimpleNamespace
import tkinter as tk

import pytest
import customtkinter as ctk

from astrbot_whale_device.desktop_ui import build


@pytest.fixture()
def qq_window():
    ctk.set_appearance_mode('light')
    root=ctk.CTk()
    root.withdraw()
    calls=[]
    names=['_on_start','_on_stop','_on_save','_on_regen','_on_fetch_models',
           '_on_fetch_apps','_on_advanced','_on_check_env','_on_qq_connect',
           '_on_qq_disconnect','_on_qq_auto_login','_on_qq_auto_reply','_on_qq_manual_login','_on_qq_logout',
           '_on_qq_qr_refresh','_on_qq_login_stop','_on_qq_clear_logs',
           '_on_qq_pick_launcher','_on_qq_pick_qq','_on_qq_save_settings']
    window=SimpleNamespace(root=root,_log=lambda text:None,
        _on_save_quiet=lambda:True)
    for name in names:
        setattr(window,name,lambda n=name:calls.append(n))
    build(window)
    root.update_idletasks()
    try:
        yield window,calls
    finally:
        root.destroy()


def test_qq_connection_defaults_are_not_presented_as_logged_in(qq_window):
    window,_=qq_window
    window.navigate('QQ接入')
    assert window.page_title.get()=='QQ接入'
    assert window.qq_page.grid_info()
    assert window.qq_url_var.get()=='ws://127.0.0.1:3001'
    assert window.qq_status_label.cget('text')=='未连接 QQ'
    assert window.qq_account_label.cget('text')=='尚未读取助手账号'
    assert not window.qq_auto_reply_var.get()
    assert window.qq_disconnect_btn.cget('state')=='disabled'
    assert window.qq_token_input.cget('show')=='•'
    assert window.qq_webui_token_input.cget('show')=='•'
    assert window.qq_tabs.get()=='登录助手'
    assert window.qq_qr_canvas.find_withtag('placeholder')


def test_qq_controls_call_only_host_callbacks(qq_window):
    window,calls=qq_window
    window.qq_connect_btn.invoke()
    window.qq_auto_reply_switch.toggle()
    assert window.qq_auto_reply_var.get()
    window.qq_disconnect_btn.configure(state='normal')
    window.qq_disconnect_btn.invoke()
    assert calls==['_on_qq_connect','_on_qq_auto_reply','_on_qq_disconnect']


def test_qq_state_is_separate_from_phone_state_and_retained_on_navigation(qq_window):
    window,_=qq_window
    window.qq_url_var.set('ws://127.0.0.1:4567')
    window.qq_token_var.set('unit-test-token')
    window.device_badge.set('手机已连接')
    for page in ['总览','手机配对','QQ接入','模型设置','应用权限','运行日志','QQ接入']:
        window.navigate(page)
        window.root.update_idletasks()
        assert window.page_title.get()==page
    assert window.qq_status_label.cget('text')=='未连接 QQ'
    assert window.qq_url_var.get()=='ws://127.0.0.1:4567'
    assert window.qq_token_var.get()=='unit-test-token'
    assert window.qq_token_input.cget('show')=='•'


def test_login_and_logs_stay_inside_qq_page(qq_window,monkeypatch):
    import webbrowser
    window,calls=qq_window
    opened=[]
    monkeypatch.setattr(webbrowser,'open',lambda address:opened.append(address))
    window.qq_login_start_btn.invoke()
    assert calls==['_on_qq_manual_login']
    assert opened==[]
    for tab in ('运行日志','高级设置','登录助手'):
        window.qq_tabs.set(tab)
        window.root.update_idletasks()
        assert window.qq_tabs.get()==tab
    assert window.qq_log_text.cget('state')=='disabled'


def test_login_refresh_and_stop_controls_are_explicit(qq_window):
    window,calls=qq_window
    assert window.qq_qr_refresh_btn.cget('state')=='disabled'
    assert window.qq_stop_login_btn.cget('state')=='disabled'
    window.qq_qr_refresh_btn.configure(state='normal')
    window.qq_stop_login_btn.configure(state='normal')
    window.qq_qr_refresh_btn.invoke()
    window.qq_stop_login_btn.invoke()
    assert calls==['_on_qq_qr_refresh','_on_qq_login_stop']
