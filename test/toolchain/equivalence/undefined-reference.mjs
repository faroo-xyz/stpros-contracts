import {pathToFileURL} from 'node:url';
const {canonical}=await import(pathToFileURL(process.cwd()+'/tasks/release/core.mjs').href);
console.log(JSON.stringify([undefined,{x:undefined},[undefined,null],{x:null}].map(v=>{const c=canonical(v),w=JSON.stringify(v);return {undefined:c===undefined,text:c??null,wire:w??null};}),null,2));
