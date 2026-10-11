// Test-only exposure of the exact committed private pinned() function.
import fs from 'node:fs';import vm from 'node:vm';import{pathToFileURL}from'node:url';import{createHash}from'node:crypto';
const root=process.cwd(),source=fs.readFileSync(root+'/tasks/release/read-only.mjs','utf8');const code=source.slice(source.indexOf('async function pinned('),source.indexOf('async function logs('));
const {requireThat}=await import(pathToFileURL(root+'/tasks/release/core.mjs').href);const pinned=vm.runInNewContext('('+code.trim()+')',{requireThat,hex:n=>`0x${BigInt(n).toString(16)}`});
const cases=JSON.parse(fs.readFileSync(process.argv[2]));const results=[];
for(const c of cases){const trace=[];const rpc=async(method,params)=>{trace.push({method,params});return method==='eth_chainId'?c.observedChain:c.header;};try{const value=await pinned({chainId:c.chainId},rpc,c.tag);results.push({id:c.id,outcome:'return',value,outputBytes:Buffer.from(JSON.stringify(value)).toString('hex'),trace});}catch(e){results.push({id:c.id,outcome:'throw',error:{name:e.name,message:e.message},trace});}}
console.log(JSON.stringify({sourceSha256:createHash('sha256').update(source).digest('hex'),functionSha256:createHash('sha256').update(code).digest('hex'),results},null,2));
