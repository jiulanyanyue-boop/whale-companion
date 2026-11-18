"""Desktop OneBot preferences, kept separate from credentials and host protocol config."""
from dataclasses import dataclass, asdict
from pathlib import Path
from urllib.parse import urlsplit
import json


@dataclass(frozen=True)
class QQSettings:
    ws_url: str = 'ws://127.0.0.1:3001'
    auto_reply: bool = False
    auto_connect: bool = False
    webui_url: str = 'http://127.0.0.1:6099'
    launcher_path: str = ''
    managed_login: bool = False
    qq_path: str = ''
    auto_login: bool = False

    def __post_init__(self):
        parsed = urlsplit(self.ws_url)
        if parsed.scheme not in ('ws', 'wss') or not parsed.hostname:
            raise ValueError('请填写 ws:// 或 wss:// 开头的 OneBot WebSocket 地址')
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError('连接地址不要包含账号、Token 或查询参数，请在 Token 输入框填写凭据')
        if parsed.scheme == 'ws' and parsed.hostname not in ('localhost', '127.0.0.1', '::1'):
            raise ValueError('跨电脑连接请使用 wss:// 加密地址；同机使用 ws://127.0.0.1:3001')
        if any(type(value) is not bool for value in (self.auto_reply, self.auto_connect, self.managed_login, self.auto_login)):
            raise ValueError('QQ 开关必须为布尔值')
        enabled = self.managed_login or self.auto_login
        object.__setattr__(self, 'managed_login', enabled)
        object.__setattr__(self, 'auto_login', enabled)
        try: parsed.port
        except ValueError: raise ValueError('连接端口不合法') from None
        webui = urlsplit(self.webui_url)
        if (webui.scheme not in ('http', 'https') or webui.hostname not in ('localhost', '127.0.0.1', '::1')
                or webui.username or webui.password or webui.query or webui.fragment or webui.path not in ('', '/')):
            raise ValueError('登录服务地址请使用本机 http://127.0.0.1:6099，Token 在独立输入框填写')
        try: webui.port
        except ValueError: raise ValueError('登录服务端口不合法') from None
        if not isinstance(self.launcher_path, str) or any(c in self.launcher_path for c in '\r\n\x00'):
            raise ValueError('启动文件路径不合法')
        if not isinstance(self.qq_path, str) or any(c in self.qq_path for c in '\r\n\x00'):
            raise ValueError('QQ 安装路径不合法')

    @staticmethod
    def path(config_path):
        return Path(config_path).resolve().parent / 'qq.json'

    @classmethod
    def load(cls, config_path):
        path = cls.path(config_path)
        if not path.exists(): return cls()
        raw = json.loads(path.read_text(encoding='utf-8'))
        return cls(**raw)

    def save(self, config_path):
        path = self.path(config_path)
        temp = path.with_suffix('.json.tmp')
        temp.write_text(json.dumps(asdict(self), ensure_ascii=False, indent=2)+'\n', encoding='utf-8')
        temp.replace(path)
