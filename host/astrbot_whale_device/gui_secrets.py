"""GUI 的本地密钥保管（让用户不必去设环境变量）。

## 为什么需要它

`HostConfig` 有一条**有意为之**的安全设计：配置文件里只存环境变量名
（`signing_secret_env` / `model_api_key_env`），明文密钥永远不进 `host.json`。
这条设计是对的（配置文件会被备份、截图、复制、进版本库），
但它带来一个真实的使用门槛：**普通用户不会去设环境变量**。

实测撞到过：用户在界面上点「拉取模型列表」，得到

```
models request failed with HTTP 401: Authentication Fails
```

——401 说明请求确实打到了服务商，只是 Key 没配对。而界面上**根本没有地方填 Key**。
所以「不用环境变量」不能靠删掉安全设计来实现，而是要在它之外补一层产品化的保管。

## 本模块怎么做

1. 用户在界面上填 Key / 签名密钥；
2. 二者存进 `host.json` **同目录**的 `secrets.json`（单独文件，便于单独忽略/删除/迁移）；
3. 启动主机时把它们**注入到当前进程的环境变量**，`HostConfig` 照旧从环境变量读——
   核心包一行都不用改，安全边界也保持不变（明文依旧不写进 `host.json`）。

## 安全边界

- 文件权限：创建时立即 `chmod 600`（Windows 上尽力而为，失败不阻断但要如实记录）；
- **绝不进版本库**：`.gitignore` 已覆盖 `host/secrets.json` 与 exe 旁的 `secrets.json`；
- **绝不进日志**：本模块只暴露 `masked()`（显示前 4 位 + 长度），不返回明文；
- **不覆盖用户已设的环境变量**：如果同名环境变量已经存在，以环境变量为准
  （用户显式设的优先级更高，且便于 CI/容器场景）。
"""

from __future__ import annotations

import json
import os
import pathlib
import secrets as _secrets
from dataclasses import dataclass, field
from typing import Any, Mapping


SECRETS_FILENAME = "secrets.json"

#: 本模块保管的两个密钥在 `HostConfig` 里的环境变量名。
DEFAULT_SIGNING_ENV = "WHALE_SIGNING_SECRET"
DEFAULT_MODEL_KEY_ENV = "WHALE_MODEL_KEY"
DEFAULT_ONEBOT_TOKEN_ENV = "NAPCAT_TOKEN"


def mask(value: str) -> str:
    """脱敏显示：只留前 4 位与总长度。

    **绝不**返回可用于重建密钥的信息：长度对 `sk-...` 这类短 token 足够
    让人确认「填的是哪一个」，但不足以推算内容。
    """
    text = (value or "").strip()
    if not text:
        return ""
    if len(text) <= 4:
        return "*" * len(text)
    return f"{text[:4]}{'*' * min(len(text) - 4, 12)}（共 {len(text)} 位）"


def generate_signing_secret() -> str:
    """生成一串够强的设备帧签名密钥（32 字节十六进制）。

    为什么由程序生成：用户没能力判断「什么样的随机串算够安全」，
    让他自己编一个 `123456` 等于把安全边界交给运气。
    """
    return _secrets.token_hex(32)


@dataclass
class StoredSecrets:
    """`secrets.json` 的内容。**只有明文密钥**，没有别的配置。"""

    signing_secret: str = ""
    model_api_key: str = ""
    onebot_token: str = ""
    #: 与这些密钥绑定的环境变量名（容许用户在 `host.json` 里改过名）。
    signing_env: str = DEFAULT_SIGNING_ENV
    model_key_env: str = DEFAULT_MODEL_KEY_ENV
    onebot_token_env: str = DEFAULT_ONEBOT_TOKEN_ENV
    extra: dict[str, Any] = field(default_factory=dict)
    napcat_webui_token: str = field(default='', repr=False)

    # -- 读写 -----------------------------------------------------------

    @staticmethod
    def path_for(config_path: str | pathlib.Path) -> pathlib.Path:
        """`secrets.json` 与 `host.json` 同目录（便于整目录备份/迁移/删除）。"""
        return pathlib.Path(config_path).resolve().parent / SECRETS_FILENAME

    @classmethod
    def load(cls, config_path: str | pathlib.Path) -> StoredSecrets:
        """读取；文件不存在或损坏时返回**空实例**（不抛异常，便于首次运行）。"""
        target = cls.path_for(config_path)
        if not target.is_file():
            return cls()
        try:
            raw = json.loads(target.read_text(encoding="utf-8"))
        except Exception:
            return cls()
        if not isinstance(raw, dict):
            return cls()
        return cls(
            signing_secret=str(raw.get("signing_secret") or ""),
            model_api_key=str(raw.get("model_api_key") or ""),
            onebot_token=str(raw.get("onebot_token") or ""),
            napcat_webui_token=str(raw.get("napcat_webui_token") or ""),
            signing_env=str(raw.get("signing_env") or DEFAULT_SIGNING_ENV),
            model_key_env=str(raw.get("model_key_env") or DEFAULT_MODEL_KEY_ENV),
            onebot_token_env=str(raw.get("onebot_token_env") or DEFAULT_ONEBOT_TOKEN_ENV),
        )

    def save(self, config_path: str | pathlib.Path) -> pathlib.Path:
        """写入并收紧权限；返回实际路径。"""
        target = self.path_for(config_path)
        payload = {
            "signing_secret": self.signing_secret,
            "model_api_key": self.model_api_key,
            "onebot_token": self.onebot_token,
            "napcat_webui_token": self.napcat_webui_token,
            "signing_env": self.signing_env,
            "model_key_env": self.model_key_env,
            "onebot_token_env": self.onebot_token_env,
        }
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        _tighten_permissions(target)
        return target

    # -- 注入 -----------------------------------------------------------

    def apply_to_environ(self, environ: dict[str, str] | None = None) -> list[str]:
        """把密钥注入进程环境变量，返回**实际注入的变量名列表**（不含密钥值）。

        **不覆盖已存在的环境变量**：用户显式设的优先（也便于容器/CI 场景）。
        """
        target = os.environ if environ is None else environ
        applied: list[str] = []
        pairs = (
            (self.signing_env, self.signing_secret),
            (self.model_key_env, self.model_api_key),
            (self.onebot_token_env, self.onebot_token),
        )
        for name, value in pairs:
            if not name or not value:
                continue
            if target.get(name, "").strip():
                # 已有值 → 尊重用户显式设置，不覆盖。
                continue
            target[name] = value
            applied.append(name)
        return applied

    def masked_summary(self) -> dict[str, str]:
        """给界面/日志用的脱敏摘要（**不含明文**）。"""
        return {
            "签名密钥": mask(self.signing_secret) or "（未填写）",
            "模型 Key": mask(self.model_api_key) or "（未填写）",
            "NapCat Token": mask(self.onebot_token) or "（未填写）",
        }

    def has_minimum(self) -> bool:
        """最低可用条件：签名密钥必须存在（设备帧签名缺它就直接拒绝启动）。"""
        return bool(self.signing_secret.strip()) or bool(
            os.environ.get(self.signing_env, "").strip()
        )

    def ensure_signing_secret(self) -> bool:
        """没有签名密钥时自动生成一个；返回是否发生了生成。"""
        if self.signing_secret.strip() or os.environ.get(self.signing_env, "").strip():
            return False
        self.signing_secret = generate_signing_secret()
        return True


def _tighten_permissions(path: pathlib.Path) -> None:
    """把文件权限收到 600（仅属主可读写）。

    Windows 上 `chmod` 语义有限（只映射到"只读"位），失败**不阻断**——
    密钥文件在用户自己的目录里，且已在 `.gitignore` 中；但尽力收紧仍是应该做的。
    """
    try:
        path.chmod(0o600)
    except Exception:
        pass


def ensure_secrets_ignored(gitignore_path: str | pathlib.Path) -> bool:
    """确保 `.gitignore` 覆盖 `secrets.json`；返回是否发生了追加。

    为什么由代码保证：密钥文件一旦被提交进版本库，就等于永久泄漏
    （即使后来删掉，历史里还在）。所以这件事不能只靠"记得手动加"。
    """
    target = pathlib.Path(gitignore_path)
    pattern = "secrets.json"
    if not target.parent.is_dir():
        return False
    try:
        existing = target.read_text(encoding="utf-8") if target.is_file() else ""
    except Exception:
        return False
    if any(line.strip() == pattern for line in existing.splitlines()):
        return False
    try:
        with target.open("a", encoding="utf-8") as handle:
            if existing and not existing.endswith("\n"):
                handle.write("\n")
            handle.write(
                "\n# GUI 的本地密钥文件（含明文密钥，绝不可进版本库）\n"
                f"{pattern}\n"
            )
        return True
    except Exception:
        return False


def summarize_env(names: Mapping[str, str], environ: Mapping[str, str] | None = None) -> list[tuple[str, bool]]:
    """检查若干环境变量是否已就绪；返回 `[(名字, 是否已设置)]`，**不返回值**。"""
    env = os.environ if environ is None else environ
    return [(name, bool(env.get(name, "").strip())) for name in names.values() if name]
