// Exercise the filter component's event handlers with controlled hook state.
// This is component wiring coverage, not a browser/layout test.
let slots=[],cursor=0,effects=[],dirty=false;
export let thread={isRunning:false};
export function useState(initial) {
  const index=cursor++;
  if(!(index in slots)) slots[index]=typeof initial==='function'?initial():initial;
  return [slots[index],value=>{const next=typeof value==='function'?value(slots[index]):value;
    dirty ||= next!==slots[index];slots[index]=next;}];
}
export function useEffect(effect,deps) {
  const index=cursor++;
  if(!slots[index] || deps.some((value,i)=>value!==slots[index][i])) effects.push(effect);
  slots[index]=deps;
}
export const useAuiState=selector=>selector({thread});
export function reset(){slots=[];thread={isRunning:false};}
export function render(Component,props){
  let tree;
  do {cursor=0;effects=[];dirty=false;tree=Component(props);for(const effect of effects) effect();} while(dirty);
  return tree;
}
export function nodes(tree,predicate) {
  if(!tree || typeof tree!=='object') return [];
  if(Array.isArray(tree)) return tree.flatMap(child=>nodes(child,predicate));
  return [...(predicate(tree)?[tree]:[]),...nodes(tree.props?.children,predicate)];
}
