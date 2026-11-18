"""Install the pinned Linux bridge into a private, reversible runtime tree."""

import argparse
import hashlib
import json
import os
import secrets
import shutil
import subprocess
from pathlib import Path

from napcat_compat import enable_call_bridge

REVISION = "22f30c021cd3170f75af9dff66cc959a07ebda4b"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--project", type=Path, required=True)
    parser.add_argument("--upstream", type=Path, required=True)
    args = parser.parse_args()
    project = args.project.resolve()
    upstream = args.upstream.resolve()
    revision = subprocess.check_output(
        ["git", "-C", str(upstream), "rev-parse", "HEAD"], text=True
    ).strip()
    if revision != REVISION:
        raise ValueError("unverified_bridge_revision")
    root = Path.home() / ".local/share/whale-qq-call"
    qq = root / "qq-package/opt/QQ"
    app = qq / "resources/app"
    napcat = app / "napcat"
    bridge = root / "bridge"
    root.mkdir(parents=True, exist_ok=True)
    os.chmod(root, 0o700)
    if not (app / "avsdk/libAVSDKPlugin.so").is_file():
        raise RuntimeError("Extract the official Linux QQ DEB under qq-package first.")
    if not (napcat / "napcat.mjs").exists():
        shutil.copytree(
            project / "dist-exe/napcat/napcat",
            napcat,
            ignore=shutil.ignore_patterns(
                "config",
                "logs",
                "plugins",
                "*.bat",
                "*.exe",
                "*.dll",
                "*.ps1",
                "*.log",
                "cache",
            ),
        )
    core = napcat / "napcat.mjs"
    original = core.read_text()
    patched = enable_call_bridge(original)
    if patched != original:
        backup = napcat / "napcat.whale-original.mjs"
        if not backup.exists():
            shutil.copy2(core, backup)
        core.write_text(patched)
    for name in ("config", "plugins"):
        (napcat / name).mkdir(exist_ok=True)
    # Reuse the desktop's existing account controller with one selected QQ runtime.
    desktop = project / "dist-exe"
    for filename in ("qq-linux-webui.token", "qq-linux-onebot.token"):
        token_path = desktop / filename
        if not token_path.exists():
            token_path.write_text(secrets.token_urlsafe(40))
        os.chmod(token_path, 0o600)
    web_path = napcat / "config/webui.json"
    web_config = json.loads(web_path.read_text()) if web_path.exists() else {}
    web_config.update(
        host="127.0.0.1",
        port=6109,
        token=(desktop / "qq-linux-webui.token").read_text().strip(),
        autoLoginAccount="",
    )
    web_path.write_text(json.dumps(web_config))
    os.chmod(web_path, 0o600)
    onebot_path = napcat / "config/onebot11.json"
    onebot = (
        json.loads(onebot_path.read_text()) if onebot_path.exists() else {"network": {}}
    )
    network = onebot.setdefault("network", {})
    network["websocketClients"] = []
    network["websocketServers"] = [
        {
            "name": "Whale unified QQ",
            "enable": True,
            "host": "127.0.0.1",
            "port": 6113,
            "messagePostFormat": "array",
            "reportSelfMessage": False,
            "token": (desktop / "qq-linux-onebot.token").read_text().strip(),
            "heartInterval": 30000,
            "enableForcePushEvent": True,
            "debug": False,
        }
    ]
    onebot_path.write_text(json.dumps(onebot))
    os.chmod(onebot_path, 0o600)
    (desktop / "qq-linux-runtime.json").write_text(
        json.dumps({"enabled": True, "distro": "Ubuntu"})
    )
    package = app / "package.json"
    if not (app / "package.whale-original.json").exists():
        shutil.copy2(package, app / "package.whale-original.json")
    pkg = json.loads(package.read_text())
    pkg["main"] = "./loadNapCat.js"
    package.write_text(json.dumps(pkg, indent=2))
    loader = app / "loadNapCat.js"
    shutil.copy2(napcat / "loadNapCat.js", napcat / "loadNapCat.cjs")
    if not loader.exists():
        loader.write_text(
            "require(" + json.dumps(str(napcat / "loadNapCat.cjs")) + ");\n"
        )
    baseline = app / "loadNapCat.maibot-qq-call.backup.cjs"
    if baseline.exists() and "napcat/loadNapCat.js" in baseline.read_text():
        baseline.write_text(
            "require(" + json.dumps(str(napcat / "loadNapCat.cjs")) + ");\n"
        )
    subprocess.run(
        [
            "bash",
            str(upstream / "bridge/scripts/install.sh"),
            "--qq-dir",
            str(qq),
            "--napcat-dir",
            str(napcat),
            "--install-dir",
            str(bridge),
        ],
        check=True,
        stdout=subprocess.DEVNULL,
    )
    html = bridge / "av-host/host.html"
    native_check = html.read_text().replace(
        "ready: true,\n            pluginFound: Boolean(plugin),",
        "ready: typeof plugin.postMessage === 'function',\n            pluginFound: typeof plugin.postMessage === 'function',",
    )
    html.write_text(native_check)
    installed = napcat / "plugins/napcat-plugin-maibot-qq-voice-call/index.mjs"
    source = installed.read_text()
    # Only an explicit failed Login reply warrants re-login; native notifications
    # 20050/120043 also occur during successful startup and can create a loop.
    source = source.replace(
        "(command === 20050 || command === 120043) &&",
        "(command === 1 && Array.isArray(value) && value[0] !== 0) &&",
    )
    # Native restart notifications can arrive in bursts. Coalesce re-login attempts.
    source = source.replace(
        "scheduleAVHostLogin(pluginContext, 100);",
        "scheduleAVHostLogin(pluginContext, 10000);",
    )
    source = source.replace(
        "    loginPosted: false,", "    loginPosted: false,\n    loginResultCode: null,"
    )
    source = source.replace(
        "  state.avHost.lastOutputCommand = command;",
        "  state.avHost.lastOutputCommand = command;\n  if (command === 1) state.avHost.loginResultCode = Array.isArray(value) && typeof value[0] === 'number' ? value[0] : null;",
    )
    anchor = "  const inviteAt = state.call.inviteAt;\n  if (!inviteAt || state.avHost.autoAcceptInviteAt === inviteAt) return;"
    gate = """  // Whale policy: only the configured owner while the AI worker is alive.
  const allowed = process.env.WHALE_QQ_CALL_OWNER_UIN || "";
  const lease = process.env.WHALE_QQ_CALL_LEASE_FILE || "";
  let alive = false;
  try { alive = Date.now() - fs.statSync(lease).mtimeMs < 15000; } catch {}
  if (!allowed || !alive || state.call.callerUin !== allowed) {
    if (!state.call.callerUin || state.call.callerUin === allowed) scheduleAccept(500);
    return;
  }
"""
    if source.count(anchor) != 1:
        raise RuntimeError("bridge_owner_gate_anchor_changed")
    installed.write_text(source.replace(anchor, gate + anchor))
    config_file = (
        project / "runtime/astrbot/data/config/astrbot_plugin_whale_device_config.json"
    )
    config = json.loads(config_file.read_text(encoding="utf-8-sig"))
    owner = str(config.get("owner_user_id", ""))
    if not owner.isdigit():
        raise ValueError("owner_QQ_configuration_required")
    token_file = bridge / "runtime/control.token"
    lease_file = bridge / "runtime/ai-worker.lease"
    gateway = root / "gateway.py"
    shutil.copy2(Path(__file__).with_name("gateway.py"), gateway)
    (root / "gateway.json").write_text(
        json.dumps(
            {
                "owner_user_id": owner,
                "token_file": str(token_file),
                "lease_file": str(lease_file),
                "pulse_server": "unix:" + str(bridge / "runtime/pulse/native"),
            },
            indent=2,
        )
    )
    os.chmod(root / "gateway.json", 0o600)
    local_token = (
        project
        / "runtime/astrbot/data/plugin_data/astrbot_plugin_whale_device/voice/call-control.token"
    )
    local_token.parent.mkdir(parents=True, exist_ok=True)
    local_token.write_bytes(token_file.read_bytes())
    os.chmod(local_token, 0o600)
    # Windows paths are supplied by the calling configurator, never exposed to the model.
    manifest = {
        "bridge_revision": REVISION,
        "napcat_sha256": hashlib.sha256(
            (napcat / "napcat.mjs").read_bytes()
        ).hexdigest(),
        "qq_version": pkg["version"],
        "gateway_port": 6112,
    }
    (root / "installation.json").write_text(json.dumps(manifest, indent=2))
    # The supervisor owns only these child processes and never kills other QQ sessions.
    supervisor = root / "supervise.py"
    shutil.copy2(Path(__file__).with_name("supervise.py"), supervisor)
    print(json.dumps({"installed": True, **manifest}))


if __name__ == "__main__":
    main()
