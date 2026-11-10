import type {ChatModelAdapter, ThreadMessage} from '@assistant-ui/react';
import {readEvents} from './stream.mjs';
import {scopePayload,type Scope} from './scope.mjs';

export type ToolActivity = {name:string; status:string; call_id:string; reason?:string};
export type AnswerMetadata = {tools:ToolActivity[]; records:string[]; complete:boolean; scope:Scope};
const textOf = (message:ThreadMessage) => message.content.flatMap(part => part.type === 'text' ? [part.text] : []).join('\n');

export function createAdapter(getScope:()=>Scope=()=>({})):ChatModelAdapter {
  return {async *run({messages,abortSignal}) {
    const last = messages.at(-1);
    if (!last || last.role !== 'user') throw new Error('Enter a question to continue.');
    // Keep bounded recent context in the browser; the server independently validates it.
    const history = messages.slice(0,-1).filter(m => m.role === 'user' || m.role === 'assistant').slice(-12)
      .map(m => ({role:m.role,content:textOf(m).slice(0,4000)}));
    while (history.reduce((total,m)=>total+m.content.length,0)>16000) history.shift();
    const scope=scopePayload(getScope());
    const response = await fetch('/api/chat', {method:'POST',signal:abortSignal,
      headers:{'Content-Type':'application/json'},
      body:JSON.stringify({question:textOf(last),history,...(Object.keys(scope).length ? {scope} : {})})});
    if (!response.ok) {
      const body = await response.json().catch(()=>({}));
      throw new Error(typeof body.detail === 'string' ? body.detail : 'The server is unavailable. Try again shortly.');
    }
    let text = '';
    let details:AnswerMetadata = {tools:[],records:[],complete:false,scope:{}};
    for await (const frame of readEvents(response)) {
      if (frame.event === 'delta') text += frame.data.text;
      if (frame.event === 'tool') details = {...details,tools:[...details.tools.filter(t=>t.call_id!==frame.data.call_id),frame.data]};
      if (frame.event === 'sources') details = {...details,records:frame.data.records};
      if (frame.event === 'done') details = {...details,complete:true,scope:{...(frame.data.scope ?? {})}};
      yield {content:[{type:'text',text}],metadata:{custom:{preflight:details}}};
    }
  }};
}
