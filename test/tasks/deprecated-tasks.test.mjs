import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import vm from "node:vm";
import test from "node:test";
import ts from "typescript";
import * as config from "hardhat/config";
import * as viem from "viem";
import { execFileSync, spawnSync } from "node:child_process";

// Evaluate the actual complete TS registry with real Hardhat builders. Only the
// HRE passed to an action is mocked; no config, wallet or RPC is initialized.
const cache = new Map();
const messages = [];
async function load(url) {
  if (cache.has(url.href)) return cache.get(url.href);
  const source = readFileSync(url, "utf8");
  const parsed = ts.createSourceFile(url.pathname, source, ts.ScriptTarget.ES2022, true);
  assert.equal(parsed.parseDiagnostics.length, 0);
  const imports = new Map();
  for (const s of parsed.statements) {
    if (!ts.isImportDeclaration(s) && !ts.isExportDeclaration(s)) continue;
    if (!s.moduleSpecifier || s.importClause?.isTypeOnly || s.isTypeOnly) continue;
    const name = s.moduleSpecifier.text;
    if (name === "hardhat/config") imports.set(name, config);
    else if (name.startsWith(".")) {
      const child = new URL(name.replace(/\.js$/, ".ts"), url);
      imports.set(name, child.pathname.endsWith(".ts") ? await load(child) : await import(child.href));
    } else imports.set(name, await import(name));
  }
  const compiled = ts.transpileModule(source, { compilerOptions: {
    target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.CommonJS,
  }}).outputText;
  const exports = {};
  vm.runInNewContext(compiled, { exports, require(name) {
    assert.ok(imports.has(name), `unresolved import ${name}`); return imports.get(name);
  }, console: { log: (...args) => messages.push(args.join(" ")), error() {} },
  setTimeout, Buffer, URL, process }, { timeout: 2000 });
  cache.set(url.href, exports);
  return exports;
}
const { appTasks } = await load(new URL("../../tasks/index.ts", import.meta.url));
const tasks = new Map(appTasks.map(t => [t.id.join(":"), t]));
const removed = ["oracle:upgrade", "oracle:upgrade-v2", "oracle:upgrade-v3", "stpros:upgrade", "stpros:upgrade-v2",
  "perms:transfer-owner", "perms:transfer-proxy-admin", "perms:migrate-owners", "yv:set-phase", "yv:grant-role",
  "yv:revoke-role", "yvf:add-counterparty", "yvf:remove-counterparty", "yvf:emergency-cancel", "yvf:transfer-owner",
  "yvf:upgrade-beacon", "stpros:set-oracle", "stpros:set-unbonding-period", "oracle:set-pool-info", "example"];
const retained = ["deploy", "oracle:pool-info", "perms:check", "stpros:preview-deposit", "stpros:preview-withdraw",
  "stpros:deposit-with-pros", "yv:info", "yv:account", "yv:preview-deposit", "yv:preview-withdraw", "yv:approve",
  "yv:deposit", "yv:open-next-round", "yield-vault:loss-calldata", "yvf:list-proxies", "yvf:create", "yvf:retire",
  "yvf:close-creation", "verify:socialscan", "release:preflight", "release:p1-runbook", "release:build-handoff",
  "release:build-u", "release:estimate-gas", "release:testnet-handoff", "release:postcheck", "release:authority",
  "yield-vault:loss-contingency"];

test("complete app registry has only supported commands and no privileged aliases", () => {
  assert.equal(tasks.size, appTasks.length, "no duplicate registrations");
  assert.deepEqual([...tasks.keys()].sort(), retained.sort());
  for (const name of removed) assert.ok(!tasks.has(name), name);
});
test("actual CLI help registers supported tasks and excludes deleted names", () => {
  const output = execFileSync(process.execPath, ["node_modules/hardhat/dist/src/cli.js", "--help"], {encoding:"utf8"});
  for (const name of removed) assert.ok(!new RegExp(`^\\s+${name}\\s`, "m").test(output), name);
  for (const name of retained) assert.match(output, new RegExp(`^\\s+${name}\\s`, "m"));
});
test("deploy remains an override and refuses before any runtime access", async () => {
  assert.equal(tasks.get("deploy").type, "TASK_OVERRIDE");
  let reads = 0;
  const forbidden = new Proxy({}, {get() {reads++; throw new Error("runtime accessed");}});
  await assert.rejects(tasks.get("deploy").inlineAction({}, forbidden), /Deprecated deploy:/);
  assert.equal(reads, 0);
  const result = spawnSync(process.execPath, ["node_modules/hardhat/dist/src/cli.js", "deploy"], {encoding:"utf8"});
  assert.notEqual(result.status, 0);
  assert.match(result.stdout + result.stderr, /Deprecated deploy:/);
});
const address = n => "0x" + n.toString(16).padStart(40, "0");
const forbidden = label => new Proxy({}, {get() {throw new Error(`forbidden ${label}`);}});
const abiFile = (name) => JSON.parse(readFileSync(new URL(`../../cache/native-r4/${name}/artifact.json`, import.meta.url))).abi;
const hre = {artifacts:{readArtifact:async name=>({abi:abiFile(name)})}, network: forbidden("network")};
async function unsigned(name, args, runtime = hre) {
  messages.length = 0;
  await tasks.get(name).inlineAction(args, runtime);
  const result = JSON.parse(messages.at(-1));
  assert.equal(result.value, "0");
  return result;
}
test("unsigned retirement/closure and legacy LOSS encode exact supported ABI", async () => {
  for (const [name, args, contract, fn, expected] of [
    ["yvf:retire", {factory:address(1),vault:address(2)}, "YieldVaultFactory", "retireVault", [address(2)]],
    ["yvf:close-creation", {factory:address(1)}, "YieldVaultFactory", "closeCreation", []],
    ["yield-vault:loss-calldata", {vault:address(1),amount:"50"}, "YieldVault", "proposeSettlement", [1,50n,address(0)]],
  ]) {
    const result = await unsigned(name,args);
    assert.equal(result.target,address(1));
    const decoded=viem.decodeFunctionData({abi:abiFile(contract),data:result.data});
    assert.equal(decoded.functionName, fn); assert.deepEqual(decoded.args??[],expected);
  }
});
test("yvf:create preserves canonical Timelock admin and refuses wrong admin, without wallet access", async () => {
  let closed = 0;
  const runtime = {...hre,network:{getOrCreate:async()=>({close:async()=>closed++,viem:{
    getWalletClients:()=>{throw new Error("wallet accessed");},
    getContractAt:async()=>({read:{timelock:async()=>address(2)}}),
  }})}};
  const args={factory:address(1),asset:address(3),operator:address(4),counterparty:address(5),feeRecipient:address(6),
    name:"Vault",symbol:"V",subscriptionWindow:"86400",lockDuration:"86400",settleTimelockWindow:"604800",
    epochCap:"100",perAddressCap:"100",minSubscription:"1",performanceFeeBps:"0",maxLossBps:"5000"};
  const result=await unsigned("yvf:create",args,runtime);
  const decoded=viem.decodeFunctionData({abi:abiFile("YieldVaultFactory"),data:result.data});
  assert.equal(decoded.functionName,"createYieldVault"); assert.equal(decoded.args[0].admin,address(2));
  await assert.rejects(tasks.get("yvf:create").inlineAction({...args,admin:address(7)},runtime),/admin must equal/);
  assert.equal(closed,2);
});
test("read-only Oracle handler still reads and closes without signers", async () => {
  let closed=0; const calls=[];
  const runtime={network:{getOrCreate:async()=>({networkName:"testnet",close:async()=>closed++,viem:{
    getContractAt:async()=>({read:{owner:async()=>address(1),paused:async()=>false,poolInfo:async args=>{calls.push(args);return [100n,200n];}}}),
    getWalletClients:()=>{throw new Error("wallet accessed");},
  }})}};
  messages.length=0;
  await tasks.get("oracle:pool-info").inlineAction({token:address(3),oracle:address(4)},runtime);
  assert.equal(closed,1); assert.equal(calls[0][0],address(3));
  assert.ok(messages.some(m=>m.endsWith("tokenAmount=100")));
});
test("native build-info feeds the unchanged release normalizer and immutable verifier", async () => {
  const {freezeArtifact} = await import("../../tasks/release/artifacts.mjs");
  const {verifyRuntime} = await import("../../tasks/release/core.mjs");
  const info = JSON.parse(readFileSync(new URL("../../cache/native-r4/YieldVaultFactory/build-info.json", import.meta.url)));
  const {artifact,reviewStatus} = freezeArtifact(info,info,"project/contracts/YieldVaultFactory.sol","YieldVaultFactory");
  assert.match(reviewStatus,/UNREVIEWED/);
  let runtime=artifact.deployedBytecode.slice(2);
  for(const slot of Object.values(artifact.immutableReferences).flat()) {
    assert.equal(slot.length,32);
    runtime=runtime.slice(0,slot.start*2)+address(9).slice(2).padStart(64,"0")+runtime.slice((slot.start+32)*2);
  }
  assert.equal(verifyRuntime("0x"+runtime,artifact,address(9)),viem.keccak256("0x"+runtime));
  assert.throws(()=>verifyRuntime("0x"+runtime,artifact,address(10)),/RUNTIME/);
});
