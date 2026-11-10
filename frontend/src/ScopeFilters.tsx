import {useEffect,useState} from 'react';
import {useAuiState} from '@assistant-ui/react';
import {ChevronDown,X} from 'lucide-react';
import options from './scope-options.json';
import {buildQuestion,filterFields,removeFilter,scopeChips,selectEntity,type Scope} from './scope.mjs';

type Props = {scope:Scope; onChange:(scope:Scope)=>void; onPrepare:(question:string)=>void};

export function ScopeFilters({scope,onChange,onPrepare}:Props){
  const [expanded,setExpanded]=useState(false);
  const isRunning=useAuiState(s=>s.thread.isRunning);
  useEffect(()=>{if(isRunning) setExpanded(false);},[isRunning]);
  const entity=scope.entity ?? 'issues';
  const chips=scopeChips(scope);
  return <section className="scope-filters" aria-label="Question filters">
    <div className="filter-bar">
      <button type="button" className="filter-toggle" aria-expanded={expanded} aria-controls="filter-panel"
        onClick={()=>setExpanded(!expanded)}>Filters<ChevronDown size={14} aria-hidden="true"/></button>
      <div className="entity-tabs" role="tablist" aria-label="Record type">
        {options.entity.enum.map((value,index)=><button key={value} type="button" role="tab"
          id={`filter-tab-${value}`} aria-selected={entity===value} aria-controls="filter-panel"
          tabIndex={entity===value ? 0 : -1}
          onClick={()=>{onChange(selectEntity(scope,value));setExpanded(true);}}
          onKeyDown={event=>{
            const next=event.key==='ArrowRight' ? (index+1)%options.entity.enum.length :
              event.key==='ArrowLeft' ? (index+options.entity.enum.length-1)%options.entity.enum.length :
              event.key==='Home' ? 0 : event.key==='End' ? options.entity.enum.length-1 : -1;
            if(next<0) return;
            event.preventDefault();
            onChange(selectEntity(scope,options.entity.enum[next]));
            setExpanded(true);
            document.getElementById(`filter-tab-${options.entity.enum[next]}`)?.focus();
          }}>{value[0].toUpperCase()+value.slice(1)}</button>)}
      </div>
      {chips.length>0&&<button type="button" className="clear-filters" onClick={()=>onChange({})}>Clear all</button>}
    </div>
    <div id="filter-panel" role="tabpanel" aria-labelledby={`filter-tab-${entity}`} hidden={!expanded}>
      <div className="filter-fields">
        {filterFields(entity).map(({key,label,type,choices})=><label key={key} className={`filter-field${key==='aircraft_id' ? ' filter-field-aircraft' : ''}`}>
          <span>{label}</span>
          {type==='date' ? <input type="date" value={scope[key] ?? ''}
            min={key==='opened_before' ? scope.opened_after : undefined}
            max={key==='opened_after' ? scope.opened_before : undefined}
            onChange={event=>onChange({...scope,entity,[key]:event.target.value})}/> :
            <select value={scope[key] ?? ''} onChange={event=>onChange({...scope,entity,[key]:event.target.value})}>
              <option value="">Any {label.toLowerCase()}</option>
              {choices.map(({value,label:optionLabel,accessibleLabel})=><option key={value} value={value} aria-label={accessibleLabel}>{optionLabel}</option>)}
            </select>}
        </label>)}
      </div>
      <button type="button" className="prepare-question" onClick={()=>{setExpanded(false);onPrepare(buildQuestion({...scope,entity}));}}>Prepare question</button>
      <span className="prepare-note">Edit it before sending.</span>
    </div>
    {chips.length>0&&<ul className="scope-chips active-scope" aria-label="Active filters">
      {chips.map(({key,label})=><li key={key}><button type="button" aria-label={`Remove ${label} filter`}
        onClick={()=>onChange(removeFilter(scope,key))}>{label}<X size={12} aria-hidden="true"/></button></li>)}
    </ul>}
  </section>;
}
