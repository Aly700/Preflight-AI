import test from 'node:test';
import assert from 'node:assert/strict';
import {createAdapter} from '../src/adapter.ts';

const messages = question => [{role:'user',content:[{type:'text',text:question}]}];
const response = scope => new Response(`event: delta\ndata: {"text":"Answer"}\n\nevent: done\ndata: ${JSON.stringify({scope})}\n\n`);
async function run(adapter,question='Show records') {
  let answer;
  for await (const part of adapter.run({messages:messages(question),abortSignal:new AbortController().signal})) answer=part;
  return answer.metadata.custom.preflight;
}

test('adapter snapshots active filters on each send, persists follow-ups and honors clear', async t => {
  let active={entity:'tasks',task_status:'awaiting_review',team_id:'TEAM-0001'};
  const sent=[];
  t.mock.method(globalThis,'fetch', async (_url,init) => {
    const body=JSON.parse(init.body);
    sent.push(body);
    return response(body.scope ?? {});
  });
  const adapter=createAdapter(()=>active);
  await run(adapter);
  await run(adapter,'And the next page?');
  assert.deepEqual(sent[0].scope,active);
  assert.deepEqual(sent[1].scope,active);
  active={};
  await run(adapter);
  assert.ok(!Object.hasOwn(sent[2],'scope'));
});

test('answer scope is the done-event snapshot even if the filters change mid-response', async t => {
  let active={entity:'issues',issue_status:'open'};
  const serverScope={entity:'issues',issue_status:'unresolved'};
  t.mock.method(globalThis,'fetch', async () => {
    active={entity:'tasks'};
    return response(serverScope);
  });
  const details=await run(createAdapter(()=>active));
  assert.deepEqual(details.scope,serverScope);
  assert.equal(details.complete,true);
  active={};
  assert.deepEqual(details.scope,serverScope);
});

test('document aircraft is sent as related_id and an old done frame shows no inferred scope', async t => {
  let sent;
  t.mock.method(globalThis,'fetch', async (_url,init) => {
    sent=JSON.parse(init.body);
    return new Response('event: done\ndata: {}\n\n');
  });
  const details=await run(createAdapter(()=>({entity:'documents',aircraft_id:'DEMO-AIR-0004',document_kind:'memo'})));
  assert.deepEqual(sent.scope,{entity:'documents',document_kind:'memo',related_id:'DEMO-AIR-0004'});
  assert.deepEqual(details.scope,{});
});

test('adapter retains refusal reasons and older failure frames through done',async t=>{
  const events=[
    {name:'search_issues',call_id:'a',status:'running'},
    {name:'search_issues',call_id:'a',status:'refused',reason:'scope'},
    {name:'count_issues',call_id:'b',status:'failed'},
  ];
  t.mock.method(globalThis,'fetch',async()=>new Response(events.map(data=>
    `event: tool\ndata: ${JSON.stringify(data)}\n\n`).join('')+'event: done\ndata: {}\n\n'));
  const details=await run(createAdapter());
  assert.deepEqual(details.tools,events.slice(1));
  assert.equal(details.complete,true);
});
