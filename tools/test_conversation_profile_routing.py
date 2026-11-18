import importlib.util
from pathlib import Path

spec = importlib.util.spec_from_file_location(
    "configure_whale", Path(__file__).with_name("configure-astrbot.py")
)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def test_owner_route_precedes_group_fallback_and_preserves_other_platforms():
    routes = module.build_profile_routing(
        {"other::": "other-profile", "whale_qq:FriendMessage:*": "old-profile"},
        "123",
        "owner",
        "group",
    )
    assert next(iter(routes)) == "whale_qq:FriendMessage:123"
    assert routes["whale_qq:FriendMessage:123"] == "owner"
    assert routes["whale_qq:FriendMessage:*"] == "group"
    assert routes["whale_qq:GroupMessage:*"] == "group"
    assert routes["other::"] == "other-profile"
