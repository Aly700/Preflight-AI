import {useEffect,useMemo,useRef,useState} from 'react';
import {AssistantRuntimeProvider,useLocalRuntime,ThreadPrimitive,MessagePrimitive,
  ComposerPrimitive,ActionBarPrimitive,
  ErrorPrimitive,AuiIf,useAui,useAuiState} from '@assistant-ui/react';
import {MarkdownTextPrimitive} from '@assistant-ui/react-markdown';
import {ArrowUp,ArrowDown,ChevronDown,Copy,Database,History,
  Plane,RotateCcw,ShieldCheck,Square} from 'lucide-react';
import {createAdapter,type AnswerMetadata} from './adapter';
import {remarkPlugins} from './markdown.mjs';
import {ToolActivity} from './ToolActivity';
import {ScopeFilters} from './ScopeFilters';
import {decodeScope,detectMismatches,encodeScope,normalizeScope,scopeChips,starterQuestions,type Scope} from './scope.mjs';

const suggestionIcons=[Plane,Database,History];

export function App(){
  const [scope,setScope]=useState<Scope>(()=>decodeScope(window.location.search));
  const scopeRef=useRef(scope);
  const adapter=useMemo(()=>createAdapter(()=>scopeRef.current),[]);
  const runtime=useLocalRuntime(adapter);
  function changeScope(next:Scope){
    const clean=normalizeScope(next);
    scopeRef.current=clean;
    setScope(clean);
    window.history.replaceState(null,'',window.location.pathname+encodeScope(clean,window.location.search)+window.location.hash);
  }
  useEffect(()=>{
    function loadScope(){changeScope(decodeScope(window.location.search));}
    loadScope();
    window.addEventListener('popstate',loadScope);
    return ()=>window.removeEventListener('popstate',loadScope);
  },[]);
  async function refreshChat(){
    await runtime.thread.composer.reset();
    runtime.thread.reset();
    document.querySelector<HTMLTextAreaElement>('textarea')?.focus();
  }
  return <AssistantRuntimeProvider runtime={runtime}>
    <div className="app-shell">
      <main className="chat-main">
        <header className="chat-header"><div className="header-title"><span>Preflight assistant</span><span className="header-divider"/><span className="mode-label">Structured records</span></div><RefreshChat onRefresh={refreshChat}/></header>
        <Thread scope={scope} onScopeChange={changeScope}/>
      </main>
    </div>
  </AssistantRuntimeProvider>;
}

function RefreshChat({onRefresh}:{onRefresh:()=>Promise<void>}){
  const isRunning=useAuiState(s=>s.thread.isRunning);
  return <button type="button" className="refresh-chat" onClick={onRefresh}
    disabled={isRunning} title={isRunning?'Stop the response before refreshing':'Clear this conversation and start fresh'}>
    <RotateCcw size={15} aria-hidden="true"/>Refresh chat
  </button>;
}

function Thread({scope,onScopeChange}:{scope:Scope;onScopeChange:(scope:Scope)=>void}){
  const aui=useAui();
  const question=useAuiState(s=>s.composer.text);
  const warnings=detectMismatches(question,scope);
  const suggestions=starterQuestions(scope);
  function prepareQuestion(text:string){
    aui.composer().setText(text);
    document.querySelector<HTMLTextAreaElement>('textarea')?.focus();
  }
  return <ThreadPrimitive.Root className="thread-root">
    <ThreadPrimitive.Viewport className="thread-viewport">
      <AuiIf condition={s=>s.thread.isEmpty}><section className="welcome"><div className="welcome-mark"><Plane size={27}/></div><h1>What would you like to understand?</h1><p>Explore aircraft, issues, and activity history.<br/>Ask a question, then follow the records.</p><div className="suggestions">{suggestions.map(({title,detail,question},index)=>{const Icon=suggestionIcons[index];return <button key={title} onClick={()=>prepareQuestion(question)}><Icon size={19}/><strong>{title}</strong><span>{detail}</span></button>;})}</div></section></AuiIf>
      <div className="message-list"><ThreadPrimitive.Messages>{({message})=>message.role==='user'?<UserMessage/>:<AssistantMessage/>}</ThreadPrimitive.Messages></div>
      <ThreadPrimitive.ScrollToBottom className="scroll-bottom" aria-label="Scroll to latest message"><ArrowDown size={18}/></ThreadPrimitive.ScrollToBottom>
    </ThreadPrimitive.Viewport>
    <div className="composer-area">
      <ScopeFilters scope={scope} onChange={onScopeChange} onPrepare={prepareQuestion}/>
      <div id="scope-warning" className="scope-warning" role="status" aria-live="polite">
        {warnings.length>0&&<p>{warnings.join(' ')} You can edit the question or filters, or send as written.</p>}
      </div>
      <ComposerPrimitive.Root className="composer"><ComposerPrimitive.Input aria-label="Message Preflight AI" aria-describedby={warnings.length ? 'scope-warning' : undefined} placeholder="Ask Preflight AI…" rows={1} maxLength={2000}/><div className="composer-toolbar"><span><Database size={13}/>Synthetic records</span><AuiIf condition={s=>!s.thread.isRunning}><ComposerPrimitive.Send className="send-button" aria-label="Send message"><ArrowUp size={19}/></ComposerPrimitive.Send></AuiIf><AuiIf condition={s=>s.thread.isRunning}><ComposerPrimitive.Cancel className="send-button" aria-label="Stop response"><Square size={15} fill="currentColor"/></ComposerPrimitive.Cancel></AuiIf></div></ComposerPrimitive.Root><p className="disclaimer">AI can make mistakes. Check the source records. Fictional data; not for maintenance decisions.</p></div>
  </ThreadPrimitive.Root>;
}

function UserMessage(){return <MessagePrimitive.Root className="user-message"><div className="user-bubble"><MessagePrimitive.Parts/></div></MessagePrimitive.Root>;}

function AssistantMessage(){
  const metadata=useAuiState(s=>s.message.metadata.custom.preflight) as AnswerMetadata|undefined;
  const status=useAuiState(s=>s.message.status);
  const chips=scopeChips(metadata?.scope ?? {});
  return <MessagePrimitive.Root className="assistant-message"><div className="assistant-avatar"><Plane size={17}/></div><div className="assistant-content"><div className="assistant-name">Preflight AI <span>Assistant</span></div>
    {chips.length>0&&<div className="answer-scope"><span>Scoped to:</span><ul className="scope-chips" aria-label="Answer scope">{chips.map(({key,label})=><li key={key}>{label}</li>)}</ul></div>}
    {metadata?.tools.length ? <ToolActivity tools={metadata.tools} complete={metadata.complete || status?.type!=='running'}/>:status?.type==='running'?<div className="thinking" role="status"><span className="loading-dot"/>Looking into your question…</div>:null}
    <div className="markdown"><MessagePrimitive.Parts components={{Text:MarkdownText}}/></div>
    <MessagePrimitive.Error><div className="message-error" role="alert"><ErrorPrimitive.Message/></div></MessagePrimitive.Error>
    {metadata?.complete&&metadata.records.length>0&&<details className="source-records"><summary><ShieldCheck size={13}/>{metadata.records.length} source record{metadata.records.length===1?'':'s'}<ChevronDown size={12}/></summary><div>{metadata.records.map(id=><code key={id}>{id}</code>)}</div></details>}
    <ActionBarPrimitive.Root className="message-actions" hideWhenRunning><ActionBarPrimitive.Copy className="icon-button" aria-label="Copy answer"><Copy size={14}/></ActionBarPrimitive.Copy><ActionBarPrimitive.Reload className="icon-button" aria-label="Retry answer"><RotateCcw size={14}/></ActionBarPrimitive.Reload></ActionBarPrimitive.Root>
  </div></MessagePrimitive.Root>;
}

function MarkdownText(){return <MarkdownTextPrimitive remarkPlugins={remarkPlugins}/>;}
