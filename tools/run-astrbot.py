"""Start AstrBot with an optional local CPU embedding service."""
from __future__ import annotations
import importlib.util, json, os, re, subprocess, sys, time
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
ASTR = ROOT/'runtime/astrbot'

def credentials_from_file(path: Path) -> dict[str, str]:
    if not path.is_file(): return {}
    values = {}
    for line in path.read_text(encoding='utf-8-sig').splitlines():
        parts = re.split(r'[:：=]\s*', line.strip(), maxsplit=1)
        if len(parts) != 2: continue
        label = parts[0].lower().replace(' ', '').replace('_', '')
        if 'secret' in label: values['VOLCENGINE_SECRET_KEY'] = parts[1].strip()
        elif 'access' in label: values['VOLCENGINE_ACCESS_KEY'] = parts[1].strip()
    return values

def main():
    if os.name == 'nt':
        import ctypes
        mutex = ctypes.windll.kernel32.CreateMutexW(None, False, 'Local\\WhaleAstrBotRuntime')
        if ctypes.windll.kernel32.GetLastError() == 183:
            return 0
    host = json.loads((ROOT/'dist-exe/host.json').read_text(encoding='utf-8-sig'))
    stored = json.loads((ROOT/'dist-exe/secrets.json').read_text(encoding='utf-8-sig'))
    rpc = json.loads((ROOT/'dist-exe/device-rpc-secret.json').read_text(encoding='utf-8'))
    env = os.environ.copy()
    env.update(ASTRBOT_ROOT=str(ASTR), WHALE_OWNER_ID=str(host['owner_user_id']),
      WHALE_MODEL_KEY=stored['model_api_key'], WHALE_DEVICE_RPC_TOKEN=rpc['token'],
      WHALE_DESKTOP_CONFIG=str(ROOT/'dist-exe/host.json'), WHALE_DEPLOYMENT_MODE='desktop',
      WHALE_MEMORY_POLICY='enabled',
      WHALE_PUBLIC_TUNNEL_CONFIG=str(ROOT/'dist-exe/public-tunnel.json'), PYTHONPATH=str(ROOT/'host'), PYTHONUTF8='1', HF_HUB_OFFLINE='1', HF_HUB_DISABLE_TELEMETRY='1')
    # Reuse the local DSH gateway credential without persisting it in bot config.
    dsh_home = Path(os.environ.get('DSH_CHANNEL_PACK_STATE_DIR') or os.environ.get('DSH_HOME') or str(Path.home()/'.dsh'))
    dsh_key = os.environ.get('DSH_OPENAI_GATEWAY_API_KEY', '')
    if not dsh_key:
        try:
            dsh_key = (dsh_home/'openai-gateway/api-key').read_text(encoding='utf-8').strip()
        except OSError:
            pass
    if dsh_key:
        env['WHALE_DSH_MODEL_KEY'] = dsh_key
    bypass = ','.join(dict.fromkeys(item.strip() for item in ','.join([env.get('NO_PROXY', ''), env.get('no_proxy', ''), '127.0.0.1,localhost,::1']).split(',') if item.strip()))
    env['NO_PROXY'] = env['no_proxy'] = bypass
    image_settings = ROOT/'dist-exe/image-upload-secret.json'
    if image_settings.is_file():
        image_config = json.loads(image_settings.read_text(encoding='utf-8'))
        env.update(WHALE_REFERENCE_UPLOAD_URL=image_config['url'], WHALE_REFERENCE_UPLOAD_TOKEN=image_config['token'])
    credential_path = os.environ.get('WHALE_JIMENG_CREDENTIALS')
    if credential_path:
        env.update(credentials_from_file(Path(credential_path)))
    logs = ROOT/'runtime/logs'
    logs.mkdir(parents=True, exist_ok=True)
    children = []
    sys.path.insert(0, str(ROOT/'host'))
    from astrbot_whale_device.napcat_service import NapCatService
    from astrbot_whale_device.media_runtime import prepare_media_environment
    prepare_media_environment(env, ROOT/'runtime/media-bin')
    napcat = NapCatService(ROOT/'dist-exe')
    linux_runtime = ROOT/'dist-exe/qq-linux-runtime.json'
    if linux_runtime.is_file() and json.loads(linux_runtime.read_text(encoding='utf-8')).get('enabled') is True:
        from astrbot_whale_device.linux_qq_service import LinuxQQService
        napcat = LinuxQQService(ROOT)
    spec=importlib.util.spec_from_file_location('whale_qq_connect',ROOT/'tools/connect-astrbot-qq.py')
    qq_connect=importlib.util.module_from_spec(spec);spec.loader.exec_module(qq_connect)
    broker_spec=importlib.util.spec_from_file_location('whale_qq_broker',ROOT/'tools/qq-runtime-broker.py')
    broker_module=importlib.util.module_from_spec(broker_spec);broker_spec.loader.exec_module(broker_module)
    qq_broker=broker_module.QQRuntimeBroker(ROOT/'dist-exe/host.json',napcat,getattr(napcat, 'connect', qq_connect.connect_qq))
    flags = subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0
    with (logs/'embedding.log').open('a', encoding='utf-8') as emb_log, (logs/'astrbot.log').open('a', encoding='utf-8') as bot_log:
        try:
            bot = subprocess.Popen([sys.executable, 'main.py'], cwd=ASTR, env=env, stdout=bot_log, stderr=subprocess.STDOUT, creationflags=flags)
            children.append(bot)
            emb = None
            try:
                config = json.loads((ASTR/'data/cmd_config.json').read_text(encoding='utf-8-sig'))
                if any(p.get('id') == 'whale_embedding' and p.get('enable') is True for p in config.get('provider', [])):
                    embed = json.loads((ROOT/'dist-exe/embedding-secret.json').read_text(encoding='utf-8'))
                    embed_env = dict(env, WHALE_EMBEDDING_TOKEN=embed['token'])
                    emb = subprocess.Popen([sys.executable, '-m', 'astrbot_whale_device.local_embeddings'], cwd=ROOT,
                        env=embed_env, stdout=emb_log, stderr=subprocess.STDOUT, creationflags=flags)
                    children.append(emb)
            except (OSError, ValueError, KeyError, TypeError):
                print('Optional local embeddings could not start; AstrBot is still running.', flush=True)
            if os.environ.get('WHALE_SKIP_QQ_CONNECT')!='1':
                try:
                    qq_broker.start()
                    print('QQ startup preference loaded; account controls are ready.',flush=True)
                except Exception:
                    # QQ login availability does not stop AstrBot or the paired phone.
                    print('QQ login service is unavailable; open the account page to retry.',flush=True)
            (logs/'processes.json').write_text(json.dumps({'launcher_pid':os.getpid(),'embedding_pid':emb.pid if emb else None,'astrbot_pid':bot.pid}),encoding='utf-8')
            print('AstrBot process launched. Logs: runtime/logs/', flush=True)
            while bot.poll() is None:
                if emb is not None and emb.poll() is not None:
                    print('Optional local embeddings stopped; see runtime/logs/embedding.log. AstrBot is still running.', flush=True)
                    emb = None
                # Recover the account worker without restarting AstrBot or the phone.
                pending = qq_broker._state()
                worker = qq_broker._thread
                if (worker is None or not worker.is_alive()) and (
                    os.environ.get('WHALE_SKIP_QQ_CONNECT') != '1' or
                    (pending.get('login_in_progress') and not pending.get('logout_requested'))
                ):
                    try:
                        qq_broker.start()
                    except Exception:
                        pass
                time.sleep(1)
            return bot.returncode
        finally:
            for child in reversed(children):
                if child.poll() is None:
                    if os.name == 'nt': subprocess.run(['taskkill','/PID',str(child.pid),'/T','/F'],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,creationflags=flags)
                    else: child.terminate()
            qq_broker.stop()

if __name__ == '__main__': raise SystemExit(main())
