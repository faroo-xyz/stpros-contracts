import{pathToFileURL}from'node:url';import{capture,context}from'./capture-runtime.mjs';
const root=process.cwd(),core=await import(pathToFileURL(root+'/tasks/release/core.mjs')),{fixture,reapprove,address}=await import(pathToFileURL(root+'/test/release/fixture.mjs'));
for(const [name,chain,vaultIndex]of [['buildHandoff',1672,0],['p1Runbook',1672,1],['testnetHandoff',688689,0]]){
 const f=fixture(chain),vault=f.c.factories[vaultIndex].vaults[0],r=f.inventory.roles[vault.address];
 for(const [role,account]of [[core.ROLES.operator,address(901)],['0x'+'ab'.repeat(32),address(902)]]){const key=role+':'+account;r.events.push({event:'RoleGranted',role,account,block:2,logIndex:r.events.length,transactionHash:core.ZERO32});r.current.push(key);r.ever.push(key);}reapprove(f);
 await context.run({test:'rich inventoried roles / revoke every role / admin last / '+name},async()=>capture('core.mjs.'+name,core[name],[f.c,f.inventory,f.artifacts]));
}
