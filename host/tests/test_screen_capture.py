import pytest
from astrbot_whale_device.astrbot_native_bridge import GROUP_COMMANDS
from astrbot_whale_device.validation import PlanValidationError, validate_device_plan


def test_screen_capture_is_fixed_read_only_command():
    validate_device_plan([{"command": "device.screen.capture", "payload": {}}])
    assert "device.screen.capture" in GROUP_COMMANDS


@pytest.mark.parametrize(
    "payload",
    [
        {"path": "/sdcard/private"},
        {"x": 1},
        {"package_name": "untrusted"},
        {"mode": "camera"},
    ],
)
def test_screen_capture_rejects_arbitrary_parameters(payload):
    with pytest.raises(PlanValidationError):
        validate_device_plan([{"command": "device.screen.capture", "payload": payload}])
