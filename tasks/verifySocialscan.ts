import fs from "node:fs/promises";
import path from "node:path";

import { task } from "hardhat/config";
import type { HardhatRuntimeEnvironment } from "hardhat/types/hre";
import { isFullyQualifiedName } from "hardhat/utils/contract-names";
import { encodeAbiParameters, isAddress, type Abi } from "viem";

type AbiConstructor = Extract<Abi[number], { type: "constructor" }>;

const SOCIALSCAN_API_URLS: Record<number, string> = {
  688689:
    "https://api.socialscan.io/pharos-atlantic-testnet/v1/explorer/command_api/contract",
  1672: "https://api.socialscan.io/pharos-mainnet/v1/explorer/command_api/contract",
};

const SOCIALSCAN_BROWSER_URLS: Record<number, string> = {
  688689: "https://pharos-testnet.socialscan.io",
  1672: "https://pharos.socialscan.io",
};

async function sleep(seconds: number): Promise<void> {
  await new Promise((resolve) => {
    setTimeout(resolve, seconds * 1000);
  });
}

async function readJsonFile<T>(filePath: string): Promise<T> {
  return JSON.parse(await fs.readFile(filePath, "utf8")) as T;
}

interface SocialscanResponse {
  status: string;
  message: string;
  result?: string;
}

function getRequiredAddress(value: string | undefined, label: string): string {
  if (value === undefined || value === "") {
    throw new Error(`Missing required ${label}`);
  }

  if (!isAddress(value)) {
    throw new Error(`Invalid ${label}: ${value}`);
  }

  return value;
}

function getRequiredContract(value: string | undefined): string {
  if (value === undefined || value === "") {
    throw new Error("Missing required contract (e.g. contracts/Oracle.sol:Oracle)");
  }

  if (!isFullyQualifiedName(value)) {
    throw new Error(`Invalid contract FQN: ${value}`);
  }

  return value;
}

async function socialscanRequest(
  apiUrl: string,
  fields: Record<string, string>,
): Promise<SocialscanResponse> {
  const body = new URLSearchParams(fields);

  const response = await fetch(apiUrl, {
    method: "POST",
    headers: {
      "Content-Type": "application/x-www-form-urlencoded",
    },
    body: body.toString(),
  });

  const responseText = await response.text();

  if (!response.ok) {
    throw new Error(
      `SocialScan request failed (${response.status}): ${responseText.slice(0, 500)}`,
    );
  }

  return JSON.parse(responseText) as SocialscanResponse;
}

function encodeConstructorArgs(
  abi: readonly unknown[],
  constructorArgs: string[],
  contract: string,
): string {
  const constructor = abi.find(
    (item): item is AbiConstructor =>
      typeof item === "object" &&
      item !== null &&
      "type" in item &&
      item.type === "constructor",
  );

  if (constructor === undefined || constructor.inputs.length === 0) {
    if (constructorArgs.length > 0) {
      throw new Error(`Contract ${contract} has no constructor arguments`);
    }
    return "";
  }

  if (constructor.inputs.length !== constructorArgs.length) {
    throw new Error(
      `Constructor argument count mismatch for ${contract}: expected ${constructor.inputs.length}, got ${constructorArgs.length}`,
    );
  }

  const encoded = encodeAbiParameters(constructor.inputs, constructorArgs);
  return encoded.slice(2);
}

async function getMinimalCompilerInput(
  hre: HardhatRuntimeEnvironment,
  sourceName: string,
  buildProfileName: string,
) {
  const rootFilePath = path.join(hre.config.paths.root, sourceName);
  const result = await hre.solidity.getCompilationJobs([rootFilePath], {
    buildProfile: buildProfileName,
    quiet: true,
    force: true,
  });

  if (!result.success) {
    throw new Error(`Failed to resolve compilation job for ${sourceName}`);
  }

  const compilationJobs = result.compilationJobsPerFile;
  const compilationJob = compilationJobs.get(rootFilePath);

  if (compilationJob === undefined) {
    throw new Error(`No compilation job found for ${sourceName}`);
  }

  return compilationJob.getSolcInput();
}

async function pollVerificationStatus(
  apiUrl: string,
  guid: string,
): Promise<void> {
  for (let attempt = 0; attempt < 40; attempt += 1) {
    await sleep(3);

    const response = await socialscanRequest(apiUrl, {
      module: "contract",
      action: "checkverifystatus",
      guid,
    });

    if (response.status !== "1") {
      continue;
    }

    if (
      response.message === "Pass - Verified" ||
      response.message.includes("already verified") ||
      response.message.includes("Contract successfully verified")
    ) {
      return;
    }

    if (response.message.startsWith("Fail")) {
      throw new Error(`SocialScan verification failed: ${response.message}`);
    }
  }

  throw new Error(`Timed out waiting for SocialScan verification (${guid})`);
}

export const verifySocialscanTask = task(
  "verify:socialscan",
  "Verify a contract on SocialScan using the official API format",
)
  /**
   * Usage:
   * pnpm hardhat build --build-profile default
   * pnpm hardhat verify:socialscan --network testnet --build-profile default \
   *   0xb922dc02c04a12ae59336140824641e29dac2860 \
   *   --contract contracts/Oracle.sol:Oracle
   */
  .addPositionalArgument({
    name: "address",
    description: "Deployed contract address",
  })
  .addOption({
    name: "contract",
    description: "Fully qualified contract name",
    defaultValue: "",
  })
  .setAction(async () => ({
    default: async (taskArgs, hre) => {
    const address = getRequiredAddress(taskArgs.address, "address");
    const contract = getRequiredContract(taskArgs.contract);
    const buildProfileName = hre.globalOptions.buildProfile ?? "production";
    const [sourceName] = contract.split(":");

    const buildProfile = hre.config.solidity.profiles[buildProfileName];
    if (buildProfile === undefined) {
      throw new Error(`Unknown build profile: ${buildProfileName}`);
    }

    const connection = await hre.network.connect();
    const chainIdHex = (await connection.provider.request({
      method: "eth_chainId",
    })) as string;
    const chainId = Number.parseInt(chainIdHex, 16);

    const apiUrl = SOCIALSCAN_API_URLS[chainId];
    const browserUrl = SOCIALSCAN_BROWSER_URLS[chainId];
    if (apiUrl === undefined || browserUrl === undefined) {
      throw new Error(`SocialScan is not configured for chainId ${chainId}`);
    }

    const buildInfoId = await hre.artifacts.getBuildInfoId(contract);
    if (buildInfoId === undefined) {
      throw new Error(
        `No build info found for ${contract}. Run: pnpm hardhat build --build-profile ${buildProfileName}`,
      );
    }

    const buildInfoPath = await hre.artifacts.getBuildInfoPath(buildInfoId);
    if (buildInfoPath === undefined) {
      throw new Error(`Build info path missing for ${contract}`);
    }

    const buildInfo = await readJsonFile<{
      solcLongVersion: string;
    }>(buildInfoPath);

    const artifact = await hre.artifacts.readArtifact(contract);
    const onChainBytecode = (await connection.provider.request({
      method: "eth_getCode",
      params: [address, "latest"],
    })) as string;

    if (
      onChainBytecode === "0x" ||
      !onChainBytecode.includes(artifact.deployedBytecode.slice(2))
    ) {
      throw new Error(
        `On-chain bytecode does not match ${contract}. Check address, contract, and --build-profile ${buildProfileName}.`,
      );
    }

    const compilerInput = await getMinimalCompilerInput(
      hre,
      sourceName,
      buildProfileName,
    );
    const encodedConstructorArgs = encodeConstructorArgs(
      artifact.abi,
      [],
      contract,
    );

    const optimizerEnabled = compilerInput.settings?.optimizer?.enabled ?? false;
    const optimizerRuns = compilerInput.settings?.optimizer?.runs ?? 200;

    console.log(`[verify:socialscan] address=${address}`);
    console.log(`[verify:socialscan] contract=${contract}`);
    console.log(`[verify:socialscan] buildProfile=${buildProfileName}`);
    console.log(`[verify:socialscan] apiUrl=${apiUrl}`);

    const submitResponse = await socialscanRequest(apiUrl, {
      module: "contract",
      action: "verifysourcecode",
      contractaddress: address,
      sourceCode: JSON.stringify(compilerInput),
      codeformat: "solidity-standard-json-input",
      contractname: contract,
      compilerversion: `v${buildInfo.solcLongVersion}`,
      constructorArguments: encodedConstructorArgs,
      optimizationUsed: optimizerEnabled ? "1" : "0",
      runs: String(optimizerRuns),
    });

    if (
      submitResponse.status === "1" &&
      (submitResponse.message.includes("Contract successfully verified") ||
        submitResponse.message.includes("Already Verified") ||
        submitResponse.message.includes("already verified"))
    ) {
      console.log(`✅ Verified on SocialScan: ${browserUrl}/address/${address}#code`);
      await connection.close();
      return;
    }

    if (submitResponse.status !== "1" || submitResponse.result === undefined) {
      throw new Error(
        `SocialScan verification submission failed: ${submitResponse.message}`,
      );
    }

    console.log(`⏳ Submitted verification guid=${submitResponse.result}`);
    await pollVerificationStatus(apiUrl, submitResponse.result);
    console.log(`✅ Verified on SocialScan: ${browserUrl}/address/${address}#code`);

    await connection.close();
    },
  }))
  .build();
