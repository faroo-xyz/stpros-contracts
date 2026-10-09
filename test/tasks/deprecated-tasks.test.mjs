import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import vm from "node:vm";
import test from "node:test";
import ts from "typescript";

// Exercise the exact inline action attached to each exported task, with an HRE
// that rejects any access (especially network creation, artifacts and signers).
const cases = [
  ["oracle.ts", "oracleSetPoolInfoTask", "oracle:set-pool-info"],
  ["stPros.ts", "stProsSetOracleTask", "stpros:set-oracle"],
  ["upgrade.ts", "oracleUpgradeV2Task", "oracle:upgrade-v2"],
  ["upgrade.ts", "oracleUpgradeV3Task", "oracle:upgrade-v3"],
  ["upgrade.ts", "stProsUpgradeV2Task", "stpros:upgrade-v2"],
];

for (const [file, exported, command] of cases) {
  test(`${command} rejects before accessing the runtime`, async () => {
    const code = readFileSync(new URL(`../../tasks/${file}`, import.meta.url), "utf8");
    const source = ts.createSourceFile(file, code, ts.ScriptTarget.ES2022, true);
    assert.equal(source.parseDiagnostics.length, 0, "task module syntax");
    const declaration = source.statements
      .filter(ts.isVariableStatement)
      .flatMap(s => [...s.declarationList.declarations])
      .find(d => d.name.getText(source) === exported);
    assert.ok(declaration, "task export must remain present");
    let call = declaration.initializer;
    let action;
    while (call && ts.isCallExpression(call) && ts.isPropertyAccessExpression(call.expression)) {
      if (call.expression.name.text === "setInlineAction") {
        action = call.arguments[0];
        break;
      }
      call = call.expression.expression;
    }
    assert.ok(action && ts.isArrowFunction(action), "registered inline action");
    const compiled = ts.transpileModule(`module.exports = ${action.getText(source)};`, {
      compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.CommonJS },
    });
    const sandbox = { module: { exports: undefined } };
    vm.runInNewContext(compiled.outputText, sandbox, { timeout: 1000 });
    let runtimeReads = 0;
    const forbiddenRuntime = new Proxy({}, { get() {
      runtimeReads++;
      throw new Error("task accessed the runtime before refusing");
    } });
    await assert.rejects(sandbox.module.exports({}, forbiddenRuntime), error => {
      assert.ok(error.message.startsWith(`Deprecated ${command}:`));
      return true;
    });
    assert.equal(runtimeReads, 0);
  });
}
