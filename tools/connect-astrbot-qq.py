"""Use NapCat's official local API to connect QQ to the official AstrBot adapter."""
import json, sys
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT/'host'))
from astrbot_whale_device.napcat_webui import NapCatWebUIClient
from astrbot_whale_device.gui_secrets import _tighten_permissions

def connect_qq():
    web = ROOT/'dist-exe/qq-data/NapCat/config/webui.json'
    client = NapCatWebUIClient(timeout=5)
    client.authenticate(json.loads(web.read_text(encoding='utf-8-sig'))['token'])
    status = client.login_status()
    if not status['logged_in']: raise RuntimeError('QQ login required')
    config = client._request('/OB11Config/GetConfig')
    backup = ROOT/'dist-exe/astrbot-backup/napcat-onebot.json'
    if not backup.exists():
        backup.parent.mkdir(parents=True, exist_ok=True)
        backup.write_text(json.dumps(config,ensure_ascii=False,indent=2),encoding='utf-8')
        _tighten_permissions(backup)
    astr = json.loads((ROOT/'runtime/astrbot/data/cmd_config.json').read_text(encoding='utf-8-sig'))
    platform = next(p for p in astr['platform'] if p['id']=='whale_qq')
    network = config['network']
    for server in network.get('websocketServers',[]):
        if server.get('name')=='Whale Desktop':server['enable']=False
    clients = network.setdefault('websocketClients',[])
    clients[:] = [c for c in clients if c.get('name')!='Whale AstrBot']
    clients.append({'name':'Whale AstrBot','enable':True,'url':'ws://127.0.0.1:6199/ws',
        'messagePostFormat':'array','reportSelfMessage':False,'reconnectInterval':5000,
        'token':platform['ws_reverse_token'],'debug':False,'heartInterval':30000})
    client._request('/OB11Config/SetConfig',{'config':json.dumps(config,ensure_ascii=False)})
    return {'logged_in':True,'adapter':'AstrBot official OneBot','reverse_port':6199,'legacy_consumer_disabled':True}

if __name__=='__main__': print(json.dumps(connect_qq()))
