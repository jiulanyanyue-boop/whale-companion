"""CPU-only local embedding endpoint for AstrBot's official OpenAI provider."""
from __future__ import annotations
import argparse,hmac,json,os,threading
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
from pathlib import Path
MODEL='BAAI/bge-small-zh-v1.5'
class EmbeddingServer(ThreadingHTTPServer):
 daemon_threads=True
 def __init__(self,address,token,model):
  if address[0]!='127.0.0.1' or len(token)<32:raise ValueError('loopback and credential required')
  self.token=token;self.model=model;self.lock=threading.Lock();super().__init__(address,Handler)
class Handler(BaseHTTPRequestHandler):
 def log_message(self,*a):pass
 def do_POST(self):
  if self.path not in ['/v1/embeddings','/embeddings']:return self.reply(404,{'error':'not_found'})
  if not hmac.compare_digest(self.headers.get('Authorization',''),'Bearer '+self.server.token):return self.reply(401,{'error':'unauthorized'})
  try:
   n=int(self.headers.get('Content-Length',0))
   if not 0<n<=256000:raise ValueError()
   body=json.loads(self.rfile.read(n));items=body.get('input');items=[items] if isinstance(items,str) else items
   if not isinstance(items,list) or not 1<=len(items)<=32 or any(not isinstance(s,str) or len(s)>12000 for s in items):raise ValueError()
   if body.get('model',MODEL)!=MODEL:raise ValueError()
   with self.server.lock:vectors=list(self.server.model.embed(items,batch_size=8))
   return self.reply(200,{'object':'list','model':MODEL,'data':[{'object':'embedding','index':i,'embedding':v.tolist()} for i,v in enumerate(vectors)],'usage':{'prompt_tokens':0,'total_tokens':0}})
  except Exception:return self.reply(400,{'error':'invalid_embedding_request'})
 def reply(self,status,data):
  raw=json.dumps(data).encode();self.send_response(status);self.send_header('Content-Type','application/json');self.send_header('Content-Length',str(len(raw)));self.end_headers();self.wfile.write(raw)
def main():
 parser=argparse.ArgumentParser();parser.add_argument('--port',type=int,default=8768);parser.add_argument('--cache',default=str(Path(__file__).resolve().parents[2]/'runtime/models'));args=parser.parse_args()
 from fastembed import TextEmbedding
 token=os.environ.get('WHALE_EMBEDDING_TOKEN','')
 if len(token)<32:raise SystemExit('Local embedding credential missing')
 model=TextEmbedding(model_name=MODEL,cache_dir=args.cache,threads=2)
 list(model.embed(['就绪']))
 print('Local CPU embedding ready on 127.0.0.1:'+str(args.port),flush=True)
 server=EmbeddingServer(('127.0.0.1',args.port),token,model)
 try:server.serve_forever()
 finally:server.server_close()
if __name__=='__main__':main()
