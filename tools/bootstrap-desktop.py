"""Build a clean integrated installation from pinned upstream source revisions."""
from __future__ import annotations
import argparse,hashlib,json,os,secrets,shutil,subprocess,sys,urllib.request,venv,zipfile
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
ASTRBOT_REV='10364e85a35d2f76bfcd706e1505652817565065'

def run(*args,cwd=None):subprocess.run([str(x) for x in args],cwd=cwd,check=True)
def write(path,value):
    path.parent.mkdir(parents=True,exist_ok=True)
    path.write_text(json.dumps(value,ensure_ascii=False,indent=2),encoding='utf-8')

def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--owner',required=True,help='Your QQ ID; the assistant account is logged in separately.')
    parser.add_argument('--model',default='deepseek-flash')
    parser.add_argument('--api-base',default='https://api.deepseek.com')
    parser.add_argument('--device-id',default='whale-phone')
    parser.add_argument('--download-embedding-model',action='store_true')
    args=parser.parse_args()
    if not args.owner.isdigit():parser.error('--owner must be a numeric QQ ID')
    if not (3,11)<=sys.version_info[:2]<(3,14):raise SystemExit('Use Python 3.12 for the packaged CPU embedding dependencies.')
    runtime=ROOT/'runtime';runtime.mkdir(exist_ok=True)
    checkout=runtime/'astrbot-source'
    if not checkout.exists():run('git','clone','--filter=blob:none','https://github.com/AstrBotDevs/AstrBot.git',checkout)
    run('git','checkout','--detach',ASTRBOT_REV,cwd=checkout)
    patch=ROOT/'patches/astrbot.patch'
    if patch.is_file():
        check=subprocess.run(['git','apply','--reverse','--check',str(patch)],cwd=checkout,capture_output=True)
        if check.returncode:
            run('git','apply','--check',patch,cwd=checkout)
            run('git','apply',patch,cwd=checkout)
    bot=runtime/'astrbot';shutil.copytree(checkout,bot,ignore=shutil.ignore_patterns('.git','.venv','node_modules','data'),dirs_exist_ok=True)
    pyenv=runtime/'astrbot-venv'
    if not pyenv.exists():venv.EnvBuilder(with_pip=True).create(pyenv)
    python=pyenv/('Scripts/python.exe' if os.name=='nt' else 'bin/python')
    run(python,'-m','pip','install','-e',bot,'fastembed','onnxruntime','tos','faster-whisper>=1.2,<2','av>=11,<17','imageio-ffmpeg>=0.6,<1')
    for name in ['astrbot_plugin_whale_device', 'astrbot_plugin_whale_memes', 'astrbot_plugin_livingmemory', 'astrbot_proactive_reply']:
        src=ROOT/'plugin-sources'/name
        if not src.exists():src=bot/'data/plugins'/name
        if src.is_dir():
            destination=bot/'data/plugins'/name
            if src.resolve()!=destination.resolve():shutil.copytree(src,destination,dirs_exist_ok=True)
            if (destination/'requirements.txt').is_file():run(python,'-m','pip','install','-r',destination/'requirements.txt')
    run(sys.executable,'-m','pip','install','-e',str(ROOT/'host')+'[desktop,runtime]','pyinstaller')
    sys.path.insert(0,str(ROOT/'host'))
    from astrbot_whale_device.tls_cert import ensure_self_signed_certificate
    from astrbot_whale_device.gui_secrets import StoredSecrets,_tighten_permissions
    from astrbot_whale_device.astrbot_native_bridge import ensure_rpc_token
    local=ROOT/'dist-exe';local.mkdir(exist_ok=True)
    cert=local/'certs/cert.pem';key=local/'certs/key.pem';ensure_self_signed_certificate(certfile=cert,keyfile=key)
    cfg=local/'host.json'
    if not cfg.exists():write(cfg,{'owner_user_id':args.owner,'device_id':args.device_id,'allowed_packages':[],
       'blocked_packages':[],'signing_secret_env':'WHALE_SIGNING_SECRET','model_api_key_env':'WHALE_MODEL_KEY',
       'model':{'provider_id':'openai','model_id':args.model,'base_url':args.api_base},
       'device_server':{'host':'0.0.0.0','port':8765,'certfile':str(cert),'keyfile':str(key)}})
    credentials=StoredSecrets.load(cfg)
    if not credentials.signing_secret:credentials.signing_secret=secrets.token_hex(32)
    # The builder's API key is never requested or baked into a distributable.
    credentials.save(cfg);ensure_rpc_token(str(cfg))
    if not (bot/'data/cmd_config.json').exists():
        env=os.environ.copy();env['ASTRBOT_ROOT']=str(bot)
        subprocess.run([str(python),'-c','from astrbot.core import astrbot_config'],cwd=bot,env=env,check=True)
    seed_script="""import asyncio
from astrbot.core import db_helper
async def main():
 await db_helper.initialize()
 for name in ['whale_girl','whale_owner','whale_group']:
  if await db_helper.get_persona_by_id(name) is None:
   await db_helper.insert_persona(persona_id=name,system_prompt='用简体中文自然简短地交流。请在官方人格设定编辑此内容。',tools=None,skills=[])
 await db_helper.engine.dispose()
asyncio.run(main())
"""
    env=os.environ.copy();env['ASTRBOT_ROOT']=str(bot)
    subprocess.run([str(python),'-c',seed_script],cwd=bot,env=env,check=True)
    setup_complete = local / '.integration-configured'
    if not setup_complete.is_file():
        run(python,ROOT/'tools/configure-astrbot.py','--activate-desktop-mode')
        run(python,ROOT/'tools/configure-astrbot-extras.py')
        setup_complete.touch()
    if args.download_embedding_model:
        run(python,'-c',"from fastembed import TextEmbedding; m=TextEmbedding(model_name='BAAI/bge-small-zh-v1.5',cache_dir="+repr(str(runtime/'models'))+",threads=2); list(m.embed(['ready']))")
        run(python,ROOT/'tools/configure-astrbot-extras.py','--enable-local-embedding')
    dashboard=bot/'astrbot/dashboard/dist'
    if not (dashboard/'index.html').exists():
        req=urllib.request.Request('https://api.github.com/repos/AstrBotDevs/AstrBot/releases/tags/v4.29.4',headers={'User-Agent':'Whale-Desktop-Builder'})
        with urllib.request.urlopen(req,timeout=30) as response:release=json.load(response)
        asset=next((a for a in release.get('assets',[]) if a['name']=='AstrBot-v4.29.4-dashboard.zip'),None)
        if not asset:raise RuntimeError('Official v4.29.4 dashboard archive is unavailable.')
        archive=runtime/'dashboard-v4.29.4.zip';urllib.request.urlretrieve(asset['browser_download_url'],archive)
        if hashlib.sha256(archive.read_bytes()).hexdigest() != '12f394569e5b7971a53218e261917032f8c499a504584de3af8dacfd7557af74':
            raise RuntimeError('Official dashboard archive checksum mismatch.')
        dashboard.parent.mkdir(parents=True,exist_ok=True)
        with zipfile.ZipFile(archive) as bundle:
            for item in bundle.infolist():
                if not (dashboard.parent/item.filename).resolve().is_relative_to(dashboard.parent.resolve()):raise ValueError('invalid dashboard archive path')
            bundle.extractall(dashboard.parent)
    print('Bootstrap ready. Configure a vision-capable chat model and its API key in AstrBot. Build with tools/build-host.ps1.')

if __name__=='__main__':main()
