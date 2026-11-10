import './tsx-loader.mjs';
import {registerHooks} from 'node:module';
import test from 'node:test';
import assert from 'node:assert/strict';
import {renderToStaticMarkup} from 'react-dom/server';
import {reset,render,nodes,thread} from './filter-harness.mjs';
registerHooks({resolve(specifier,context,nextResolve){
  if(context.parentURL?.endsWith('/ScopeFilters.tsx')) {
    if(['react','@assistant-ui/react'].includes(specifier))
      return {url:new URL('./filter-harness.mjs',import.meta.url).href,shortCircuit:true};
  }
  return nextResolve(specifier,context);
}});
const {ScopeFilters}=await import('../src/ScopeFilters.tsx');
const scope={entity:'issues',aircraft_id:'DEMO-AIR-0004',issue_status:'unresolved'};
const find=(tree,className)=>nodes(tree,node=>node.props?.className===className)[0];
function panel(tree,expanded){
  assert.equal(find(tree,'filter-toggle').props['aria-expanded'],expanded);
  assert.equal(nodes(tree,node=>node.props?.id==='filter-panel')[0].props.hidden,!expanded);
  assert.match(renderToStaticMarkup(find(tree,'scope-chips active-scope')),/DEMO-AIR-0004/);
}

test('filters start collapsed at phone widths with active chips still visible',()=>{
  for(const width of [390,639]) {
    reset();globalThis.window={innerWidth:width};
    panel(render(ScopeFilters,{scope,onChange(){},onPrepare(){}}),false);
  }
  delete globalThis.window;
});

test('Prepare question collapses the open panel without sending or losing chips',()=>{
  reset();let prepared;
  const props={scope,onChange(){assert.fail('Scope must stay selected');},onPrepare:text=>prepared=text};
  let tree=render(ScopeFilters,props);
  find(tree,'filter-toggle').props.onClick();tree=render(ScopeFilters,props);panel(tree,true);
  find(tree,'prepare-question').props.onClick();tree=render(ScopeFilters,props);
  assert.match(prepared,/DEMO-AIR-0004/);panel(tree,false);
});

test('a sent message collapses filters; finishing leaves them closed and the toggle can reopen them',()=>{
  reset();const props={scope,onChange(){},onPrepare(){}};
  let tree=render(ScopeFilters,props);
  find(tree,'filter-toggle').props.onClick();tree=render(ScopeFilters,props);panel(tree,true);
  thread.isRunning=true;tree=render(ScopeFilters,props);panel(tree,false);
  thread.isRunning=false;tree=render(ScopeFilters,props);panel(tree,false);
  find(tree,'filter-toggle').props.onClick();panel(render(ScopeFilters,props),true);
});

test('aircraft option displays compact text and exposes full program and station',()=>{
  reset();const tree=render(ScopeFilters,{scope,onChange(){},onPrepare(){}});
  const option=nodes(tree,node=>node.type==='option' && node.props.value==='DEMO-AIR-0004')[0];
  assert.equal(option.props.children,'DEMO-AIR-0004 (BETA · Station 02)');
  assert.equal(option.props['aria-label'],'DEMO-AIR-0004 · DEMO-PROGRAM-BETA · DEMO-STATION-02');
});

test('aircraft occupies a full filter row so its compact label fits on a phone',async()=>{
  const {readFile}=await import('node:fs/promises');
  reset();const tree=render(ScopeFilters,{scope,onChange(){},onPrepare(){}});
  assert.ok(nodes(tree,node=>node.props?.className?.includes('filter-field-aircraft')).length);
  const css=await readFile(new URL('../src/styles.css',import.meta.url),'utf8');
  assert.match(css,/\.filter-field-aircraft\s*\{\s*grid-column:\s*1\s*\/\s*-1\s*\}/);
});
