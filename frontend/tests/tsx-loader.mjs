// Compile local TSX with the already-installed build tool; no server or new dependency.
import {registerHooks} from 'node:module';
import {readFileSync} from 'node:fs';
import {transformSync} from 'rolldown/experimental';
registerHooks({
  load(url,context,nextLoad) {
    if (url.endsWith('/scope-options.json')) return nextLoad(url,{...context,importAttributes:{type:'json'}});
    if (!url.endsWith('.tsx')) return nextLoad(url,context);
    const {code,errors}=transformSync(url,readFileSync(new URL(url),'utf8'),{jsx:{runtime:'automatic'}});
    if(errors.length) throw new Error(errors.map(e=>e.message).join('\n'));
    return {format:'module',source:code,shortCircuit:true};
  },
});
