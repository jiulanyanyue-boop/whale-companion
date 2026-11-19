"""Whale Companion 的 Slay the Spire 2 本机管理与 API 客户端。"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

try:
    from .sts2_lan import STS2LanManager
    from .sts2_relay import STS2RelayManager
except ImportError:
    from sts2_lan import STS2LanManager
    from sts2_relay import STS2RelayManager

RELEASE_API = "https://api.github.com/repos/Gennadiyev/STS2MCP/releases/latest"
RELEASE_PREFIX = "https://github.com/Gennadiyev/STS2MCP/releases/download/"
ASSET_NAMES = ("STS2_MCP.dll", "STS2_MCP.json")
MAX_RELEASE_BYTES = 2 * 1024 * 1024
MAX_RESPONSE_BYTES = 256 * 1024
MULTIPLAYER_ACTIONS = frozenset(
    {
        "menu_select",
        "play_card",
        "use_potion",
        "discard_potion",
        "end_turn",
        "undo_end_turn",
        "combat_select_card",
        "combat_confirm_selection",
        "claim_reward",
        "proceed",
        "select_card_reward",
        "skip_card_reward",
        "choose_map_node",
        "choose_event_option",
        "advance_dialogue",
        "choose_rest_option",
        "shop_purchase",
        "claim_treasure_relic",
        "select_card",
        "confirm_selection",
        "cancel_selection",
        "select_bundle",
        "confirm_bundle_selection",
        "cancel_bundle_selection",
        "select_relic",
        "skip_relic_selection",
        "crystal_sphere_set_tool",
        "crystal_sphere_click_cell",
        "crystal_sphere_proceed",
    }
)


class STS2Error(RuntimeError):
    """表示 STS2 配置、Mod 管理或本机 API 调用失败。"""


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    """拒绝本机游戏 API 的 HTTP 重定向，保持所有请求在回环地址内。"""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        """拒绝重定向，不向其他主机转发本机游戏请求。"""
        return None


class STS2Manager:
    """管理 STS2MCP Mod、本机游戏进程和受限的多人游戏 API。"""

    def __init__(self, config: dict[str, Any], data_dir: str | Path):
        """初始化配置、受管安装记录及本次进程句柄。"""
        self.config = config
        self.data_dir = Path(data_dir).resolve()
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.install_record = self.data_dir / "sts2-install.json"
        self.process_record = self.data_dir / "sts2-processes.json"
        self._lock = threading.RLock()
        self._processes: dict[int, subprocess.Popen] = {}
        self.lan = STS2LanManager(self.data_dir)
        self.relay = STS2RelayManager(self.data_dir, self.config)

    def transport(self) -> str:
        """返回本机 FastMP 或 LAN 实验模式，保持旧配置默认行为。"""
        return "lan" if self.config.get("sts2_transport") == "lan" else "local"

    def launch_args(self) -> list[str]:
        """LAN 只强制窗口化，不带会绕过 LAN 菜单的 FastMP 参数。"""
        return ["--windowed"] if self.transport() == "lan" else ["--fastmp"]

    def install_lan(self) -> dict[str, Any]:
        """确认所有同目录游戏已关闭后安装受管 LAN Mod。"""
        game_dir = self.game_dir()
        if game_dir is None:
            raise STS2Error("configure_valid_game_directory_first")
        with self._lock:
            self._assert_game_stopped(game_dir)
            return self.lan.install(game_dir)

    def uninstall_lan(self) -> dict[str, Any]:
        """确认游戏已关闭后仅移除当前目录受管的 LAN 文件。"""
        game_dir = self.game_dir()
        if game_dir is None:
            raise STS2Error("configure_valid_game_directory_first")
        with self._lock:
            self._assert_game_stopped(game_dir)
            return self.lan.uninstall(game_dir)

    def enabled(self) -> bool:
        """返回 AstrBot 中是否允许 AI 使用 STS2 工具。"""
        return self.config.get("sts2_enabled") is True

    def port(self) -> int:
        """返回 STS2MCP API 端口；只接受有效的 TCP 用户端口。"""
        value = self.config.get("sts2_api_port", 15526)
        if type(value) is not int or not 1024 <= value <= 65535:
            return 15526
        return value

    def game_dir(self) -> Path | None:
        """解析并验证用户配置的游戏安装目录。"""
        raw = self.config.get("sts2_game_dir", "")
        if not isinstance(raw, str) or not raw.strip():
            return None
        try:
            path = Path(raw).expanduser().resolve(strict=True)
        except (OSError, RuntimeError):
            return None
        if not path.is_dir() or not (path / "SlayTheSpire2.exe").is_file():
            return None
        return path

    def discover_game_dir(self) -> Path | None:
        """从 Steam 注册表与 libraryfolders.vdf 中查找游戏安装目录。"""
        candidates: list[Path] = []
        try:
            import winreg

            for hive, key_path in (
                (winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam"),
                (winreg.HKEY_LOCAL_MACHINE, r"Software\WOW6432Node\Valve\Steam"),
                (winreg.HKEY_LOCAL_MACHINE, r"Software\Valve\Steam"),
            ):
                try:
                    with winreg.OpenKey(hive, key_path) as key:
                        for value_name in ("SteamPath", "InstallPath"):
                            try:
                                root = Path(winreg.QueryValueEx(key, value_name)[0])
                                candidates.append(root)
                            except OSError:
                                continue
                except OSError:
                    continue
        except ImportError:
            pass
        for root in (Path("C:/Program Files (x86)/Steam"), Path("C:/Program Files/Steam")):
            if root.is_dir():
                candidates.append(root)
        library_roots = set(candidates)
        for root in list(candidates):
            library_file = root / "steamapps" / "libraryfolders.vdf"
            try:
                contents = library_file.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            for raw_path in re.findall(r'"path"\s*"([^"]+)"', contents):
                library_roots.add(Path(raw_path.replace("\\\\", "\\")))
        for root in library_roots:
            candidate = root / "steamapps" / "common" / "Slay the Spire 2"
            if (candidate / "SlayTheSpire2.exe").is_file():
                return candidate.resolve()
        return None

    def api_url(self) -> str:
        """构造固定回环地址，避免页面配置把服务请求发往远端。"""
        return f"http://127.0.0.1:{self.port()}"

    def _assert_game_stopped(self, game_dir: Path) -> None:
        """更新 Mod 前拒绝仍在运行的同目录游戏，保护手动启动的实例。"""
        try:
            import psutil
        except ImportError as exc:
            raise STS2Error("process_management_dependency_missing") from exc
        exe = (game_dir / "SlayTheSpire2.exe").resolve()
        for process in psutil.process_iter(["name", "exe"]):
            try:
                if str(process.info.get("name", "")).lower() != exe.name.lower():
                    continue
                actual = process.info.get("exe")
                if not actual or Path(actual).resolve() == exe:
                    raise STS2Error("close_game_before_mod_changes")
            except (OSError, psutil.NoSuchProcess):
                continue
            except psutil.AccessDenied as exc:
                raise STS2Error("cannot_verify_game_process") from exc

    def _http(self, path: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
        """向固定回环 API 发起有界的 JSON 请求。"""
        if path != "/" and (not path.startswith("/api/v1/") or "?" in path or "#" in path):
            raise STS2Error("invalid_api_path")
        payload = None if body is None else json.dumps(body).encode("utf-8")
        request = urllib.request.Request(
            self.api_url() + path,
            data=payload,
            headers={"Accept": "application/json", "Content-Type": "application/json"},
            method="GET" if body is None else "POST",
        )
        try:
            opener = urllib.request.build_opener(
                urllib.request.ProxyHandler({}), _NoRedirectHandler()
            )
            with opener.open(request, timeout=3) as response:
                data = response.read(MAX_RESPONSE_BYTES + 1)
            if len(data) > MAX_RESPONSE_BYTES:
                raise STS2Error("game_response_too_large")
            result = json.loads(data.decode("utf-8"))
            if not isinstance(result, dict):
                raise STS2Error("invalid_game_response")
            return result
        except STS2Error:
            raise
        except urllib.error.HTTPError as exc:
            if exc.code == 409:
                return {"error": "wrong_game_mode", "http_status": 409}
            raise STS2Error(f"game_api_http_{exc.code}") from exc
        except (OSError, TimeoutError, urllib.error.URLError, json.JSONDecodeError) as exc:
            raise STS2Error("game_api_unavailable") from exc

    def game_state(self) -> dict[str, Any]:
        """读取多人局；未开局时只允许读取共享菜单，不接管单人存档。"""
        result = self._http("/api/v1/multiplayer")
        if result.get("http_status") == 409:
            menu = self._http("/api/v1/singleplayer")
            if menu.get("state_type") != "menu":
                raise STS2Error("not_in_multiplayer_or_menu")
            result = menu
        if result.get("status") == "error" or result.get("error"):
            raise STS2Error(str(result.get("message") or "game_state_unavailable")[:300])
        return result

    def _api_available(self) -> bool:
        """探测 API 传输层是否已就绪，不要求游戏已经进入多人模式。"""
        try:
            result = self._http("/")
            return result.get("status") == "ok" and "STS2 MCP" in str(result.get("message", ""))
        except STS2Error:
            return False

    def game_action(self, action: str, parameters: dict[str, Any]) -> dict[str, Any]:
        """验证并执行 STS2MCP 文档允许的合作模式动作。"""
        if not isinstance(action, str) or action not in MULTIPLAYER_ACTIONS:
            raise STS2Error("unsupported_game_action")
        if not isinstance(parameters, dict) or len(parameters) > 12 or "action" in parameters:
            raise STS2Error("invalid_action_parameters")
        if any(not isinstance(key, str) or len(key) > 80 for key in parameters):
            raise STS2Error("invalid_action_parameters")
        try:
            encoded = json.dumps(parameters, ensure_ascii=False).encode("utf-8")
        except (TypeError, ValueError) as exc:
            raise STS2Error("invalid_action_parameters") from exc
        if len(encoded) > 8192:
            raise STS2Error("action_parameters_too_large")
        endpoint = "/api/v1/multiplayer"
        if action == "menu_select":
            state = self.game_state()
            if state.get("state_type") == "menu":
                if self.transport() == "lan":
                    raise STS2Error("lan_menu_requires_manual_action")
                option = parameters.get("option")
                if not isinstance(option, str):
                    raise STS2Error("select_an_advertised_menu_option")
                option = option.casefold()
                advertised = set()
                for row in state.get("options", []):
                    if isinstance(row, str):
                        advertised.add(row.casefold())
                    elif isinstance(row, dict) and row.get("enabled") is True:
                        name = row.get("name")
                        if isinstance(name, str):
                            advertised.add(name.casefold())
                if option not in advertised:
                    raise STS2Error("select_an_advertised_menu_option")
                screen = str(state.get("menu_screen", ""))
                allowed = {
                    "main": {"multiplayer"},
                    "multiplayer": {"host", "join", "load", "back"},
                    "multiplayer_host": {"standard", "daily", "custom", "back"},
                    "multiplayer_join": advertised,
                    "popup": {"cancel", "back"},
                }.get(screen, set())
                lobby = state.get("lobby")
                if (
                    screen in {"character_select", "multiplayer_load_lobby"}
                    and isinstance(lobby, dict)
                    and lobby.get("type") in {"host", "client"}
                ):
                    allowed = advertised
                if option not in allowed:
                    raise STS2Error("multiplayer_menu_action_only")
                parameters = {**parameters, "option": option}
                endpoint = "/api/v1/singleplayer"
        result = self._http(endpoint, {"action": action, **parameters})
        return result

    def _release(self) -> tuple[str, dict[str, dict[str, str]]]:
        """读取最新 release，并只接收指定仓库的 DLL/manifest 资产。"""
        request = urllib.request.Request(
            RELEASE_API,
            headers={"Accept": "application/vnd.github+json", "User-Agent": "WhaleCompanion"},
        )
        try:
            with urllib.request.urlopen(request, timeout=15) as response:
                release = json.loads(response.read(MAX_RESPONSE_BYTES + 1).decode("utf-8"))
        except (OSError, TimeoutError, urllib.error.URLError, json.JSONDecodeError) as exc:
            raise STS2Error("github_release_unavailable") from exc
        tag = release.get("tag_name") if isinstance(release, dict) else None
        rows = release.get("assets") if isinstance(release, dict) else None
        if not isinstance(tag, str) or not tag or not isinstance(rows, list):
            raise STS2Error("invalid_github_release")
        assets = {}
        for row in rows:
            if not isinstance(row, dict) or row.get("name") not in ASSET_NAMES:
                continue
            url, digest = row.get("browser_download_url"), row.get("digest")
            if (
                not isinstance(url, str)
                or not url.startswith(RELEASE_PREFIX + urllib.parse.quote(tag, safe="") + "/")
                or not isinstance(digest, str)
                or not digest.startswith("sha256:")
                or len(digest) != 71
            ):
                raise STS2Error("release_asset_integrity_unavailable")
            assets[row["name"]] = {"url": url, "sha256": digest.removeprefix("sha256:")}
        if set(assets) != set(ASSET_NAMES):
            raise STS2Error("required_release_assets_missing")
        return tag, assets

    def _download_asset(self, asset: dict[str, str]) -> bytes:
        """下载并通过 GitHub release 元数据中的 SHA-256 校验单个资产。"""
        try:
            with urllib.request.urlopen(asset["url"], timeout=30) as response:
                data = response.read(MAX_RELEASE_BYTES + 1)
        except (OSError, TimeoutError, urllib.error.URLError) as exc:
            raise STS2Error("release_asset_download_failed") from exc
        if len(data) > MAX_RELEASE_BYTES:
            raise STS2Error("release_asset_too_large")
        actual = hashlib.sha256(data).hexdigest()
        if not hmac.compare_digest(actual, asset["sha256"]):
            raise STS2Error("release_asset_checksum_mismatch")
        return data

    def _read_record(self) -> dict[str, Any]:
        """读取本插件创建的安装记录，损坏时不推断文件所有权。"""
        try:
            value = json.loads(self.install_record.read_text(encoding="utf-8"))
            return value if isinstance(value, dict) else {}
        except (OSError, json.JSONDecodeError):
            return {}

    def install(self) -> dict[str, Any]:
        """下载校验 Mod 资产并安装到选定游戏的 mods 根目录。"""
        game_dir = self.game_dir()
        if game_dir is None:
            raise STS2Error("configure_valid_game_directory_first")
        self._assert_game_stopped(game_dir)
        mods_dir = game_dir / "mods"
        mods_dir.mkdir(exist_ok=True)
        tag, assets = self._release()
        downloaded = {name: self._download_asset(assets[name]) for name in ASSET_NAMES}
        record = self._read_record()
        if record.get("game_dir") and record["game_dir"] != str(game_dir):
            raise STS2Error("uninstall_previous_game_directory_first")
        owned = record.get("files") if record.get("game_dir") == str(game_dir) else []
        owned = owned if isinstance(owned, list) else []
        targets = {name: mods_dir / name for name in ASSET_NAMES}
        config_path = mods_dir / "STS2_MCP.conf"
        if config_path.exists() and "STS2_MCP.conf" not in owned:
            raise STS2Error("unmanaged_mod_config_exists")
        for name, target in targets.items():
            if target.exists() and name not in owned:
                raise STS2Error("unmanaged_mod_file_exists")
        conf = json.dumps({"port": self.port()}, indent=2).encode("utf-8")
        downloaded["STS2_MCP.conf"] = conf
        targets["STS2_MCP.conf"] = config_path
        previous = {
            target: target.read_bytes() if target.is_file() else None
            for target in targets.values()
        }
        written: list[Path] = []
        temporaries: list[Path] = []
        record_backup = (
            self.install_record.read_bytes() if self.install_record.is_file() else None
        )
        try:
            for name, data in downloaded.items():
                target = targets[name]
                temporary = target.with_name(target.name + ".whale-tmp")
                temporaries.append(temporary)
                temporary.write_bytes(data)
                os.replace(temporary, target)
                written.append(target)
            record_temp = self.install_record.with_suffix(".tmp")
            temporaries.append(record_temp)
            record_temp.write_text(
                json.dumps(
                    {
                        "game_dir": str(game_dir),
                        "release": tag,
                        "files": list(downloaded),
                    },
                    indent=2,
                ), encoding="utf-8"
            )
            os.replace(record_temp, self.install_record)
        except OSError as exc:
            for target in reversed(written):
                old_data = previous[target]
                if old_data is None:
                    target.unlink(missing_ok=True)
                else:
                    restore = target.with_name(target.name + ".whale-restore")
                    restore.write_bytes(old_data)
                    os.replace(restore, target)
            if record_backup is None:
                self.install_record.unlink(missing_ok=True)
            else:
                restore = self.install_record.with_suffix(".restore")
                restore.write_bytes(record_backup)
                os.replace(restore, self.install_record)
            raise STS2Error("mod_install_failed") from exc
        finally:
            for temporary in temporaries:
                temporary.unlink(missing_ok=True)
        return {"installed": True, "release": tag, "game_dir": str(game_dir)}

    def uninstall(self) -> dict[str, Any]:
        """移除仅由本插件记录为受管的三个 STS2MCP 文件。"""
        record = self._read_record()
        game_dir_raw, files = record.get("game_dir"), record.get("files")
        if not isinstance(game_dir_raw, str) or not isinstance(files, list):
            raise STS2Error("no_managed_mod_installation")
        game_dir = Path(game_dir_raw).resolve()
        self._assert_game_stopped(game_dir)
        mods_dir = (game_dir / "mods").resolve()
        allowed = {*ASSET_NAMES, "STS2_MCP.conf"}
        for name in files:
            if name not in allowed:
                continue
            target = (mods_dir / name).resolve()
            if target.parent == mods_dir:
                target.unlink(missing_ok=True)
        self.install_record.unlink(missing_ok=True)
        return {"installed": False}

    def _saved_processes(self) -> list[dict[str, Any]]:
        """读取带进程创建时间的本机进程记录。"""
        try:
            value = json.loads(self.process_record.read_text(encoding="utf-8"))
            rows = value.get("processes", []) if isinstance(value, dict) else []
            return [row for row in rows if isinstance(row, dict)]
        except (OSError, json.JSONDecodeError):
            return []

    def _save_processes(self, rows: list[dict[str, Any]]) -> None:
        """原子保存本插件启动的进程身份。"""
        temp = self.process_record.with_suffix(".tmp")
        temp.write_text(json.dumps({"processes": rows}, indent=2), encoding="utf-8")
        os.replace(temp, self.process_record)

    def _remember_process(self, proc: subprocess.Popen, exe: Path, role: str, rows: list[dict[str, Any]]) -> None:
        """启动后立即持久化身份；记录失败时回收新进程，避免留下不可管理的窗口。"""
        import psutil

        try:
            process = psutil.Process(proc.pid)
            rows.append({
                "pid": proc.pid,
                "created_at": process.create_time(),
                "exe": str(exe),
                "role": role,
                "transport": self.transport(),
                "args": self.launch_args(),
            })
            self._save_processes(rows)
            self._processes[proc.pid] = proc
        except Exception as exc:
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=3)
            raise STS2Error("game_process_record_failed") from exc

    def _assert_mod_port(self, game_dir: Path) -> None:
        """阻止页面端口与 Mod 配置不一致时启动，提示先同步安装配置。"""
        conf = game_dir / "mods" / "STS2_MCP.conf"
        try:
            port = json.loads(conf.read_text(encoding="utf-8")).get("port", 15526) if conf.exists() else 15526
        except (OSError, ValueError, AttributeError) as exc:
            raise STS2Error("mod_port_not_synced") from exc
        if port != self.port():
            raise STS2Error("mod_port_not_synced")

    def _alive_managed_processes(self) -> list[dict[str, Any]]:
        """仅将路径、启动参数和创建时间均匹配的进程视为受管实例。"""
        try:
            import psutil
        except ImportError:
            return []
        alive = []
        for row in self._saved_processes():
            try:
                process = psutil.Process(int(row["pid"]))
                if (
                    process.create_time() == float(row["created_at"])
                    and Path(process.exe()).resolve() == Path(row["exe"]).resolve()
                    and row.get("role") in {"ai_host", "human_joiner"}
                    and (
                        (row.get("args") == ["--windowed"] and row.get("transport") == "lan"
                         and process.cmdline()[1:] == ["--windowed"])
                        or (row.get("transport", "local") == "local"
                            and process.cmdline()[1:] == ["--fastmp"])
                    )
                ):
                    alive.append(row)
            except (KeyError, ValueError, OSError, psutil.Error):
                continue
        return alive

    def _launch_pair(self) -> dict[str, Any]:
        """启动 AI 房主实例；等待用户在游戏内开房后再启动加入端。"""
        game_dir = self.game_dir()
        if game_dir is None:
            raise STS2Error("configure_valid_game_directory_first")
        if not (game_dir / "mods" / "STS2_MCP.dll").is_file():
            raise STS2Error("install_sts2mcp_before_launch")
        self._assert_mod_port(game_dir)
        if self.transport() == "lan":
            self.lan.assert_ready(game_dir)
        try:
            import psutil
        except ImportError as exc:
            raise STS2Error("process_management_dependency_missing") from exc
        exe = (game_dir / "SlayTheSpire2.exe").resolve()
        with self._lock:
            rows = self._alive_managed_processes()
            if any(row.get("role") == "ai_host" for row in rows):
                return {"started": True, "processes": rows, "already_running": True}
            if rows:
                raise STS2Error("stop_remaining_joiner_first")
            self._assert_game_stopped(game_dir)
            if not rows:
                if self._api_available():
                    raise STS2Error("sts2_api_port_already_in_use")
            proc = subprocess.Popen([str(exe), *self.launch_args()], cwd=str(game_dir), close_fds=True)
            self._remember_process(proc, exe, "ai_host", rows)
            return {"started": True, "processes": rows, "join_address": "127.0.0.1"}

    def join_local(self) -> dict[str, Any]:
        """房主 API 在线后按模式启动加入实例，LAN 菜单仍由用户手动操作。"""
        game_dir = self.game_dir()
        if game_dir is None:
            raise STS2Error("configure_valid_game_directory_first")
        try:
            import psutil
        except ImportError as exc:
            raise STS2Error("process_management_dependency_missing") from exc
        exe = (game_dir / "SlayTheSpire2.exe").resolve()
        with self._lock:
            rows = self._alive_managed_processes()
            if not any(row.get("role") == "ai_host" for row in rows):
                raise STS2Error("start_ai_host_and_create_lobby_first")
            if any(row.get("role") == "human_joiner" for row in rows):
                return {"started": True, "processes": rows, "already_running": True}
            if not self._api_available():
                raise STS2Error("sts2_host_api_unavailable")
            if self.transport() == "lan":
                self.lan.assert_ready(game_dir)
            proc = subprocess.Popen(
                [str(exe), *self.launch_args()], cwd=str(game_dir), close_fds=True
            )
            time.sleep(1)
            if proc.poll() is not None:
                raise STS2Error("game_instance_exited_during_start")
            self._remember_process(proc, exe, "human_joiner", rows)
            return {"started": True, "processes": rows, "join_address": "127.0.0.1"}

    def start_coop(self) -> dict[str, Any]:
        """在工作线程中按当前联机模式启动 AI 房主窗口。"""
        return self._launch_pair()

    def stop_coop(self) -> dict[str, Any]:
        """关闭本插件启动且身份仍匹配的客户端，不终止其他游戏进程。"""
        try:
            import psutil
        except ImportError as exc:
            raise STS2Error("process_management_dependency_missing") from exc
        rows = self._alive_managed_processes()
        stopped = []
        for row in reversed(rows):
            try:
                process = psutil.Process(int(row["pid"]))
            except (KeyError, ValueError, OSError, psutil.Error):
                continue
            try:
                process.terminate()
                process.wait(timeout=8)
                stopped.append(int(row["pid"]))
            except psutil.TimeoutExpired:
                process.kill()
                process.wait(timeout=3)
                stopped.append(int(row["pid"]))
            except (OSError, psutil.Error):
                continue
            self._processes.pop(int(row["pid"]), None)
        remaining = [row for row in rows if int(row["pid"]) not in stopped]
        self._save_processes(remaining)
        return {"stopped": stopped, "remaining": len(remaining)}

    def status(self) -> dict[str, Any]:
        """汇总 Mod 安装、游戏进程和 multiplayer API 的当前状态。"""
        game_dir = self.game_dir()
        record = self._read_record()
        installed = bool(
            game_dir
            and (game_dir / "mods" / "STS2_MCP.dll").is_file()
            and (game_dir / "mods" / "STS2_MCP.json").is_file()
        )
        api_connected = self._api_available()
        game_state: dict[str, Any] = {}
        try:
            if api_connected:
                game_state = self.game_state()
        except STS2Error:
            pass
        return {
            "enabled": self.enabled(),
            "transport": self.transport(),
            "share_address": str(self.config.get("sts2_share_address", "")),
            "connection": self.config.get("sts2_connection", "lan"),
            "public_port": self.config.get("sts2_public_port", 33771),
            "relay_host": self.config.get("sts2_relay_host", ""),
            "relay_control_port": self.config.get("sts2_relay_control_port", 7000),
            "relay": self.relay.status(),
            "lan": self.lan.status(game_dir),
            "addresses": self.lan.addresses(),
            "game_dir": str(game_dir) if game_dir else str(self.config.get("sts2_game_dir", "")),
            "game_dir_valid": game_dir is not None,
            "installed": installed,
            "managed_install": record.get("game_dir") == str(game_dir) if game_dir else False,
            "installed_release": record.get("release", "") if record.get("game_dir") == str(game_dir) else "",
            "api_port": self.port(),
            "api_connected": api_connected,
            "game_state": game_state if api_connected else {},
            "processes": self._alive_managed_processes(),
        }
