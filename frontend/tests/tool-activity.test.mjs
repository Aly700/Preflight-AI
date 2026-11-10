import './tsx-loader.mjs';
import test from 'node:test';
import assert from 'node:assert/strict';
import {createElement} from 'react';
import {renderToStaticMarkup} from 'react-dom/server';

const tool=(status,reason)=>({name:'search_issues',call_id:crypto.randomUUID(),status,...(reason ? {reason} : {})});
async function render(tools,complete) {
  const {ToolActivity}=await import('../src/ToolActivity.tsx');
  return renderToStaticMarkup(createElement(ToolActivity,{tools,complete}));
}

test('tool activity remains running until the answer finishes, even after all lookups succeed',async()=>{
  assert.match(await render([tool('complete')],false),/Reading records…/);
  assert.match(await render([tool('failed'),tool('refused','scope')],false),/Reading records…/);
  assert.match(await render([tool('complete')],true),/Checked source records/);
});

test('completed tool activity distinguishes scope refusals and legacy failures with plural counts',async()=>{
  for(const [scope,failed] of [[1,2],[2,1],[0,1],[1,0],[0,0]]) {
    const html=await render([tool('complete'),...Array.from({length:scope},()=>tool('refused','scope')),
      ...Array.from({length:failed},()=>tool('failed'))],true);
    const summary=html.match(/<summary>(.*?)<\/summary>/s)[1].replace(/<[^>]*>/g,'');
    assert.equal(summary,'Checked source records'+
      (scope ? ` · ${scope} lookup${scope===1?'':'s'} outside the selected scope` : '')+
      (failed ? ` · ${failed} lookup${failed===1?'':'s'} failed` : ''));
    assert.doesNotMatch(html,/loading-dot|Reading records/);
  }
});

test('expanded tool rows provide distinct accessible success, scope and failure icons',async()=>{
  const html=await render([tool('complete'),tool('refused','scope'),tool('failed')],true);
  for(const label of ['Complete','Outside the selected scope','Failed']) assert.ok(html.includes(`aria-label="${label}"`));
  for(const icon of ['lucide-check','lucide-scan-search','lucide-circle-alert']) assert.ok(html.includes(icon));
});
