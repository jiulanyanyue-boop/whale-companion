/** Local, authenticated account-management adapter for Whale. */
import http from 'node:http';
import fs from 'node:fs';
import path from 'node:path';
import os from 'node:os';
import { timingSafeEqual } from 'node:crypto';
export const name = 'whale-channel-bridge';
export const inject = ['connection', 'webServer'];
const METHODS = new Set(['account.list', 'account.refresh', 'credits.balances', 'credits.status', 'credits.claimAll', 'usage.autoCheckin']);
export function apply(ctx) {
  const server = http.createServer(async (req, res) => {
    const send = (status, data) => { res.writeHead(status, {'Content-Type':'application/json','Cache-Control':'no-store'}); res.end(JSON.stringify(data)); };
    try {
      const home = process.env.DSH_CHANNEL_PACK_STATE_DIR || process.env.DSH_HOME || path.join(os.homedir(), '.dsh');
      const key = process.env.DSH_OPENAI_GATEWAY_API_KEY || fs.readFileSync(path.join(home, 'openai-gateway/api-key'), 'utf8').trim();
      const actual = Buffer.from(req.headers.authorization || '');
      const expected = Buffer.from(`Bearer ${key}`);
      if (!key || actual.length !== expected.length || !timingSafeEqual(actual, expected)) return send(401, {ok:false});
      if (req.method !== 'POST' || req.url !== '/rpc') return send(404, {ok:false});
      let body = '';
      for await (const part of req) { body += part; if (Buffer.byteLength(body)>8192) return send(413,{ok:false}); }
      const {method, payload={}} = JSON.parse(body);
      if (!METHODS.has(method) || !payload || typeof payload !== 'object' || Array.isArray(payload)) return send(400,{ok:false});
      // Use the host's supported login exchange; never read or duplicate account credentials.
      const base = `${ctx.webServer.protocol || "http:"}//127.0.0.1:${ctx.webServer.port}`;
      const auth = await fetch(ctx.connection.authenticatedUrl(base), {redirect:'manual', signal:AbortSignal.timeout(5000)});
      const cookie = auth.headers.get('set-cookie')?.split(';',1)[0];
      if (!cookie) return send(503,{ok:false});
      const upstream = await fetch(base+'/api/channel-pack', {method:'POST',headers:{'Content-Type':'application/json',Cookie:cookie,Origin:base},body:JSON.stringify({type:'client-request',rpcId:crypto.randomUUID(),method:'channel-pack',payload:{method,payload}}),signal:AbortSignal.timeout(60000)});
      if (!upstream.ok) return send(502,{ok:false});
      const result = (await upstream.json()).result;
      // Project only public account fields; credential refs and tokens stay inside DSH.
      if (method === 'account.list' && result?.ok) result.value.accounts = (result.value.accounts || []).map(a=>({id:a.id,provider:a.provider,nickname:a.nickname,expiresAt:a.expiresAt,refreshable:a.refreshable,enabled:a.enabled}));
      send(200,result);
    } catch { send(503,{ok:false,error:{message:'DSH account management is temporarily unavailable'}}); }
  });
  server.on('error',()=>ctx.logger?.warn?.('Whale channel bridge could not bind its local port.'));
  server.listen(Number(process.env.WHALE_DSH_MANAGEMENT_PORT || 8327),'127.0.0.1');
  ctx.effect(()=>()=>server.close(), 'Whale account bridge');
}
