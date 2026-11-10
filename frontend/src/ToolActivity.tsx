import {Check,ChevronDown,CircleAlert,Database,ScanSearch} from 'lucide-react';
import type {ToolActivity as Activity} from './adapter';

export function ToolActivity({tools,complete}:{tools:Activity[];complete:boolean}){
  const outside=tools.filter(tool=>tool.status==='refused' && tool.reason==='scope').length;
  const failed=tools.filter(tool=>tool.status==='failed' || (tool.status==='refused' && tool.reason!=='scope')).length;
  const label=complete ? 'Checked source records'+
    (outside ? ` · ${outside} lookup${outside===1?'':'s'} outside the selected scope` : '')+
    (failed ? ` · ${failed} lookup${failed===1?'':'s'} failed` : '') : 'Reading records…';
  return <details className="tool-activity"><summary><Database size={13} aria-hidden="true"/>{label}<ChevronDown size={12} aria-hidden="true"/></summary>
    <div>{tools.map(tool=><p key={tool.call_id}>
      {tool.status==='complete' ? <Check size={13} aria-label="Complete"/> :
        tool.status==='refused' && tool.reason==='scope' ? <ScanSearch size={13} aria-label="Outside the selected scope"/> :
        tool.status==='failed' || tool.status==='refused' || complete ? <CircleAlert size={13} aria-label="Failed"/> :
        <span className="loading-dot" aria-label="Running"/>}
      {tool.name.replaceAll('_',' ')}
    </p>)}</div>
  </details>;
}
