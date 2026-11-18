"""Filtering and refreshing a catalog must not lose user permission decisions."""
from astrbot_whale_device.app_selection import AppSelection
from astrbot_whale_device.host_gui import AppEntry


def test_search_preserves_selection_and_unselected_apps_stay_unselected():
    state=AppSelection()
    state.load(['com.wechat','com.not.in.catalog'])
    state.replace_apps([AppEntry('com.wechat','微信'),AppEntry('com.qq','QQ')])
    assert state.visible('微') == [AppEntry('com.wechat','微信')]
    state.toggle('com.qq',True)
    assert state.visible('qq','已选择') == [AppEntry('com.qq','QQ')]
    assert state.packages() == ['com.not.in.catalog','com.qq','com.wechat']
    state.toggle('com.wechat',False)
    assert state.visible('微信','已选择') == []
    state.replace_apps([AppEntry('com.qq','QQ'),AppEntry('com.wechat','微信')])
    assert 'com.wechat' not in state.selected
    assert state.dirty


def test_same_names_use_separate_package_keys():
    state=AppSelection()
    state.replace_apps([AppEntry('camera.one','相机'),AppEntry('camera.two','相机')])
    state.toggle('camera.two',True)
    assert [a.package_name for a in state.visible('相机','已选择')] == ['camera.two']
    assert [a.package_name for a in state.visible('相机','未选择')] == ['camera.one']


def test_saved_state_restores_empty_and_external_selections():
    state=AppSelection()
    state.load(['external.app'])
    assert not state.dirty
    state.selected.clear()
    assert state.dirty
    state.load([])
    assert not state.dirty
    assert state.packages() == []
