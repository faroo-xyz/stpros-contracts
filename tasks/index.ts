import * as deprecatedDeployModule from "./deprecatedDeploy.js";
import * as releaseTasksModule from "./release/index.js";
import * as exampleTasksModule from "./example.js";
import * as stProsTasksModule from "./stPros.js";
import * as yieldVaultTasksModule from "./yieldVault.js";
import * as yieldVaultFactoryTasksModule from "./yieldVaultFactory.js";
import * as oracleTasksModule from "./oracle.js";
import * as permissionsTasksModule from "./permissions.js";
import * as upgradeTasksModule from "./upgrade.js";
import * as verifySocialscanTasksModule from "./verifySocialscan.js";
import type { TaskDefinition } from "hardhat/types/tasks";

function isTaskDefinitionLike(value: unknown): value is TaskDefinition {
  return (
    typeof value === "object" &&
    value !== null &&
    "id" in value &&
    Array.isArray((value as { id?: unknown }).id) &&
    "type" in value &&
    typeof (value as { type?: unknown }).type === "string"
  );
}

function collectTasks(moduleExports: Record<string, unknown>): TaskDefinition[] {
  return Object.values(moduleExports).filter(isTaskDefinitionLike);
}

export * from "./example.js";
export * from "./stPros.js";
export * from "./yieldVault.js";
export * from "./yieldVaultFactory.js";
export * from "./permissions.js";
export * from "./oracle.js";
export * from "./upgrade.js";
export * from "./verifySocialscan.js";

export const appTasks = [
  ...collectTasks(deprecatedDeployModule),
  ...collectTasks(releaseTasksModule),
  ...collectTasks(exampleTasksModule),
  ...collectTasks(stProsTasksModule),
  ...collectTasks(yieldVaultTasksModule),
  ...collectTasks(yieldVaultFactoryTasksModule),
  ...collectTasks(permissionsTasksModule),
  ...collectTasks(oracleTasksModule),
  ...collectTasks(upgradeTasksModule),
  ...collectTasks(verifySocialscanTasksModule),
];
