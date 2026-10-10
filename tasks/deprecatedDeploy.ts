import { overrideTask } from "hardhat/config";

// The plugin's deploy action creates a network connection before loading files.
// Refuse here as well as in each preserved script, before that action is entered.
export const deprecatedDeployTask = overrideTask("deploy")
  .setInlineAction(async () => {
    throw new Error("Deprecated deploy: legacy deployment is incompatible with R4. Use the reviewed release process; no wallet, network or deployment records are accessed by this action.");
  })
  .build();
