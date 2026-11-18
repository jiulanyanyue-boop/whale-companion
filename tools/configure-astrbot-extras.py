"""Configure local-only memory retrieval and the shared meme catalog."""
import json, secrets, sys
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
ASTR = ROOT / 'runtime/astrbot'
sys.path.insert(0, str(ROOT / 'host'))
from astrbot_whale_device.gui_secrets import _tighten_permissions

def save(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix('.tmp')
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
    tmp.replace(path)

cmd_path = ASTR / 'data/cmd_config.json'
memory_path = ASTR/'data/config/astrbot_plugin_livingmemory_config.json'
cmd = json.loads(cmd_path.read_text(encoding='utf-8-sig'))
if sys.argv[1:] == ['--enable-local-embedding']:
    secret_path = ROOT/'dist-exe/embedding-secret.json'
    if not secret_path.exists():
        save(secret_path, {'token': secrets.token_urlsafe(48)})
    _tighten_permissions(secret_path)
    token = json.loads(secret_path.read_text(encoding='utf-8'))['token']
    providers = cmd.setdefault('provider', [])
    providers[:] = [p for p in providers if p.get('id') != 'whale_embedding']
    providers.append({'id':'whale_embedding','type':'openai_embedding','provider':'openai',
        'provider_type':'embedding','enable':True,'embedding_api_key':token,
        'embedding_api_base':'http://127.0.0.1:8768/v1','embedding_model':'BAAI/bge-small-zh-v1.5',
        'embedding_dimensions':512,'embedding_dimensions_mode':'never','timeout':30,'proxy':''})
    memory = json.loads(memory_path.read_text(encoding='utf-8-sig'))
    settings = memory.setdefault('provider_settings', {})
    if not settings.get('embedding_provider_id'):
        settings['embedding_provider_id'] = 'whale_embedding'
        save(memory_path, memory)
    save(cmd_path, cmd)
    _tighten_permissions(cmd_path)
    print('Local embeddings enabled.')
    raise SystemExit(0)
if sys.argv[1:]:
    raise SystemExit('Usage: configure-astrbot-extras.py [--enable-local-embedding]')
cmd.setdefault('agent_runner', {}).setdefault('config', {}).setdefault('compression', {})['max_turns'] = -1
cmd.setdefault('dashboard', {})['host'] = '127.0.0.1'
cmd['admins_id'] = [str(json.loads((ROOT/'dist-exe/host.json').read_text(encoding='utf-8-sig'))['owner_user_id'])]
save(cmd_path, cmd)
_tighten_permissions(cmd_path)
save(memory_path, {
 'bot_language':'zh',
 'provider_settings':{'embedding_provider_id':'','llm_provider_id':'whale_chat','llm_stream_mode':'auto'},
 'session_manager':{'enable_full_group_capture':False,'context_window_size':24},
 'recall_engine':{'top_k':4,'rerank_enabled':False},
 'graph_memory':{'enabled':False},
 'reflection_engine':{'summary_trigger_rounds':10,'max_segments_per_run':2,'max_messages_per_segment':40},
 'agent_tools':{'enable_recall_tool':False,'enable_agentic_recall_beta':False,'enable_memorize_tool':False,'enable_forget_tool':False}})
save(ASTR/'data/config/astrbot_plugin_whale_memes_config.json', {
 'library_path':str(ROOT/'data/memes'),'allow_generation':True,'daily_generation_limit':5,'duplicate_cooldown_seconds':90})
print('Scoped memory, official recent history and meme library configured.')
