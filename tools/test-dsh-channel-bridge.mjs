import {test} from 'node:test';
import assert from 'node:assert/strict';
import http from 'node:http';
import {apply} from './dsh-channel-bridge.mjs';

test('local management bridge preserves host auth and limits account exposure',async()=>{
 process.env.DSH_OPENAI_GATEWAY_API_KEY='local-test-key';
 process.env.WHALE_DSH_MANAGEMENT_PORT='18327';
 const seen=[];
 const upstream=http.createServer(async(req,res)=>{
  if(req.url==='/'){
   assert.equal(new URL(req.url,'http://host').search,'');
   res.writeHead(302,{'set-cookie':'session=test-only; HttpOnly; SameSite=Strict','location':'/'});res.end();return;
  }
  assert.equal(req.headers.cookie,'session=test-only');
  let body='';for await(const part of req)body+=part;
  const call=JSON.parse(body);seen.push(call.payload);
  res.setHeader('Content-Type','application/json');
  res.end(JSON.stringify({result:{ok:true,value:call.payload.method==='account.list'?{accounts:[{id:'a',provider:'trae',nickname:'Alice',refreshable:true,credentialRef:'private',accessToken:'secret'}]}:{success:true}}}));
 });
 await new Promise(resolve=>upstream.listen(0,'127.0.0.1',resolve));
 let close;
 apply({webServer:{port:upstream.address().port},connection:{authenticatedUrl:base=>base},effect:fn=>{close=fn()},logger:{warn:()=>{}}});
 const call=(method,key='local-test-key')=>fetch('http://127.0.0.1:18327/rpc',{method:'POST',headers:{Authorization:'Bearer '+key,'Content-Type':'application/json'},body:JSON.stringify({method,payload:{provider:'trae'}})});
 try {
  assert.equal((await call('account.list','wrong')).status,401);
  assert.equal((await call('backup.export')).status,400);
  const result=await (await call('account.list')).json();
  assert.equal(result.value.accounts[0].id,'a');
  assert.ok(!JSON.stringify(result).includes('private'));
  assert.ok(!JSON.stringify(result).includes('secret'));
  assert.equal((await (await call('account.refresh')).json()).value.success,true);
  assert.deepEqual(seen.map(x=>x.method),['account.list','account.refresh']);
 }finally{close();await new Promise(resolve=>upstream.close(resolve))}
});
