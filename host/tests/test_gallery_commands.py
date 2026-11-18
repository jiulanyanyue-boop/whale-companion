import pytest

from astrbot_whale_device.validation import PlanValidationError, validate_device_plan


@pytest.mark.parametrize(
    "payload", [{}, {"limit": 1, "offset": 0}, {"limit": 20, "offset": 1000}]
)
def test_gallery_listing_accepts_bounded_pages(payload):
    validate_device_plan([{"command": "device.gallery.list", "payload": payload}])


@pytest.mark.parametrize(
    "payload",
    [
        {"limit": 0},
        {"limit": 21},
        {"offset": -1},
        {"offset": 1001},
        {"limit": True},
        {"offset": "0"},
        {"path": "/sdcard/private"},
    ],
)
def test_gallery_listing_rejects_out_of_range_and_arbitrary_paths(payload):
    with pytest.raises(PlanValidationError):
        validate_device_plan([{"command": "device.gallery.list", "payload": payload}])


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"media_id": ""},
        {"media_id": "x" * 81},
        {"media_id": 123},
        {"media_id": "123", "path": "/sdcard/private"},
    ],
)
def test_gallery_read_requires_bounded_media_id(payload):
    with pytest.raises(PlanValidationError):
        validate_device_plan([{"command": "device.gallery.read", "payload": payload}])
