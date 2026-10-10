import { task } from "hardhat/config";
import { isAddress, type Address, type PublicClient } from "viem";

import { Mainnet, TESTNET } from "../contants/index.js";

const EIP1967_IMPLEMENTATION_SLOT =
  "0x360894a13ba1a3210667c828492db98dca3e2076cc3735a920a3ca505d382bbc";
const EIP1967_ADMIN_SLOT =
  "0xb53127684a568b3173ae13b9f8a6016e243e63b6e8ee1178d6a717850b5d6103";

const PROXY_ADMIN_ABI = [
  {
    type: "function",
    name: "owner",
    stateMutability: "view",
    inputs: [],
    outputs: [{ name: "", type: "address" }],
  },
] as const;

const BEACON_ABI = [
  {
    type: "function",
    name: "owner",
    stateMutability: "view",
    inputs: [],
    outputs: [{ name: "", type: "address" }],
  },
  {
    type: "function",
    name: "implementation",
    stateMutability: "view",
    inputs: [],
    outputs: [{ name: "", type: "address" }],
  },
] as const;

type NetworkDefaults = {
  ORACLE: Address;
  STPROS: Address;
  YIELD_VAULT_FACTORY: Address;
};

type OwnableProxyPermissions = {
  label: string;
  proxy: Address;
  owner: Address | undefined;
  paused: boolean | undefined;
  proxyAdmin: Address;
  proxyAdminOwner: Address;
  implementation: Address;
  ownerCapabilities: string[];
  upgradeCapabilities: string[];
  warnings: string[];
};

const ZERO_ADDRESS = "0x0000000000000000000000000000000000000000" as Address;

function getOptionalAddress(value: string | undefined, label: string): Address | undefined {
  if (value === undefined || value === "") {
    return undefined;
  }

  if (!isAddress(value)) {
    throw new Error(`Invalid ${label}: ${value}`);
  }

  return value;
}

function getNetworkDefaults(networkName: string): NetworkDefaults | undefined {
  if (networkName === "mainnet") {
    return Mainnet;
  }

  if (networkName === "testnet") {
    return TESTNET;
  }

  return undefined;
}

function addressFromStorageSlot(slotValue: `0x${string}` | null | undefined): Address | undefined {
  if (slotValue === undefined || slotValue === null) {
    return undefined;
  }

  const normalized = slotValue.toLowerCase();
  if (normalized === "0x" || /^0x0+$/.test(normalized)) {
    return undefined;
  }

  return `0x${normalized.slice(-40)}` as Address;
}

async function readProxyAdmin(publicClient: PublicClient, proxy: Address): Promise<Address | undefined> {
  const slotValue = await publicClient.getStorageAt({
    address: proxy,
    slot: EIP1967_ADMIN_SLOT,
  });

  return addressFromStorageSlot(slotValue);
}

async function readProxyImplementation(
  publicClient: PublicClient,
  proxy: Address,
): Promise<Address | undefined> {
  const slotValue = await publicClient.getStorageAt({
    address: proxy,
    slot: EIP1967_IMPLEMENTATION_SLOT,
  });

  return addressFromStorageSlot(slotValue);
}

async function readProxyAdminOwner(
  publicClient: PublicClient,
  proxyAdmin: Address | undefined,
): Promise<Address | undefined> {
  if (proxyAdmin === undefined) {
    return undefined;
  }

  return publicClient.readContract({
    address: proxyAdmin,
    abi: PROXY_ADMIN_ABI,
    functionName: "owner",
  });
}

function matchesAddress(left: Address | undefined, right: Address | undefined): boolean {
  if (left === undefined || right === undefined) {
    return false;
  }

  return left.toLowerCase() === right.toLowerCase();
}

function printOwnableProxyPermissions(
  report: OwnableProxyPermissions,
  account: Address | undefined,
): void {
  console.log(`[perms:check] ${report.label}`);
  console.log(`[perms:check]   proxy=${report.proxy}`);
  console.log(`[perms:check]   owner=${report.owner ?? "unknown"}`);
  console.log(`[perms:check]   paused=${report.paused ?? "unknown"}`);
  console.log(`[perms:check]   proxyAdmin=${report.proxyAdmin}`);
  console.log(`[perms:check]   proxyAdminOwner=${report.proxyAdminOwner}`);
  console.log(`[perms:check]   implementation=${report.implementation}`);
  console.log(`[perms:check]   ownerCapabilities=${report.ownerCapabilities.join("; ")}`);
  console.log(`[perms:check]   upgradeCapabilities=${report.upgradeCapabilities.join("; ")}`);

  for (const warning of report.warnings) {
    console.log(`[perms:check]   warning=${warning}`);
  }

  if (account !== undefined && report.owner !== undefined) {
    console.log(
      `[perms:check]   accountIsOwner=${matchesAddress(account, report.owner)}`,
    );
    console.log(
      `[perms:check]   accountCanUpgradeProxy=${matchesAddress(account, report.proxyAdminOwner)}`,
    );
  }
}

async function safeContractRead<T>(
  label: string,
  read: () => Promise<T>,
): Promise<{ value?: T; warning?: string }> {
  try {
    return { value: await read() };
  } catch (error) {
    const message = error instanceof Error ? error.message : String(error);
    return { warning: `${label} read failed: ${message}` };
  }
}

async function inspectOwnableProxy(
  publicClient: PublicClient,
  getContractAt: (contractName: "Oracle" | "StPROS" | "YieldVaultFactory", address: Address) => Promise<{
    read: {
      owner: () => Promise<Address>;
      paused: () => Promise<boolean>;
    };
  }>,
  label: "Oracle" | "StPROS" | "YieldVaultFactory",
  proxy: Address,
  ownerCapabilities: string[],
  upgradeCapabilities: string[],
): Promise<OwnableProxyPermissions> {
  const warnings: string[] = [];
  const bytecode = await publicClient.getBytecode({ address: proxy });

  if (bytecode === undefined || bytecode === "0x") {
    warnings.push("address has no contract code; check proxy address and network");
    return {
      label,
      proxy,
      owner: undefined,
      paused: undefined,
      proxyAdmin: ZERO_ADDRESS,
      proxyAdminOwner: ZERO_ADDRESS,
      implementation: ZERO_ADDRESS,
      ownerCapabilities,
      upgradeCapabilities,
      warnings,
    };
  }

  const contract = await getContractAt(label, proxy);
  const [ownerResult, pausedResult, proxyAdmin, implementation] = await Promise.all([
    safeContractRead("owner()", () => contract.read.owner()),
    safeContractRead("paused()", () => contract.read.paused()),
    readProxyAdmin(publicClient, proxy),
    readProxyImplementation(publicClient, proxy),
  ]);

  if (ownerResult.warning !== undefined) {
    warnings.push(ownerResult.warning);
  }
  if (pausedResult.warning !== undefined) {
    warnings.push(pausedResult.warning);
  }
  if (proxyAdmin === undefined) {
    warnings.push("proxy admin slot is empty; address may not be a transparent proxy");
  }
  if (implementation === undefined) {
    warnings.push("implementation slot is empty; address may not be a transparent proxy");
  }

  const proxyAdminOwnerResult = proxyAdmin === undefined
    ? { warning: "skipped proxyAdmin.owner() because proxy admin is unknown" }
    : await safeContractRead("proxyAdmin.owner()", () => readProxyAdminOwner(publicClient, proxyAdmin));

  if (proxyAdminOwnerResult.warning !== undefined) {
    warnings.push(proxyAdminOwnerResult.warning);
  }

  return {
    label,
    proxy,
    owner: ownerResult.value,
    paused: pausedResult.value,
    proxyAdmin: proxyAdmin ?? ZERO_ADDRESS,
    proxyAdminOwner: proxyAdminOwnerResult.value ?? ZERO_ADDRESS,
    implementation: implementation ?? ZERO_ADDRESS,
    ownerCapabilities,
    upgradeCapabilities,
    warnings,
  };
}

export const permsCheckTask = task(
  "perms:check",
  "Check owner and upgrade permissions for Oracle, StPROS, and YieldVaultFactory",
)
  /**
   * Usage:
   * pnpm hardhat perms:check --network mainnet
   *
   * Example:
   * pnpm hardhat perms:check --network mainnet \
   *   --oracle 0x051713fd66845a13bf23baca008c5c22c27ccb58 \
   *   --stpros 0x6b0a44c64190279f7034b77c13a566e914fe5ec4 \
   *   --factory 0xc9fb7dc52b0fb92c417d481442d2641637483881 \
   *   --account 0xYourAddress
   *
   * Notes:
   * - On mainnet/testnet, omitted addresses default to contants/index.ts.
   * - owner = Ownable owner on the proxy (business/admin operations).
   * - proxyAdminOwner = ProxyAdmin owner (transparent proxy implementation upgrades).
   * - YieldVaultFactory beacon upgrades are controlled by factory owner via upgradeBeaconTo.
   */
  .addOption({
    name: "oracle",
    description: "Oracle proxy address",
    defaultValue: "",
  })
  .addOption({
    name: "stpros",
    description: "StPROS proxy address",
    defaultValue: "",
  })
  .addOption({
    name: "factory",
    description: "YieldVaultFactory proxy address",
    defaultValue: "",
  })
  .addOption({
    name: "account",
    description: "Optional account to check against owner / upgrade permissions",
    defaultValue: "",
  })
  .setInlineAction(async (taskArgs, hre) => {
    const connection = await hre.network.getOrCreate();

    try {
      const networkDefaults = getNetworkDefaults(connection.networkName);
      const account = getOptionalAddress(taskArgs.account, "account");

      const oracleAddress =
        getOptionalAddress(taskArgs.oracle, "oracle address") ?? networkDefaults?.ORACLE;
      const stProsAddress =
        getOptionalAddress(taskArgs.stpros, "stpros address") ?? networkDefaults?.STPROS;
      const factoryAddress =
        getOptionalAddress(taskArgs.factory, "factory address")
        ?? networkDefaults?.YIELD_VAULT_FACTORY;

      if (oracleAddress === undefined) {
        throw new Error("Missing oracle address. Pass --oracle or use mainnet/testnet defaults.");
      }
      if (stProsAddress === undefined) {
        throw new Error("Missing stpros address. Pass --stpros or use mainnet/testnet defaults.");
      }
      if (factoryAddress === undefined) {
        throw new Error("Missing factory address. Pass --factory or use mainnet/testnet defaults.");
      }

      const publicClient = await connection.viem.getPublicClient();
      const getContractAt = connection.viem.getContractAt.bind(connection.viem);

      const [oracleReport, stProsReport, factoryReport] = await Promise.all([
        inspectOwnableProxy(
          publicClient,
          getContractAt,
          "Oracle",
          oracleAddress,
          ["pause/unpause"],
          ["upgrade Oracle proxy implementation via ProxyAdmin"],
        ),
        inspectOwnableProxy(
          publicClient,
          getContractAt,
          "StPROS",
          stProsAddress,
          [
            "setOracle",
            "setMaxWithdrawCount",
            "setUnbondingPeriod",
            "pause/unpause",
          ],
          ["upgrade StPROS proxy implementation via ProxyAdmin"],
        ),
        inspectOwnableProxy(
          publicClient,
          getContractAt,
          "YieldVaultFactory",
          factoryAddress,
          [
            "createYieldVault",
            "add/remove counterparty whitelist",
            "upgradeBeaconTo",
            "emergencyCancel",
            "pause/unpause",
          ],
          ["upgrade YieldVaultFactory proxy implementation via ProxyAdmin"],
        ),
      ]);

      const stProsContract = await connection.viem.getContractAt("StPROS", stProsAddress);
      const factoryContract = await connection.viem.getContractAt(
        "YieldVaultFactory",
        factoryAddress,
      );
      const [stProsOracleResult, beaconResult, yieldVaultImplementationResult, totalProxiesResult] =
        await Promise.all([
          safeContractRead("stPROS.oracle()", () => stProsContract.read.oracle()),
          safeContractRead("factory.beacon()", () => factoryContract.read.beacon()),
          safeContractRead(
            "factory.currentImplementation()",
            () => factoryContract.read.currentImplementation(),
          ),
          safeContractRead("factory.totalProxies()", () => factoryContract.read.totalProxies()),
        ]);

      const beacon = beaconResult.value;
      const beaconReads = beacon === undefined
        ? {
            owner: { warning: "skipped beacon.owner() because beacon is unknown" },
            implementation: { warning: "skipped beacon.implementation() because beacon is unknown" },
          }
        : {
            owner: await safeContractRead("beacon.owner()", () =>
              publicClient.readContract({
                address: beacon,
                abi: BEACON_ABI,
                functionName: "owner",
              }),
            ),
            implementation: await safeContractRead("beacon.implementation()", () =>
              publicClient.readContract({
                address: beacon,
                abi: BEACON_ABI,
                functionName: "implementation",
              }),
            ),
          };

      console.log(`[perms:check] network=${connection.networkName}`);
      if (account !== undefined) {
        console.log(`[perms:check] account=${account}`);
      }
      console.log("");

      printOwnableProxyPermissions(oracleReport, account);
      console.log("");
      printOwnableProxyPermissions(stProsReport, account);
      console.log(`[perms:check]   linkedOracle=${stProsOracleResult.value ?? "unknown"}`);
      if (stProsOracleResult.warning !== undefined) {
        console.log(`[perms:check]   warning=${stProsOracleResult.warning}`);
      }
      console.log("");
      printOwnableProxyPermissions(factoryReport, account);
      console.log(`[perms:check]   beacon=${beacon ?? "unknown"}`);
      console.log(`[perms:check]   beaconOwner=${beaconReads.owner.value ?? "unknown"}`);
      console.log(`[perms:check]   beaconImplementation=${beaconReads.implementation.value ?? "unknown"}`);
      console.log(
        `[perms:check]   currentYieldVaultImplementation=${yieldVaultImplementationResult.value ?? "unknown"}`,
      );
      console.log(`[perms:check]   totalVaultProxies=${totalProxiesResult.value ?? "unknown"}`);
      if (beaconResult.warning !== undefined) {
        console.log(`[perms:check]   warning=${beaconResult.warning}`);
      }
      if (yieldVaultImplementationResult.warning !== undefined) {
        console.log(`[perms:check]   warning=${yieldVaultImplementationResult.warning}`);
      }
      if (totalProxiesResult.warning !== undefined) {
        console.log(`[perms:check]   warning=${totalProxiesResult.warning}`);
      }
      if (beaconReads.owner.warning !== undefined) {
        console.log(`[perms:check]   warning=${beaconReads.owner.warning}`);
      }
      if (beaconReads.implementation.warning !== undefined) {
        console.log(`[perms:check]   warning=${beaconReads.implementation.warning}`);
      }
      console.log(
        "[perms:check]   beaconUpgradeCapabilities=YieldVaultFactory owner calls upgradeBeaconTo",
      );

      if (account !== undefined && factoryReport.owner !== undefined) {
        console.log(
          `[perms:check]   accountCanUpgradeBeacon=${matchesAddress(account, factoryReport.owner)}`,
        );
        console.log(
          `[perms:check]   accountCanCreateVault=${matchesAddress(account, factoryReport.owner)}`,
        );
      }
    } finally {
      await connection.close();
    }
  })
  .build();
