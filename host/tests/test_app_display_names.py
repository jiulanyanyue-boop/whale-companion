"""Human-readable app names must never be saved as package identifiers."""
import json
from types import SimpleNamespace

from astrbot_whale_device.host_gui import AppEntry, HostWindow, app_entries


from astrbot_whale_device.app_selection import AppSelection


class Picker:
    def __init__(self):
        self.state = AppSelection()
    def load(self, packages): self.state.load(packages)
    def set_apps(self, apps): self.state.replace_apps(apps)
    def packages(self): return self.state.packages()


class Text:
    def __init__(self, text=''):
        self.text = text
    def get(self, *_):
        return self.text
    def delete(self, *_):
        self.text = ''
    def insert(self, _, text):
        self.text = text


def make_window():
    win = HostWindow.__new__(HostWindow)
    win._app_rows = []
    win.app_picker = Picker()
    win.app_picker.load(['com.tencent.mm', 'com.unknown.app'])
    win.allowed_text = Text()
    win.app_count_label = SimpleNamespace(configure=lambda **kw: None)
    win._update_device_connection = lambda: None
    win._log = lambda message: None
    return win


def test_catalog_keeps_chinese_names_quotes_and_identifiers():
    evidence = {'apps': json.dumps([
        {'package_name': 'com.tencent.mm', 'label': '微信'},
        {'package_name': 'com.example.reader', 'label': '我的 "阅读器"'},
        {'package_name': 'com.empty.app', 'label': ''},
    ], ensure_ascii=False)}
    apps = app_entries(evidence)
    assert AppEntry('com.tencent.mm', '微信') in apps
    assert AppEntry('com.example.reader', '我的 "阅读器"') in apps
    assert AppEntry('com.empty.app', 'com.empty.app') in apps


def test_display_names_restore_selection_and_save_only_packages():
    win = make_window()
    win._on_apps_fetched([AppEntry('com.tencent.mm', '微信'), AppEntry('com.tencent.mobileqq', 'QQ')])
    assert win.app_picker.state.apps[0] == AppEntry('com.tencent.mm', '微信')
    assert win.app_picker.state.selected == {'com.tencent.mm', 'com.unknown.app'}
    assert win._selectedOrTypedPackages() == 'com.tencent.mm\ncom.unknown.app'
    # Deselect WeChat and select QQ: the original saved package must not sneak back in.
    win.app_picker.state.toggle('com.tencent.mm', False)
    win.app_picker.state.toggle('com.tencent.mobileqq', True)
    assert win._selectedOrTypedPackages() == 'com.tencent.mobileqq\ncom.unknown.app'


def test_duplicate_app_names_remain_independently_selectable():
    win = make_window()
    win.app_picker.load([])
    win._on_apps_fetched([AppEntry('com.one.camera', '相机'), AppEntry('com.two.camera', '相机')])
    assert len(win.app_picker.state.apps) == 2
    win.app_picker.state.toggle('com.two.camera', True)
    assert win._selectedOrTypedPackages() == 'com.two.camera'


def test_reloading_saved_config_restores_named_selection():
    win = make_window()
    win._on_apps_fetched([AppEntry('com.tencent.mm', '微信')])
    win._set_allowed_packages(['com.tencent.mm'])
    assert win.app_picker.state.selected == {'com.tencent.mm'}
    assert win.allowed_text.text == ''
    assert win._selectedOrTypedPackages() == 'com.tencent.mm'
