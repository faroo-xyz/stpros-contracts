// Supplemental independent original-engine octet / text-domain observations.
import fs from 'node:fs';import{createRequire}from'node:module';import{pathToFileURL}from'node:url';
const root=process.cwd(),require=createRequire(root+'/package.json'),v=require('viem');
const core=await import(pathToFileURL(root+'/tasks/release/core.mjs'));
const revive=x=>x&&typeof x==='object'&&!Array.isArray(x)&&Object.keys(x).length===1&&'$bigint'in x?BigInt(x.$bigint):Array.isArray(x)?x.map(revive):x&&typeof x==='object'?Object.fromEntries(Object.entries(x).map(([k,w])=>[k,revive(w)])):x;
const pack=x=>typeof x==='bigint'?{$bigint:String(x)}:Array.isArray(x)?x.map(pack):x&&typeof x==='object'?Object.fromEntries(Object.entries(x).map(([k,w])=>[k,pack(w)])):x;
const rows=[];
for(const c of JSON.parse(fs.readFileSync(process.argv[2]))) {
 const params=c.op==='function'?c.abi.inputs:c.params,values=revive(c.values);
 const encoded=c.op==='function'?v.encodeFunctionData({abi:[c.abi],functionName:c.abi.name,args:values}):v.encodeAbiParameters(params,values);
 const body=c.op==='function'?'0x'+encoded.slice(10):encoded;
 const row={id:c.id,encoded,decoded:pack(v.decodeAbiParameters(params,body)),keccak:v.keccak256(encoded),rawTextSha256:core.sha256(encoded)};
 if(c.op==='function') {
  const safe=core.safeBuilder({chainId:31337,mode:'simulation'},[{to:'0x'+'12'.repeat(20),value:'0',data:encoded}],'Hex spelling witness','0x'+'34'.repeat(20));
  row.safeJson=JSON.stringify(safe);row.safeCanonicalSha256=core.sha256(core.canonical(safe));
 }
 rows.push(row);
}
process.stdout.write(JSON.stringify({origin:'Unmodified original viem and core.safeBuilder; synthetic unsigned observations only',viem:JSON.parse(fs.readFileSync(root+'/node_modules/viem/package.json')).version,results:rows},null,2)+'\n');
