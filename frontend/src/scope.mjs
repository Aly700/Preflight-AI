import options from './scope-options.json' with {type:'json'};

/** @typedef {Record<string, string>} Scope */
/** @type {Record<string, string[]>} */
const fields = {
  issues: ['aircraft_id','subsystem','priority','issue_status','opened_after','opened_before'],
  tasks: ['aircraft_id','task_status','task_kind','team_id'],
  documents: ['document_kind','aircraft_id'],
};
/** @type {Record<string, string>} */
const queryKeys = {entity:'entity',aircraft_id:'aircraft',subsystem:'subsystem',priority:'priority',
  issue_status:'status',task_status:'status',task_kind:'kind',document_kind:'kind',team_id:'team',
  opened_after:'from',opened_before:'to'};
/** @type {Record<string, string>} */
const labels = {aircraft_id:'Aircraft',subsystem:'Subsystem',priority:'Priority',issue_status:'Issue status',
  opened_after:'Opened from',opened_before:'Opened to',task_status:'Task status',task_kind:'Task kind',
  team_id:'Team',document_kind:'Document kind'};
const words = value => value.replaceAll('_',' ');
const title = value => value[0].toUpperCase() + value.slice(1);
const teamName = id => options.teams.find(team=>team.id===id)?.name ?? id;
const statusLabel = value => value==='open' ? 'Open (exact)' : value==='unresolved' ? 'Unresolved (any not closed)' : words(value);

function validDate(value) {
  if (!/^\d{4}-\d{2}-\d{2}$/.test(value) || value.startsWith('0000')) return false;
  const date = new Date(`${value}T00:00:00Z`);
  return Number.isFinite(date.getTime()) && date.toISOString().slice(0,10)===value;
}

function validValue(key,value) {
  if (typeof value!=='string') return false;
  if (key==='aircraft_id') return options.aircraft.some(item=>item.id===value);
  if (key==='team_id') return options.teams.some(item=>item.id===value);
  if (key.startsWith('opened_')) return validDate(value);
  return options[key]?.enum?.includes(value) ?? false;
}

/** Keep only known, applicable UI filters. An empty object means no selected scope. @param {Scope} scope */
export function normalizeScope(scope) {
  if (!options.entity.enum.includes(scope.entity)) return {};
  /** @type {Scope} */
  const clean = {entity:scope.entity};
  for (const key of fields[scope.entity]) if (validValue(key,scope[key])) clean[key]=scope[key];
  if (clean.opened_after && clean.opened_before && clean.opened_after>clean.opened_before) {
    delete clean.opened_after;
    delete clean.opened_before;
  }
  return clean;
}

/** @param {Scope} scope */
export function scopePayload(scope) {
  const clean = normalizeScope(scope);
  if (clean.entity==='documents' && clean.aircraft_id) {
    clean.related_id=clean.aircraft_id;
    delete clean.aircraft_id;
  }
  return clean;
}

/** @param {Scope} scope @param {string} entity */
export function selectEntity(scope,entity) {
  if (scope.entity===entity) return normalizeScope(scope);
  return normalizeScope({entity,...(scope.aircraft_id ? {aircraft_id:scope.aircraft_id} : {})});
}

/** @param {Scope} scope @param {string} key */
export function removeFilter(scope,key) {
  if (key==='entity') return {};
  const clean = {...scope};
  delete clean[key];
  return clean;
}

/** @param {string} query */
export function decodeScope(query) {
  const params = new URLSearchParams(query);
  const entity = params.getAll('entity');
  if (entity.length!==1 || !options.entity.enum.includes(entity[0])) return {};
  /** @type {Scope} */
  const scope = {entity:entity[0]};
  for (const key of fields[scope.entity]) {
    const values = params.getAll(queryKeys[key]);
    if (values.length===1) scope[key]=values[0];
  }
  return normalizeScope(scope);
}

/** Preserve unrelated URL parameters and emit filter keys in a stable order. @param {Scope} scope */
export function encodeScope(scope,query='') {
  const params = new URLSearchParams(query);
  for (const key of Object.values(queryKeys)) params.delete(key);
  const clean = normalizeScope(scope);
  for (const [key,value] of Object.entries(clean)) params.set(queryKeys[key],value);
  const encoded=params.toString();
  return encoded ? `?${encoded}` : '';
}

/** A display-only projection also accepts the server's related_id document scope. @param {Scope} scope */
export function scopeChips(scope) {
  return Object.entries(scope).filter(([key,value])=>typeof value==='string' && (key==='related_id' || key==='entity' || key in labels))
    .map(([key,value])=>({key,label:
      key==='entity' ? title(value) : key==='team_id' ? teamName(value) :
      key==='issue_status' ? statusLabel(value) : key==='priority' ? `${value} priority` :
      key==='opened_after' ? `From ${value}` : key==='opened_before' ? `To ${value}` :
      key==='related_id' || (key==='aircraft_id' && scope.entity==='documents') ? `Related: ${value}` : words(value)}));
}

/** Rendering descriptors: all select values come from the generated JSON.
 * @returns {{key:string,label:string,type:string,choices:{value:string,label:string,accessibleLabel?:string}[]}[]}
 */
export function filterFields(entity='issues') {
  return (fields[entity] ?? []).map(key=>({key,
    label:key==='aircraft_id' && entity==='documents' ? 'Related aircraft' : labels[key],
    type:key.startsWith('opened_') ? 'date' : 'select',
    choices:key==='aircraft_id' ? options.aircraft.map(item=>({value:item.id,label:`${item.id} (${item.program.replace(/^DEMO-PROGRAM-/,'')} · ${item.station.replace(/^DEMO-STATION-/,'Station ')})`,accessibleLabel:`${item.id} · ${item.program} · ${item.station}`})) :
      key==='team_id' ? options.teams.map(item=>({value:item.id,label:item.name})) :
      (options[key]?.enum ?? []).map(value=>({value,label:key==='issue_status' ? statusLabel(value) : words(value)})),
  }));
}

/** @param {Scope} scope */
/** @param {Scope} s */
const dateWindow = s => s.opened_after && s.opened_before ? ` opened between ${s.opened_after} and ${s.opened_before} (inclusive)` :
  `${s.opened_after ? ` opened on or after ${s.opened_after}` : ''}${s.opened_before ? ` opened on or before ${s.opened_before}` : ''}`;

function subject(scope) {
  const s=normalizeScope(scope);
  if (s.entity==='documents') return `${s.document_kind ? `${s.document_kind} ` : ''}documents${s.aircraft_id ? ` related to ${s.aircraft_id}` : ''}`;
  if (s.entity==='tasks') return `tasks${s.task_status ? ` ${words(s.task_status)}` : ''}${s.task_kind ? ` of kind ${s.task_kind}` : ''}${s.team_id ? ` assigned to ${teamName(s.team_id)}` : ''}${s.aircraft_id ? ` for ${s.aircraft_id}` : ''}`;
  return `issues${s.issue_status ? (s.issue_status==='unresolved' ? ' that are unresolved (any not closed)' : ` with status exactly ${words(s.issue_status)}`) : ''}${s.aircraft_id ? ` for ${s.aircraft_id}` : ''}${s.subsystem ? ` in ${s.subsystem}` : ''}${s.priority ? ` with ${s.priority} priority` : ''}${dateWindow(s)}`;
}

/** Returns text only: the caller puts it in the editable composer. @param {Scope} scope */
export function buildQuestion(scope) {
  return scope.entity==='documents' ? `Find ${subject(scope)}.` : `List the first 20 ${subject(scope)}.`;
}

/** @param {Scope} scope */
export function starterQuestions(scope) {
  if (!scope.entity) return [
    {title:'Get the aircraft picture',detail:'Issues, progress, and work remaining',question:'Summarize DEMO-AIR-0001, including issue and task counts.'},
    {title:'Find unresolved issues',detail:'See what still needs attention',question:'List the first five unresolved issues and the total unresolved count.'},
    {title:'Follow an issue’s history',detail:'Understand how a record changed',question:'Show the activity history for ISSUE-0001.'},
  ];
  const s=normalizeScope(scope);
  let questions;
  let details;
  if(s.entity==='documents') {
    const target=subject(s);
    questions=[`Find ${target}`,`Summarize the decisions recorded in the ${target}`,
      `Which issues are referenced in the ${target}?`];
    details=['Matching passages with citations','What the documents decided','Issues the documents mention'];
  } else if(s.entity==='tasks') {
    const target=`${s.task_status ? `${words(s.task_status)} ` : ''}${s.task_kind ? `${s.task_kind} ` : ''}tasks`+
      `${s.aircraft_id ? ` on ${s.aircraft_id}` : ''}${s.team_id ? ` for ${teamName(s.team_id)}` : ''}`;
    questions=[`List the ${target}`,`Which issues are linked to the ${target}?`,`Who is assigned to the ${target}?`];
    details=['Matching tasks with sources','The issues behind them','People and teams on the work'];
  } else {
    const target=`${s.issue_status==='unresolved' ? 'unresolved issues' :
      `issues${s.issue_status ? ` with status exactly ${words(s.issue_status)}` : ''}`}`+
      `${s.aircraft_id ? ` on ${s.aircraft_id}` : ''}${s.subsystem ? ` in ${s.subsystem}` : ''}`+
      `${s.priority ? ` with ${s.priority} priority` : ''}`+
      dateWindow(s);
    const group=!s.priority ? 'priority' : !s.subsystem ? 'subsystem' : 'status';
    questions=[`List the ${target}`,`Break the ${target} down by ${group}`,
      !s.issue_status || s.issue_status==='unresolved' ? `Which ${target} are blocked or reopened?` :
        `Show the activity history for the first of the ${target}`];
    details=['Matching issues with sources',`Counts by ${group}`,
      !s.issue_status || s.issue_status==='unresolved' ? 'What is stuck or came back' : 'How a record changed'];
  }
  return questions.map((question,index)=>({title:question,detail:details[index],question}));
}

const mentions = (text,value) => new RegExp(`\\b${value.split('_').join('[ _-]+')}\\b`,'i').test(text);

/** Heuristic warnings only; the backend remains authoritative. @param {string} question @param {Scope} scope */
export function detectMismatches(question,scope) {
  const s=normalizeScope(scope);
  const warnings=[];
  for (const [key,pattern,label] of [
    ['aircraft_id',/\bDEMO-AIR-\d+\b/gi,'aircraft'],['team_id',/\bTEAM-\d+\b/gi,'team'],
  ]) {
    const selected=s[String(key)];
    if (selected && [...question.matchAll(/** @type {RegExp} */ (pattern))].some(match=>match[0].toUpperCase()!==selected))
      warnings.push(`The question names a different ${label} from ${selected}.`);
  }
  if (s.subsystem && options.subsystem.enum.some(value=>value!==s.subsystem && mentions(question,value)))
    warnings.push(`The question names a subsystem outside ${s.subsystem}.`);
  const statusKey=s.entity==='tasks' ? 'task_status' : 'issue_status';
  const status=s[statusKey];
  // "Any not closed" describes unresolved; it is not a request for closed records.
  const text=question.replace(/\b(?:any\s+)?not[ _-]+closed\b/gi,'unresolved');
  if (status && options[statusKey].enum.some(value=>
    (status==='unresolved' ? value==='closed' : value!==status) && mentions(text,value)))
    warnings.push(`The question names a status outside ${statusLabel(status)}.`);
  return warnings;
}
