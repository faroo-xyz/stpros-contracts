import { requireActionBearingCoverage } from "./timelock-history.mjs";
import { task } from "hardhat/config";
import { checkedJson, validateRecordedTopology, preflight, buildU, buildHandoff, p1Runbook, testnetHandoff, expectations, deploymentDefinitions } from "./core.mjs";
import { readOnlyRpc, recheckObserved, verifyCheckpoint, estimateGas } from "./read-only.mjs";

// All commands either read or print unsigned artifacts. No wallet client is acquired.
function command(name: string, description: string) {
  return task(name, description)
    .addOption({name: "releaseConfig", description: "Reviewed release configuration JSON (required)", defaultValue: ""})
    .addOption({name: "reviewHash", description: "Independently approved SHA-256 of the configuration file", defaultValue: ""})
    .addOption({name: "rpc", description: "Explicit public read-only endpoint or simulation localhost", defaultValue: ""})
    .addOption({name: "checkpoint", description: "pre or execution (reuse the frozen pre-H bundle)", defaultValue: "pre"})
    .addOption({name: "checkpointReview", description: "Fresh independently reviewed execution evidence JSON", defaultValue: ""})
    .addOption({name: "checkpointReviewHash", description: "Independent raw SHA-256 of execution evidence", defaultValue: ""})
    .setInlineAction(async (args: {releaseConfig: string; reviewHash: string; rpc: string; checkpoint: string; checkpointReview: string; checkpointReviewHash: string}) => {
      if (!args.releaseConfig || !/^[a-f0-9]{64}$/i.test(args.reviewHash)) throw new Error("INCOMPLETE: config and independent review hash required");
      const c = checkedJson(args.releaseConfig, args.reviewHash);
      validateRecordedTopology(c, checkedJson("tools/release/registry.json"));
      const inventory = checkedJson(c.inventoryFile);
      const artifacts: Record<string, any> = {};
      for (const [key, path] of Object.entries(c.artifactFiles as Record<string,string>)) artifacts[key] = checkedJson(path);
      const checked = preflight(c, inventory, artifacts);
      if(!["pre","execution"].includes(args.checkpoint)) throw new Error("Invalid checkpoint");
      if(name === "release:postcheck" || (name === "release:preflight" && args.checkpoint === "execution")) {
        if(!args.rpc) throw new Error("Explicit read-only RPC required");
        let executionEvidence;
        if(c.mode === "release" && name !== "release:postcheck") {
          if(!args.checkpointReview || !/^[a-f0-9]{64}$/i.test(args.checkpointReviewHash)) throw new Error("Fresh independently reviewed execution evidence and hash required");
          executionEvidence = checkedJson(args.checkpointReview,args.checkpointReviewHash);
        }
        const result = await verifyCheckpoint(c,inventory,artifacts,readOnlyRpc(args.rpc,c.mode === "simulation"),name === "release:postcheck" ? "R4" : "H",{executionEvidence});
        if(c.mode === "release") requireActionBearingCoverage(result);
        console.log(JSON.stringify(result,(_,v)=>typeof v==="bigint"?v.toString():v,2)); return;
      }
      let liveVerification: any;
      if(c.mode === "release") {
        if(!args.rpc) throw new Error("Release output requires a fresh pinned RPC recheck");
        liveVerification = await recheckObserved(c, inventory, readOnlyRpc(args.rpc));
        requireActionBearingCoverage(liveVerification);
      }
      let result: unknown;
      switch(name) {
        case "release:preflight": result = { ...checked, deploymentDefinitions: deploymentDefinitions(c,artifacts) }; break;
        case "release:build-u": result = buildU(c,inventory,artifacts); break;
        case "release:build-handoff": result = buildHandoff(c,inventory,artifacts); break;
        case "release:p1-runbook": result = p1Runbook(c,inventory,artifacts); break;
        case "release:testnet-handoff": result = testnetHandoff(c,inventory,artifacts); break;
        case "release:authority": result = {checked, roles: inventory.roles, assumptions: "Completeness requires the reviewed full history and fresh recheck, not candidate accounts."}; break;
        case "release:estimate-gas": {
          if(!args.rpc) throw new Error("Explicit local simulation RPC required");
          result = await estimateGas(c,buildU(c,inventory,artifacts).transactions,c.addresses.timelock,readOnlyRpc(args.rpc,true,true));break;
        }
      }
      if(liveVerification) {
        const anchor = {reviewedBlock: liveVerification.reviewedBlock, reviewedHash: liveVerification.reviewedHash, currentBlock: liveVerification.currentBlock, currentHash: liveVerification.currentHash};
        if((result as any)?.meta) (result as any).meta.description += ` Verified block ${anchor.currentBlock} (${anchor.currentHash}); reviewed GOV nonce ${c.review.safes.gov.nonce}. Recheck before signing.`;
        else result = {...(result as object), verification: anchor};
      }
      console.log(JSON.stringify(result,(_,v)=>typeof v==="bigint"?v.toString():v,2));
    }).build();
}
export const preflightTask=command("release:preflight","Read-only reviewed-input preflight and safe deployment definitions");
export const p1Task=command("release:p1-runbook","Build inspectable P-1 actor steps without execution");
export const handoffTask=command("release:build-handoff","Build unsigned mainnet H Safe transactions");
export const uTask=command("release:build-u","Build the atomic Oracle/StPROS U batch");
export const gasTask=command("release:estimate-gas","Local-only preliminary estimates; never FS-6 acceptance");
export const testnetTask=command("release:testnet-handoff","Build the separate T-0 through T-5 and U_t actor stages");
export const postcheckTask=command("release:postcheck","Read-only post-state expectations and explicit evidence limits");
export const authorityTask=command("release:authority","Inspect the complete reviewed role inventory");
