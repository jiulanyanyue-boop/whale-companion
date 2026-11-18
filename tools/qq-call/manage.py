"""Start, inspect, and stop the isolated WSL QQ call runtime from Windows."""

import argparse
import json
import subprocess
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "action", choices=["start", "stop", "status", "login-code", "enable"]
    )
    parser.add_argument("--distro", default="Ubuntu")
    args = parser.parse_args()
    if args.action == "login-code":
        import base64

        sys.path.insert(0, str(ROOT / "host"))
        from astrbot_whale_device.qq_account import QQAccountController

        controller = QQAccountController(ROOT / "dist-exe/host.json")
        result = controller.login_qr()
        if not result.get("available"):
            raise SystemExit(
                "QQ login code not ready; use the QQ login button in AstrBot."
            )
        path = ROOT / ".verify/qq-call-login.png"
        path.parent.mkdir(exist_ok=True)
        path.write_bytes(
            base64.b64decode(result["qr_image"].split(",", 1)[1], validate=True)
        )
        print(path)
        return
    if args.action in {"start", "stop", "login-code"}:
        if args.action == "start":
            code = """from pathlib import Path
import subprocess
r=Path.home()/'.local/share/whale-qq-call'
log=(r/'supervisor.log').open('ab')
p=subprocess.Popen(['python3',str(r/'supervise.py')],stdin=subprocess.DEVNULL,stdout=log,stderr=log,start_new_session=True)
print('Call runtime starting.')
"""
        elif args.action == "stop":
            code = """from pathlib import Path
import os,signal
p=Path.home()/'.local/share/whale-qq-call/supervisor.pid'
if p.exists(): os.kill(int(p.read_text()),signal.SIGTERM)
print('Call runtime stopping.')
"""
        else:
            code = """from pathlib import Path
import base64
r=Path.home()/'.local/share/whale-qq-call/qq-package/opt/QQ/resources/app/napcat/cache'
files=sorted(r.glob('*.png'),key=lambda p:p.stat().st_mtime)
if not files: raise SystemExit('No login code yet; start the call runtime first.')
print(base64.b64encode(files[-1].read_bytes()).decode())
"""
        result = subprocess.run(
            ["wsl", "-d", args.distro, "--", "python3", "-c", code],
            capture_output=True,
            check=False,
            timeout=20,
        )
        if result.returncode:
            raise SystemExit("WSL call runtime operation failed.")
        if args.action == "login-code":
            import base64

            path = ROOT / ".verify/qq-call-login.png"
            path.parent.mkdir(exist_ok=True)
            path.write_bytes(base64.b64decode(result.stdout.strip(), validate=True))
            print(path)
        else:
            print(result.stdout.decode(errors="replace").strip())
        return
    config_file = (
        ROOT / "runtime/astrbot/data/config/astrbot_plugin_whale_device_config.json"
    )
    token_file = (
        ROOT
        / "runtime/astrbot/data/plugin_data/astrbot_plugin_whale_device/voice/call-control.token"
    )
    if args.action == "enable":
        if not token_file.is_file():
            raise SystemExit("Prepare the WSL bridge first.")
        sys.path.insert(0, str(ROOT / "host"))
        from astrbot_whale_device.gui_secrets import _tighten_permissions

        config = json.loads(config_file.read_text(encoding="utf-8-sig"))
        config.update(
            voice_calls_enabled=True,
            voice_call_token_file=str(token_file),
            voice_call_gateway="http://127.0.0.1:6112",
        )
        temp = config_file.with_suffix(".voice-call.tmp")
        temp.write_text(
            json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        _tighten_permissions(temp)
        temp.replace(config_file)
        _tighten_permissions(token_file)
        print("Owner call inference enabled for the next AstrBot start.")
        return
    token = token_file.read_text().strip()
    for port, endpoint in [(6111, "/v1/status"), (6112, "/v1/status")]:
        try:
            request = urllib.request.Request(
                f"http://127.0.0.1:{port}{endpoint}",
                headers={"Authorization": "Bearer " + token},
            )
            with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(
                request, timeout=3
            ) as response:
                data = json.load(response)
            if port == 6111:
                data = data.get("data", {})
                data = {
                    "native_ready": data.get("ready") is True
                    and "postMessage" in data.get("methods", []),
                    "error": data.get("error"),
                }
            else:
                data = {
                    key: data.get(key)
                    for key in ("ready", "phase", "error", "capabilities")
                }
            print(json.dumps({"port": port, **data}, ensure_ascii=False))
        except OSError:
            print(json.dumps({"port": port, "ready": False, "error": "not_running"}))


if __name__ == "__main__":
    main()
