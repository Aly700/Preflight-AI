import test from 'node:test';
import assert from 'node:assert/strict';
import options from '../src/scope-options.json' with {type:'json'};

const load = () => import('../src/scope.mjs');

test('task question names status and team and stays editable text', async () => {
  const {buildQuestion} = await load();
  assert.equal(buildQuestion({entity:'tasks',task_status:'awaiting_review',team_id:'TEAM-0001'}),
    'List the first 20 tasks awaiting review assigned to Demo Team 01.');
});

test('issue questions distinguish exact open and unresolved and include dates', async () => {
  const {buildQuestion} = await load();
  const scope = {entity:'issues',aircraft_id:'DEMO-AIR-0004',subsystem:'cabin',priority:'high',
    issue_status:'open',opened_after:'2026-01-01',opened_before:'2026-02-01'};
  const question = buildQuestion(scope);
  for (const phrase of ['DEMO-AIR-0004','cabin','high priority','status exactly open','2026-01-01','2026-02-01'])
    assert.ok(question.includes(phrase), phrase);
  assert.match(buildQuestion({...scope,issue_status:'unresolved'}), /unresolved \(any not closed\)/);
});

test('document question uses related aircraft and kind', async () => {
  const {buildQuestion,scopePayload} = await load();
  const scope = {entity:'documents',document_kind:'memo',aircraft_id:'DEMO-AIR-0004'};
  assert.match(buildQuestion(scope), /memo documents related to DEMO-AIR-0004/);
  assert.deepEqual(scopePayload(scope), {entity:'documents',document_kind:'memo',related_id:'DEMO-AIR-0004'});
});

test('URL round trip is deterministic for each entity and retains unrelated query fields', async () => {
  const {encodeScope,decodeScope} = await load();
  for (const scope of [
    {entity:'issues',aircraft_id:'DEMO-AIR-0004',issue_status:'unresolved',subsystem:'cabin',priority:'high',opened_after:'2026-01-01',opened_before:'2026-02-01'},
    {entity:'tasks',aircraft_id:'DEMO-AIR-0002',task_status:'awaiting_review',task_kind:'review',team_id:'TEAM-0001'},
    {entity:'documents',document_kind:'memo',aircraft_id:'DEMO-AIR-0003'},
  ]) {
    const query = encodeScope(scope, '?view=chat&status=obsolete');
    assert.deepEqual(decodeScope(query), scope);
    assert.equal(encodeScope(scope,query), query);
    assert.equal(new URLSearchParams(query).get('view'), 'chat');
  }
  assert.equal(encodeScope({entity:'issues',aircraft_id:'DEMO-AIR-0004',issue_status:'unresolved'}),
    '?entity=issues&aircraft=DEMO-AIR-0004&status=unresolved');
  assert.equal(encodeScope({}, '?entity=tasks&team=TEAM-0001&view=chat'), '?view=chat');
});

test('invalid, duplicate, wrong-entity and unknown URL values are silently dropped', async () => {
  const {decodeScope,scopePayload} = await load();
  assert.deepEqual(decodeScope('?entity=issues&aircraft=DEMO-AIR-9999&status=completed&team=TEAM-0001&priority=HIGH&from=2026-02-30&to=bad'), {entity:'issues'});
  assert.deepEqual(decodeScope('?entity=tasks&status=todo&status=completed&team=TEAM-9999&kind=repair'), {entity:'tasks'});
  assert.deepEqual(decodeScope('?entity=unknown&status=open&aircraft=DEMO-AIR-0001'), {});
  assert.deepEqual(decodeScope('?entity=issues&from=2026-02-02&to=2026-02-01'), {entity:'issues'});
  assert.deepEqual(scopePayload({entity:'tasks',issue_status:'open',priority:'high',task_status:'todo'}), {entity:'tasks',task_status:'todo'});
  assert.deepEqual(scopePayload({}), {});
});

test('every shipped choice round trips and IDs must be in the generated list', async () => {
  const {decodeScope,encodeScope} = await load();
  for (const [entity,field] of [['issues','subsystem'],['issues','priority'],['issues','issue_status'],
    ['tasks','task_status'],['tasks','task_kind'],['documents','document_kind']]) {
    for (const value of options[field].enum) {
      const scope = {entity,[field]:value};
      assert.deepEqual(decodeScope(encodeScope(scope)),scope);
    }
  }
  for (const {id} of options.aircraft) assert.equal(decodeScope(`?entity=issues&aircraft=${id}`).aircraft_id,id);
  for (const {id} of options.teams) assert.equal(decodeScope(`?entity=tasks&team=${id}`).team_id,id);
});

test('chip labels identify filters, support each removal and preserve the source', async () => {
  const {scopeChips,removeFilter,selectEntity} = await load();
  const scope = {entity:'tasks',task_status:'awaiting_review',team_id:'TEAM-0001',aircraft_id:'DEMO-AIR-0004'};
  const before = structuredClone(scope);
  const chips = scopeChips(scope);
  for (const label of ['Tasks','awaiting review','Demo Team 01','DEMO-AIR-0004']) assert.ok(chips.some(c=>c.label===label));
  for (const {key} of chips) assert.ok(!Object.hasOwn(removeFilter(scope,key),key));
  assert.deepEqual(removeFilter(scope,'entity'), {});
  assert.deepEqual(selectEntity(scope,'issues'), {entity:'issues',aircraft_id:'DEMO-AIR-0004'});
  assert.deepEqual(selectEntity(scope,'tasks'), scope);
  assert.deepEqual(scope,before);
  assert.ok(scopeChips({entity:'issues',issue_status:'open'}).some(c=>c.label==='Open (exact)'));
  assert.ok(scopeChips({entity:'issues',issue_status:'unresolved'}).some(c=>c.label==='Unresolved (any not closed)'));
  assert.ok(scopeChips({entity:'documents',related_id:'DEMO-AIR-0004'}).some(c=>c.label==='Related: DEMO-AIR-0004'));
});

test('mismatch warning finds exact foreign IDs and whole enum words case-insensitively', async () => {
  const {detectMismatches} = await load();
  const scope = {entity:'issues',aircraft_id:'DEMO-AIR-0004',subsystem:'cabin',issue_status:'open'};
  assert.equal(detectMismatches('Show demo-air-0005 avionics issues that are BLOCKED.',scope).length,3);
  assert.deepEqual(detectMismatches('Show DEMO-AIR-0004 cabin issues with status open.',scope),[]);
  assert.deepEqual(detectMismatches('The cabinetwork is reopenedness.',scope),[]);
  assert.equal(detectMismatches('TEAM-0002 awaiting review', {entity:'tasks',team_id:'TEAM-0001',task_status:'todo'}).length,2);
  assert.equal(detectMismatches('in_progress', {entity:'tasks',task_status:'todo'}).length,1);
  assert.deepEqual(detectMismatches('anything including closed avionics TEAM-0002',{}),[]);
});

test('unresolved permits narrower statuses and not-closed phrasing without self-warnings', async () => {
  const {detectMismatches,buildQuestion} = await load();
  const scope = {entity:'issues',issue_status:'unresolved'};
  assert.deepEqual(detectMismatches('Show open, blocked, reopened and in progress issues.',scope),[]);
  assert.deepEqual(detectMismatches(buildQuestion(scope),scope),[]);
  assert.equal(detectMismatches('Show closed issues.',scope).length,1);
  assert.equal(detectMismatches('Show unresolved issues.',{entity:'issues',issue_status:'open'}).length,1);
});

test('three starter questions stay inside every selected filter', async () => {
  const {starterQuestions,detectMismatches} = await load();
  for (const scope of [
    {entity:'issues',issue_status:'unresolved',aircraft_id:'DEMO-AIR-0003',subsystem:'controls',priority:'low'},
    {entity:'tasks',task_status:'awaiting_review',team_id:'TEAM-0001',task_kind:'review'},
    {entity:'documents',document_kind:'memo',aircraft_id:'DEMO-AIR-0002'},
  ]) {
    const starters = starterQuestions(scope);
    assert.equal(starters.length,3);
    assert.equal(new Set(starters.map(s=>s.question)).size,3);
    for (const starter of starters) assert.equal(starter.title,starter.question);
    for (const {question} of starters) assert.deepEqual(detectMismatches(question,scope),[]);
  }
});

test('aircraft and unresolved starters offer a list, priority breakdown and blocked/reopened lookup',async()=>{
  const {starterQuestions}=await load();
  assert.deepEqual(starterQuestions({entity:'issues',aircraft_id:'DEMO-AIR-0004',issue_status:'unresolved'}).map(s=>s.question),[
    'List the unresolved issues on DEMO-AIR-0004',
    'Break the unresolved issues on DEMO-AIR-0004 down by priority',
    'Which unresolved issues on DEMO-AIR-0004 are blocked or reopened?',
  ]);
});

test('specific starters include every selected filter and keep exact statuses within scope',async()=>{
  const {starterQuestions,detectMismatches}=await load();
  for(const [scope,phrases] of [
    [{entity:'issues',aircraft_id:'DEMO-AIR-0004',issue_status:'open',subsystem:'cabin',priority:'high',opened_after:'2026-01-01',opened_before:'2026-02-01'},
      ['DEMO-AIR-0004','status exactly open','cabin','high priority','2026-01-01','2026-02-01']],
    [{entity:'tasks',aircraft_id:'DEMO-AIR-0004',task_status:'awaiting_review',task_kind:'review',team_id:'TEAM-0001'},
      ['DEMO-AIR-0004','awaiting review','review tasks','Demo Team 01']],
    [{entity:'documents',aircraft_id:'DEMO-AIR-0004',document_kind:'memo'},['memo documents','DEMO-AIR-0004']],
  ]) {
    const starters=starterQuestions(scope);
    assert.equal(starters.length,3);
    assert.equal(new Set(starters.map(s=>s.question)).size,3);
    for(const {title,question} of starters) {
      assert.equal(title,question);
      for(const phrase of phrases) assert.ok(question.includes(phrase),`${phrase}: ${question}`);
      assert.deepEqual(detectMismatches(question,scope),[]);
    }
  }
  for(const issue_status of options.issue_status.enum)
    for(const {question} of starterQuestions({entity:'issues',issue_status}))
      assert.deepEqual(detectMismatches(question,{entity:'issues',issue_status}),[]);
});

test('no filters retains the three original welcome defaults',async()=>{
  const {starterQuestions}=await load();
  assert.deepEqual(starterQuestions({}).map(s=>s.title),[
    'Get the aircraft picture','Find unresolved issues','Follow an issue’s history',
  ]);
});

test('starter cards read naturally: distinct details and no doubled phrasing',async()=>{
  const {starterQuestions}=await load();
  const tasks=starterQuestions({entity:'tasks',task_status:'awaiting_review',team_id:'TEAM-0001'});
  assert.deepEqual(tasks.map(s=>s.question),[
    'List the awaiting review tasks for Demo Team 01',
    'Which issues are linked to the awaiting review tasks for Demo Team 01?',
    'Who is assigned to the awaiting review tasks for Demo Team 01?',
  ]);
  for(const scope of [{entity:'issues',aircraft_id:'DEMO-AIR-0004'},{entity:'tasks',team_id:'TEAM-0001'},{entity:'documents',document_kind:'memo'}]) {
    const starters=starterQuestions(scope);
    assert.equal(new Set(starters.map(s=>s.detail)).size,3,JSON.stringify(starters));
    for(const {question} of starters) assert.ok((question.match(/\bassigned\b/g) ?? []).length<=1,question);
  }
});

test('a date window reads as one phrase in starters',async()=>{
  const {starterQuestions}=await load();
  const [first]=starterQuestions({entity:'issues',issue_status:'unresolved',opened_after:'2026-03-01',opened_before:'2026-09-30'});
  assert.equal(first.question,'List the unresolved issues opened between 2026-03-01 and 2026-09-30 (inclusive)');
  const [after]=starterQuestions({entity:'issues',opened_after:'2026-03-01'});
  assert.equal(after.question,'List the issues opened on or after 2026-03-01');
});

test('a prepared question states a date window once',async()=>{
  const {buildQuestion}=await load();
  assert.equal(buildQuestion({entity:'issues',aircraft_id:'DEMO-AIR-0004',opened_after:'2026-03-01',opened_before:'2026-09-30'}),
    'List the first 20 issues for DEMO-AIR-0004 opened between 2026-03-01 and 2026-09-30 (inclusive).');
});
