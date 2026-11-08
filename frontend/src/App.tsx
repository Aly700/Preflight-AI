import {useMemo} from 'react';
import {AssistantRuntimeProvider,useLocalRuntime,ThreadPrimitive,MessagePrimitive,
  ComposerPrimitive,ActionBarPrimitive,
  ErrorPrimitive,AuiIf,useAui,useAuiState} from '@assistant-ui/react';
import {MarkdownTextPrimitive} from '@assistant-ui/react-markdown';
import {ArrowUp,ArrowDown,Check,ChevronDown,Copy,Database,History,
  Plane,RotateCcw,ShieldCheck,Square} from 'lucide-react';
import {createAdapter,type AnswerMetadata} from './adapter';
import {remarkPlugins} from './markdown.mjs';

const suggestions = [
  {icon:Plane,title:'Get the aircraft picture',detail:'Issues, progress, and work remaining',question:'Summarize DEMO-AIR-0001, including issue and task counts.'},
  {icon:Database,title:'Find unresolved issues',detail:'See what still needs attention',question:'List the first five unresolved issues and the total unresolved count.'},
  {icon:History,title:'Follow an issue’s history',detail:'Understand how a record changed',question:'Show the activity history for ISSUE-0001.'},
];

export function App(){
  const adapter=useMemo(()=>createAdapter(),[]);
  const runtime=useLocalRuntime(adapter);
  async function refreshChat(){
    await runtime.thread.composer.reset();
    runtime.thread.reset();
    document.querySelector<HTMLTextAreaElement>('textarea')?.focus();
  }
  return <AssistantRuntimeProvider runtime={runtime}>
    <div className="app-shell">
      <main className="chat-main">
        <header className="chat-header"><div className="header-title"><span>Preflight assistant</span><span className="header-divider"/><span className="mode-label">Structured records</span></div><RefreshChat onRefresh={refreshChat}/></header>
        <Thread/>
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

function Thread(){
  const aui=useAui();
  return <ThreadPrimitive.Root className="thread-root">
    <ThreadPrimitive.Viewport className="thread-viewport">
      <AuiIf condition={s=>s.thread.isEmpty}><section className="welcome"><div className="welcome-mark"><Plane size={27}/></div><h1>What would you like to understand?</h1><p>Explore aircraft, issues, and activity history.<br/>Ask a question, then follow the records.</p><div className="suggestions">{suggestions.map(({icon:Icon,title,detail,question})=><button key={title} onClick={()=>{aui.composer().setText(question);document.querySelector<HTMLTextAreaElement>('textarea')?.focus();}}><Icon size={19}/><strong>{title}</strong><span>{detail}</span></button>)}</div></section></AuiIf>
      <div className="message-list"><ThreadPrimitive.Messages>{({message})=>message.role==='user'?<UserMessage/>:<AssistantMessage/>}</ThreadPrimitive.Messages></div>
      <ThreadPrimitive.ScrollToBottom className="scroll-bottom" aria-label="Scroll to latest message"><ArrowDown size={18}/></ThreadPrimitive.ScrollToBottom>
    </ThreadPrimitive.Viewport>
    <div className="composer-area"><ComposerPrimitive.Root className="composer"><ComposerPrimitive.Input aria-label="Message Preflight AI" placeholder="Ask Preflight AI…" rows={1} maxLength={2000}/><div className="composer-toolbar"><span><Database size={13}/>Synthetic records</span><AuiIf condition={s=>!s.thread.isRunning}><ComposerPrimitive.Send className="send-button" aria-label="Send message"><ArrowUp size={19}/></ComposerPrimitive.Send></AuiIf><AuiIf condition={s=>s.thread.isRunning}><ComposerPrimitive.Cancel className="send-button" aria-label="Stop response"><Square size={15} fill="currentColor"/></ComposerPrimitive.Cancel></AuiIf></div></ComposerPrimitive.Root><p className="disclaimer">AI can make mistakes. Check the source records. Fictional data; not for maintenance decisions.</p></div>
  </ThreadPrimitive.Root>;
}

function UserMessage(){return <MessagePrimitive.Root className="user-message"><div className="user-bubble"><MessagePrimitive.Parts/></div></MessagePrimitive.Root>;}

function AssistantMessage(){
  const metadata=useAuiState(s=>s.message.metadata.custom.preflight) as AnswerMetadata|undefined;
  const status=useAuiState(s=>s.message.status);
  return <MessagePrimitive.Root className="assistant-message"><div className="assistant-avatar"><Plane size={17}/></div><div className="assistant-content"><div className="assistant-name">Preflight AI <span>Assistant</span></div>
    {metadata?.tools.length ? <details className="tool-activity"><summary><Database size={13}/>{metadata.tools.every(t=>t.status==='complete')?'Checked source records':'Reading records…'}<ChevronDown size={12}/></summary><div>{metadata.tools.map(tool=><p key={tool.call_id}>{tool.status==='complete'?<Check size={13}/>:<span className="loading-dot"/>}{tool.name.replaceAll('_',' ')}</p>)}</div></details>:status?.type==='running'?<div className="thinking" role="status"><span className="loading-dot"/>Looking into your question…</div>:null}
    <div className="markdown"><MessagePrimitive.Parts components={{Text:MarkdownText}}/></div>
    <MessagePrimitive.Error><div className="message-error" role="alert"><ErrorPrimitive.Message/></div></MessagePrimitive.Error>
    {metadata?.complete&&metadata.records.length>0&&<details className="source-records"><summary><ShieldCheck size={13}/>{metadata.records.length} source record{metadata.records.length===1?'':'s'}<ChevronDown size={12}/></summary><div>{metadata.records.map(id=><code key={id}>{id}</code>)}</div></details>}
    <ActionBarPrimitive.Root className="message-actions" hideWhenRunning><ActionBarPrimitive.Copy className="icon-button" aria-label="Copy answer"><Copy size={14}/></ActionBarPrimitive.Copy><ActionBarPrimitive.Reload className="icon-button" aria-label="Retry answer"><RotateCcw size={14}/></ActionBarPrimitive.Reload></ActionBarPrimitive.Root>
  </div></MessagePrimitive.Root>;
}

function MarkdownText(){return <MarkdownTextPrimitive remarkPlugins={remarkPlugins}/>;}
