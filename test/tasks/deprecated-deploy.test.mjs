import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import vm from "node:vm";
import test from "node:test";
import ts from "typescript";

const entries = {
  "Oracle.ts": ["Oracle", "Oracle_deploy"],
  "stPROS.ts": ["StPROS", "StPROS_deploy"],
  "BridgeVault.ts": ["BridgeVault", "BridgeVault_deploy"],
  "SlpDistributor.ts": ["SlpDistributor", "SlpDistributor_deploy"],
  "YieldVaultFactory.ts": ["YieldVaultFactory", "YieldVaultFactory_deploy"],
  "OracleImplementation.ts": ["implementations", "Oracle_impl"],
  "StPROSImplementation.ts": ["implementations", "StPROS_impl"],
  "YieldVaultImplementation.ts": ["implementations", "YieldVault_impl"],
  "YieldVaultFactoryImplementation.ts": ["implementations", "YieldVaultFactory_impl"],
};
for (const [file, tags] of Object.entries(entries)) {
  test(`${file} preserves tags and refuses with no imports or runtime access`, async () => {
    const code = readFileSync(new URL(`../../deploy/${file}`, import.meta.url), "utf8");
    const source = ts.createSourceFile(file, code, ts.ScriptTarget.ES2022, true);
    assert.equal(source.parseDiagnostics.length, 0);
    assert.ok(!source.statements.some(ts.isImportDeclaration));
    const compiled = ts.transpileModule(code, { compilerOptions: { module: ts.ModuleKind.CommonJS } });
    const sandbox = { exports: {} };
    vm.runInNewContext(compiled.outputText, sandbox, { timeout: 1000 });
    const entry = sandbox.exports.default;
    assert.deepEqual(Array.from(entry.tags), tags);
    let reads = 0;
    const forbidden = new Proxy({}, {
      get() { reads++; throw new Error("runtime accessed"); },
      ownKeys() { reads++; throw new Error("runtime enumerated"); },
      getPrototypeOf() { reads++; throw new Error("runtime prototype accessed"); },
    });
    await assert.rejects(entry(forbidden, forbidden), new RegExp(`Deprecated deploy/${file}:`));
    assert.equal(reads, 0);
  });
}
test("obsolete CounterV2 package alias refuses without Hardhat or a network", () => {
  const p = JSON.parse(readFileSync(new URL("../../package.json", import.meta.url)));
  const command = p.scripts["upgrade:testnet"];
  assert.ok(command.startsWith('node -e "throw new Error('));
  assert.ok(!command.includes("--network") && !command.includes("hardhat"));
});
