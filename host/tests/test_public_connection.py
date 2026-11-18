import base64
import io

import pytest
from PIL import Image

from astrbot_whale_device.host_config import DeviceServerEndpoint
from astrbot_whale_device.models import ConfigError
from astrbot_whale_device.visual_sampling import VisualSampler


def frame(color):
    output = io.BytesIO()
    Image.new("RGB", (1080, 1920), color).save(output, format="PNG")
    return base64.b64encode(output.getvalue()).decode()


@pytest.mark.parametrize("url", [
    "ws://public.example:443/device", "wss://name/device",
    "wss://user:pass@name:443/device", "wss://name:65535/device",
    "wss://name:443/device?token=secret", "wss://name:443/../device",
])
def test_public_url_rejects_ambiguous_or_insecure_endpoints(url):
    with pytest.raises(ConfigError):
        DeviceServerEndpoint(public_url=url)


def test_public_https_proxy_path_is_preserved():
    url = "wss://host.example.com:443/whale/device"
    assert DeviceServerEndpoint(public_url=url).public_url == url


def test_static_frames_are_deduplicated_and_changes_remain_ordered():
    sampler = VisualSampler(max_frames=2)
    assert sampler.add(frame("black"), 0)
    assert not sampler.add(frame("black"), 1)
    assert sampler.add(frame("white"), 2)
    assert not sampler.add(frame("red"), 3)
    assert [stamp for stamp, _ in sampler.frames] == [0, 2]
    with Image.open(io.BytesIO(base64.b64decode(sampler.result()))) as result:
        assert result.size == (960, 344)
