// Normalize one independently compiled, production-profile solc output.
// Run only inside the approved isolated export. Prints JSON; never writes or deploys.
import { checkedJson, canonical, sha256, requireThat } from "./core.mjs";
import { pathToFileURL } from "node:url";
export function freezeArtifact(buildInfo, output, source, name) {
 const input=buildInfo.input;
 requireThat(buildInfo.solcVersion==="0.8.28" && input?.settings?.optimizer?.enabled===true && input.settings.optimizer.runs===200 && input.settings.evmVersion==="cancun","PRODUCTION_BUILD_SETTINGS");
 const c=(output.output??output).contracts?.[source]?.[name];
 requireThat(c?.abi && c.evm?.deployedBytecode?.object,"BUILD_CONTRACT");
 requireThat(!Object.keys(c.evm.deployedBytecode.linkReferences??{}).length,"LINKED_LIBRARY_REVIEW_REQUIRED");
 const artifact={contractName:name,sourceName:source,compiler:buildInfo.solcVersion,inputSha256:sha256(canonical(input)),profile:"production",abi:c.abi,deployedBytecode:`0x${c.evm.deployedBytecode.object}`,immutableReferences:c.evm.deployedBytecode.immutableReferences??{}};
 return {artifact,canonicalSha256:sha256(canonical(artifact)),reviewStatus:"UNREVIEWED — independent approval required"};
}
if(process.argv[1] && import.meta.url===pathToFileURL(process.argv[1]).href) {
 const [info,output,source,name]=process.argv.slice(2);requireThat(info&&output&&source&&name,"USAGE: artifacts.mjs BUILD_INFO.json OUTPUT.json SOURCE CONTRACT");
 console.log(JSON.stringify(freezeArtifact(checkedJson(info),checkedJson(output),source,name),null,2));
}
