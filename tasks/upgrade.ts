import { task } from "hardhat/config";
import {
  encodeFunctionData,
  getContract,
  isAddress,
  type Abi,
  type Address,
  type PublicClient,
  type WalletClient,
} from "viem";

import { Mainnet, TESTNET } from "../contants/index.js";
import mainnetProxyAdminDeployment from "../deployments/mainnet/DefaultProxyAdmin.json" with { type: "json" };
import testnetProxyAdminDeployment from "../deployments/testnet/DefaultProxyAdmin.json" with { type: "json" };

const PROXY_ADMIN_UPGRADE_ABI = [
  {
    type: "function",
    name: "upgrade",
    stateMutability: "nonpayable",
    inputs: [
      { name: "proxy", type: "address" },
      { name: "implementation", type: "address" },
    ],
    outputs: [],
  },
] as const;

const PROXY_ADMIN_UPGRADE_AND_CALL_ABI = [
  {
    type: "function",
    name: "upgradeAndCall",
    stateMutability: "payable",
    inputs: [
      { name: "proxy", type: "address" },
      { name: "implementation", type: "address" },
      { name: "data", type: "bytes" },
    ],
    outputs: [],
  },
] as const;

const ORACLE_INITIALIZE_V2_ABI = [
  {
    type: "function",
    name: "initializeV2",
    stateMutability: "nonpayable",
    inputs: [
      { name: "_slp", type: "address" },
      { name: "_vToken", type: "address" },
      { name: "_maxUpdateAmount", type: "uint256" },
      { name: "_updateInterval", type: "uint256" },
    ],
    outputs: [],
  },
] as const;

const ORACLE_INITIALIZE_V3_ABI = [
  {
    type: "function",
    name: "initializeV3",
    stateMutability: "nonpayable",
    inputs: [
      { name: "_commissionAccount", type: "address" },
      { name: "_commissionRatePpm", type: "uint256" },
      { name: "_tokens", type: "address[]" },
      { name: "_vTokens", type: "address[]" },
    ],
    outputs: [],
  },
] as const;

const ORACLE_INITIALIZE_V2_AND_V3_ABI = [
  {
    type: "function",
    name: "initializeV2AndV3",
    stateMutability: "nonpayable",
    inputs: [
      { name: "_slp", type: "address" },
      { name: "_vToken", type: "address" },
      { name: "_maxUpdateAmount", type: "uint256" },
      { name: "_updateInterval", type: "uint256" },
      { name: "_commissionAccount", type: "address" },
      { name: "_commissionRatePpm", type: "uint256" },
      { name: "_tokens", type: "address[]" },
      { name: "_vTokens", type: "address[]" },
    ],
    outputs: [],
  },
] as const;

const STPROS_INITIALIZE_V2_ABI = [
  {
    type: "function",
    name: "initializeV2",
    stateMutability: "nonpayable",
    inputs: [
      { name: "_slp", type: "address" },
      { name: "_bridgeVault", type: "address" },
    ],
    outputs: [],
  },
] as const;

const ERC1967_IMPLEMENTATION_SLOT =
  "0x360894a13ba1a3210667c828492db98dca3e2076cc3735a920a3ca505d382bbc" as const;

type UpgradeParams = {
  taskLabel: string;
  networkName: string;
  proxy: Address;
  implementation: Address;
  proxyAdmin: Address;
  initData: `0x${string}`;
  publicClient?: PublicClient;
  signer?: WalletClient;
  afterUpgrade?: () => Promise<void>;
};

function getOptionalAddress(value: string | undefined, label: string): Address | undefined {
  if (value === undefined || value === "") {
    return undefined;
  }

  if (!isAddress(value)) {
    throw new Error(`Invalid ${label}: ${value}`);
  }

  return value;
}

function getRequiredAddress(value: string | undefined, label: string): Address {
  const address = getOptionalAddress(value, label);

  if (address === undefined) {
    throw new Error(`Missing required ${label}`);
  }

  return address;
}

function getRequiredBigInt(value: string | undefined, label: string): bigint {
  if (value === undefined || value.trim() === "") {
    throw new Error(`Missing required ${label}`);
  }

  return BigInt(value);
}

function getOptionalAddressList(value: string | undefined, label: string): Address[] | undefined {
  if (value === undefined || value.trim() === "") {
    return undefined;
  }

  return value.split(",").map((item, index) => {
    const address = item.trim();

    if (!isAddress(address)) {
      throw new Error(`Invalid ${label}[${index}]: ${address}`);
    }

    return address;
  });
}

function getDefaultCommissionTokens(networkName: string): Address[] | undefined {
  if (networkName === "mainnet") {
    return [Mainnet.WPROS as Address];
  }

  if (networkName === "testnet") {
    return [TESTNET.WPROS as Address];
  }

  return undefined;
}

function getDefaultCommissionVTokens(networkName: string): Address[] | undefined {
  const stPros = getNetworkStPros(networkName);

  if (stPros === undefined) {
    return undefined;
  }

  return [stPros];
}

function requireSameLength<T, U>(left: T[], right: U[], leftLabel: string, rightLabel: string): void {
  if (left.length !== right.length) {
    throw new Error(`${leftLabel} length ${left.length} must equal ${rightLabel} length ${right.length}`);
  }
}

function getNetworkOracle(networkName: string): Address | undefined {
  if (networkName === "mainnet") {
    return Mainnet.ORACLE as Address;
  }

  if (networkName === "testnet") {
    return TESTNET.ORACLE as Address;
  }

  return undefined;
}

function getNetworkStPros(networkName: string): Address | undefined {
  if (networkName === "mainnet") {
    return Mainnet.STPROS as Address;
  }

  if (networkName === "testnet") {
    return TESTNET.STPROS as Address;
  }

  return undefined;
}

function getNetworkSlp(networkName: string): Address | undefined {
  if (networkName === "testnet") {
    return TESTNET.SLP as Address;
  }

  return undefined;
}

function getNetworkBridgeVault(networkName: string): Address | undefined {
  if (networkName === "testnet") {
    return TESTNET.BRIDGE_VAULT as Address;
  }

  return undefined;
}

function getNetworkProxyAdmin(networkName: string): Address | undefined {
  if (networkName === "mainnet") {
    return "0x4238ea4adfa2bd6a5fc9b5e245dc1900cf0258aa";
  }

  if (networkName === "testnet") {
    return "0x79d6028229f2d819a1a4bb52a05bc97f5f37d667";
  }

  return undefined;
}

function getProxyAdminDeploymentAbi(networkName: string): readonly unknown[] {
  if (networkName === "testnet") {
    return testnetProxyAdminDeployment.abi;
  }

  if (networkName === "mainnet") {
    return mainnetProxyAdminDeployment.abi;
  }

  throw new Error(`No deployed ProxyAdmin ABI for network ${networkName}`);
}

function proxyAdminUsesV5ImplUpgrade(abi: readonly { name?: string }[]): boolean {
  return abi.some((item) => item.name === "UPGRADE_INTERFACE_VERSION");
}

function getDeployedProxyAdminContract(
  proxyAdminAbi: readonly unknown[],
  proxyAdmin: Address,
  publicClient: PublicClient,
  walletClient: WalletClient,
) {
  return getContract({
    address: proxyAdmin,
    abi: proxyAdminAbi as Abi,
    client: {
      public: publicClient,
      wallet: walletClient,
    },
  });
}

async function getProxyImplementation(
  publicClient: PublicClient,
  proxy: Address,
): Promise<Address> {
  const storage = await publicClient.getStorageAt({
    address: proxy,
    slot: ERC1967_IMPLEMENTATION_SLOT,
  });

  if (storage === undefined) {
    throw new Error(`Failed to read implementation slot for proxy ${proxy}`);
  }

  return (`0x${storage.slice(-40)}`) as Address;
}

function buildUpgradeCalldata(
  proxy: Address,
  implementation: Address,
  initData: `0x${string}`,
  useV5Upgrade: boolean,
): `0x${string}` {
  if (initData !== "0x" || useV5Upgrade) {
    return encodeFunctionData({
      abi: PROXY_ADMIN_UPGRADE_AND_CALL_ABI,
      functionName: "upgradeAndCall",
      args: [proxy, implementation, initData],
    });
  }

  return encodeFunctionData({
    abi: PROXY_ADMIN_UPGRADE_ABI,
    functionName: "upgrade",
    args: [proxy, implementation],
  });
}

function requireSupportedNetwork(networkName: string): void {
  if (networkName !== "testnet" && networkName !== "mainnet") {
    throw new Error(`Unsupported network ${networkName}; use testnet or mainnet`);
  }
}

function printSafeTransaction(params: {
  taskLabel: string;
  proxy: Address;
  implementation: Address;
  proxyAdmin: Address;
  proxyAdminInterface: "v4" | "v5";
  initData: `0x${string}`;
  upgradeCalldata: `0x${string}`;
}): void {
  const method = params.initData === "0x" && params.proxyAdminInterface === "v4"
    ? "upgrade(address,address)"
    : "upgradeAndCall(address,address,bytes)";

  console.log(`[${params.taskLabel}] safe.to=${params.proxyAdmin}`);
  console.log(`[${params.taskLabel}] safe.value=0`);
  console.log(`[${params.taskLabel}] safe.method=${method}`);
  console.log(`[${params.taskLabel}] proxy=${params.proxy}`);
  console.log(`[${params.taskLabel}] implementation=${params.implementation}`);
  console.log(`[${params.taskLabel}] proxyAdmin=${params.proxyAdmin}`);
  console.log(`[${params.taskLabel}] proxyAdminInterface=${params.proxyAdminInterface}`);
  console.log(`[${params.taskLabel}] initializerCalldata=${params.initData}`);
  console.log(`[${params.taskLabel}] calldata=${params.upgradeCalldata}`);
}

async function runUpgrade(params: UpgradeParams): Promise<void> {
  requireSupportedNetwork(params.networkName);

  const proxyAdminAbi = getProxyAdminDeploymentAbi(params.networkName);
  const useV5Upgrade = proxyAdminUsesV5ImplUpgrade(
    proxyAdminAbi as readonly { name?: string }[],
  );
  const proxyAdminInterface = useV5Upgrade ? "v5" : "v4";
  const upgradeCalldata = buildUpgradeCalldata(
    params.proxy,
    params.implementation,
    params.initData,
    useV5Upgrade,
  );

  if (params.networkName === "mainnet") {
    printSafeTransaction({
      taskLabel: params.taskLabel,
      proxy: params.proxy,
      implementation: params.implementation,
      proxyAdmin: params.proxyAdmin,
      proxyAdminInterface,
      initData: params.initData,
      upgradeCalldata,
    });
    return;
  }

  if (params.signer?.account?.address === undefined) {
    throw new Error("No signer account available for the selected network");
  }

  if (params.publicClient === undefined) {
    throw new Error("No public client available for testnet upgrade execution");
  }

  const proxyAdminContract = getDeployedProxyAdminContract(
    proxyAdminAbi,
    params.proxyAdmin,
    params.publicClient,
    params.signer,
  );
  const proxyAdminOwner = (await proxyAdminContract.read.owner()) as Address;

  if (proxyAdminOwner.toLowerCase() !== params.signer.account.address.toLowerCase()) {
    throw new Error(
      `Signer ${params.signer.account.address} is not ProxyAdmin owner ${proxyAdminOwner}`,
    );
  }

  const previousImplementation = await getProxyImplementation(params.publicClient, params.proxy);
  const hash = params.initData !== "0x" || useV5Upgrade
    ? await proxyAdminContract.write.upgradeAndCall([
        params.proxy,
        params.implementation,
        params.initData,
      ])
    : await proxyAdminContract.write.upgrade([params.proxy, params.implementation]);
  const receipt = await params.publicClient.waitForTransactionReceipt({ hash });
  const currentImplementation = await getProxyImplementation(params.publicClient, params.proxy);

  console.log(`[${params.taskLabel}] proxy=${params.proxy}`);
  console.log(`[${params.taskLabel}] implementation=${params.implementation}`);
  console.log(`[${params.taskLabel}] proxyAdmin=${params.proxyAdmin}`);
  console.log(`[${params.taskLabel}] proxyAdminInterface=${proxyAdminInterface}`);
  console.log(`[${params.taskLabel}] caller=${params.signer.account.address}`);
  console.log(`[${params.taskLabel}] previousImplementation=${previousImplementation}`);
  console.log(`[${params.taskLabel}] currentImplementation=${currentImplementation}`);
  console.log(`[${params.taskLabel}] initializerCalldata=${params.initData}`);
  console.log(`[${params.taskLabel}] txHash=${hash}`);
  console.log(`[${params.taskLabel}] blockNumber=${receipt.blockNumber}`);

  await params.afterUpgrade?.();
}

export const oracleUpgradeTask = task(
  "oracle:upgrade",
  "Upgrade Oracle implementation only, or print Safe calldata on mainnet",
)
  /**
   * Usage:
   * pnpm hardhat oracle:upgrade --network testnet <implementation>
   * pnpm hardhat oracle:upgrade --network mainnet <implementation>
   */
  .addPositionalArgument({
    name: "implementation",
    description: "New Oracle implementation address",
  })
  .addOption({
    name: "proxy",
    description: "Oracle proxy address; defaults to contants/index.ts on mainnet/testnet",
    defaultValue: "",
  })
  .addOption({
    name: "proxyAdmin",
    description: "ProxyAdmin contract address",
    defaultValue: "",
  })
  .setInlineAction(async () => {
    throw new Error("Release tooling required: implementation-only/proxy/beacon upgrades are disabled. Build a reviewed atomic Timelock operation; this legacy task cannot execute.");
  })
  .build();

export const oracleUpgradeV2Task = task(
  "oracle:upgrade-v2",
  "Deprecated in R4; refuses before connecting to a network",
)
  .addPositionalArgument({
    name: "implementation",
    description: "New Oracle implementation address",
  })
  .addOption({
    name: "proxy",
    description: "Oracle proxy address; defaults to contants/index.ts on mainnet/testnet",
    defaultValue: "",
  })
  .addOption({
    name: "slp",
    description: "SLP address; defaults to TESTNET.SLP on testnet",
    defaultValue: "",
  })
  .addOption({
    name: "vToken",
    description: "vToken address; defaults to contants/index.ts STPROS on mainnet/testnet",
    defaultValue: "",
  })
  .addOption({
    name: "maxUpdateAmount",
    description: "Maximum token amount per update; 0 disables update",
    defaultValue: "100000000000000000000",
  })
  .addOption({
    name: "updateInterval",
    description: "Minimum seconds between updates per token; 0 disables interval check",
    defaultValue: "3600",
  })
  .addOption({
    name: "proxyAdmin",
    description: "ProxyAdmin contract address",
    defaultValue: "",
  })
  .setInlineAction(async () => {
    throw new Error(
      "Deprecated oracle:upgrade-v2: R4 removed initializeV2. Legacy V2 upgrade commands are disabled; use independently reviewed R4 migration tooling when available.",
    );
  })
  .build();

export const oracleUpgradeV3Task = task(
  "oracle:upgrade-v3",
  "Deprecated in R4; refuses before connecting to a network",
)
  .addPositionalArgument({
    name: "implementation",
    description: "New Oracle implementation address",
  })
  .addOption({
    name: "proxy",
    description: "Oracle proxy address; defaults to contants/index.ts on mainnet/testnet",
    defaultValue: "",
  })
  .addOption({
    name: "commissionAccount",
    description: "Global commission account that receives minted vTokens",
    defaultValue: "",
  })
  .addOption({
    name: "commissionRatePpm",
    description: "Commission rate in PPM, where 1_000_000 is 100%",
    defaultValue: "",
  })
  .addOption({
    name: "tokens",
    description: "Comma-separated token addresses for commission mapping; defaults to WPROS",
    defaultValue: "",
  })
  .addOption({
    name: "vTokens",
    description: "Comma-separated vToken addresses for commission mapping; defaults to STPROS",
    defaultValue: "",
  })
  .addOption({
    name: "slp",
    description: "SLP address for mainnet initializeV2AndV3; defaults to TESTNET.SLP on testnet",
    defaultValue: "",
  })
  .addOption({
    name: "vToken",
    description: "VToken address for mainnet initializeV2AndV3; defaults to STPROS",
    defaultValue: "",
  })
  .addOption({
    name: "maxUpdateAmount",
    description: "Maximum token amount per update for mainnet initializeV2AndV3",
    defaultValue: "100000000000000000000",
  })
  .addOption({
    name: "updateInterval",
    description: "Minimum seconds between updates per token for mainnet initializeV2AndV3",
    defaultValue: "3600",
  })
  .addOption({
    name: "proxyAdmin",
    description: "ProxyAdmin contract address",
    defaultValue: "",
  })
  .setInlineAction(async () => {
    throw new Error(
      "Deprecated oracle:upgrade-v3: R4 removed initializeV3 and initializeV2AndV3. Use the reviewed four-argument migrateFromV3 through atomic ProxyAdmin upgradeAndCall; this legacy command remains disabled.",
    );
  })
  .build();

export const stProsUpgradeTask = task(
  "stpros:upgrade",
  "Upgrade StPROS implementation only, or print Safe calldata on mainnet",
)
  /**
   * Usage:
   * pnpm hardhat stpros:upgrade --network testnet <implementation>
   * pnpm hardhat stpros:upgrade --network mainnet <implementation>
   */
  .addPositionalArgument({
    name: "implementation",
    description: "New StPROS implementation address",
  })
  .addOption({
    name: "proxy",
    description: "StPROS proxy address; defaults to contants/index.ts on mainnet/testnet",
    defaultValue: "",
  })
  .addOption({
    name: "proxyAdmin",
    description: "ProxyAdmin contract address",
    defaultValue: "",
  })
  .setInlineAction(async () => {
    throw new Error("Release tooling required: implementation-only/proxy/beacon upgrades are disabled. Build a reviewed atomic Timelock operation; this legacy task cannot execute.");
  })
  .build();

export const stProsUpgradeV2Task = task(
  "stpros:upgrade-v2",
  "Deprecated in R4; refuses before connecting to a network",
)
  .addPositionalArgument({
    name: "implementation",
    description: "New StPROS implementation address",
  })
  .addOption({
    name: "proxy",
    description: "StPROS proxy address; defaults to contants/index.ts on mainnet/testnet",
    defaultValue: "",
  })
  .addOption({
    name: "slp",
    description: "SLP address; defaults to TESTNET.SLP on testnet",
    defaultValue: "",
  })
  .addOption({
    name: "bridgeVault",
    description: "BridgeVault address; defaults to TESTNET.BRIDGE_VAULT on testnet",
    defaultValue: "",
  })
  .addOption({
    name: "proxyAdmin",
    description: "ProxyAdmin contract address",
    defaultValue: "",
  })
  .setInlineAction(async () => {
    throw new Error(
      "Deprecated stpros:upgrade-v2: R4 removed initializeV2. Legacy V2 upgrade commands are disabled; use independently reviewed R4 migration tooling when available.",
    );
  })
  .build();
