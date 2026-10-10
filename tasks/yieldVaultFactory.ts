import { task } from "hardhat/config";
import { encodeFunctionData, isAddress, parseEventLogs, type Address } from "viem";

const MAX_PERFORMANCE_FEE_BPS = 10_000n;
const MIN_LOCK_DURATION = 1n;
const MIN_SETTLE_TIMELOCK = 1n;
const PHASE_NAMES = [
  "OPEN",
  "LOCKED",
  "SETTLE_PROPOSED",
  "SETTLED",
] as const;

function getEnumName(names: readonly string[], index: number): string {
  return names[index] ?? `UNKNOWN(${index})`;
}

type YieldVaultInitParams = {
  asset: Address;
  factory: Address;
  admin: Address;
  operator: Address;
  counterparty: Address;
  feeRecipient: Address;
  name: string;
  symbol: string;
  firstRound: {
    openWindow: bigint;
    lockDuration: bigint;
    settleTimelockWindow: bigint;
    roundCap: bigint;
    perAddressCap: bigint;
    minSubscription: bigint;
    performanceFeeBps: bigint;
    maxLossBps: bigint;
    openedAt: bigint;
  };
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

function getOptionalBigInt(
  cliValue: string | undefined,
  label: string,
): bigint | undefined {
  const rawValue = cliValue;

  if (rawValue === undefined || rawValue === "") {
    return undefined;
  }

  if (typeof rawValue === "bigint") {
    return rawValue;
  }

  if (typeof rawValue === "number") {
    if (!Number.isInteger(rawValue) || rawValue < 0) {
      throw new Error(`Invalid ${label}: ${rawValue}`);
    }

    return BigInt(rawValue);
  }

  if (typeof rawValue === "string") {
    if (rawValue.trim() === "") {
      throw new Error(`Invalid ${label}: empty string`);
    }

    const value = BigInt(rawValue);
    if (value < 0n) {
      throw new Error(`Invalid ${label}: ${rawValue}`);
    }

    return value;
  }

  throw new Error(`Invalid ${label}: ${String(rawValue)}`);
}

function getRequiredBigInt(
  cliValue: string | undefined,
  label: string,
): bigint {
  const value = getOptionalBigInt(cliValue, label);

  if (value === undefined) {
    throw new Error(`Missing required ${label}`);
  }

  return value;
}

function resolveCreateYieldVaultParams(
  taskArgs: {
    factory: string;
    asset: string | undefined;
    admin: string | undefined;
    operator: string | undefined;
    counterparty: string | undefined;
    feeRecipient: string | undefined;
    name: string | undefined;
    symbol: string | undefined;
    lockDuration: string | undefined;
    subscriptionWindow: string | undefined;
    epochCap: string | undefined;
    perAddressCap: string | undefined;
    minSubscription: string | undefined;
    performanceFeeBps: string | undefined;
    maxLossBps: string | undefined;
    settleTimelockWindow: string | undefined;
    openedAt: string | undefined;
  },
  canonicalTimelock: Address,
): YieldVaultInitParams {
  const factory = getRequiredAddress(taskArgs.factory, "factory address");
  const openWindow = getRequiredBigInt(
    taskArgs.subscriptionWindow,
    "subscriptionWindow",
  );
  const lockDuration = getRequiredBigInt(taskArgs.lockDuration, "lockDuration");
  const settleTimelockWindow = getRequiredBigInt(
    taskArgs.settleTimelockWindow,
    "settleTimelockWindow",
  );
  const roundCap = getRequiredBigInt(taskArgs.epochCap, "epochCap");
  const perAddressCap = getRequiredBigInt(taskArgs.perAddressCap, "perAddressCap");
  const minSubscription = getRequiredBigInt(taskArgs.minSubscription, "minSubscription");
  const performanceFeeBps = getRequiredBigInt(
    taskArgs.performanceFeeBps,
    "performanceFeeBps",
  );
  const maxLossBps = getRequiredBigInt(taskArgs.maxLossBps, "maxLossBps");

  if (
    openWindow === 0n ||
    lockDuration < MIN_LOCK_DURATION ||
    settleTimelockWindow < MIN_SETTLE_TIMELOCK ||
    roundCap === 0n ||
    minSubscription === 0n ||
    performanceFeeBps > MAX_PERFORMANCE_FEE_BPS ||
    maxLossBps > MAX_PERFORMANCE_FEE_BPS
  ) {
    throw new Error(
      "Invalid round params: subscriptionWindow/epochCap/minSubscription must be > 0, " +
      `lockDuration >= ${MIN_LOCK_DURATION}, settleTimelockWindow >= ${MIN_SETTLE_TIMELOCK}, ` +
      `performanceFeeBps/maxLossBps <= ${MAX_PERFORMANCE_FEE_BPS}`,
    );
  }

  return {
    asset: getRequiredAddress(taskArgs.asset, "asset address"),
    factory,
    admin: getOptionalAddress(taskArgs.admin, "admin address") ?? canonicalTimelock,
    operator: getRequiredAddress(taskArgs.operator, "operator address"),
    counterparty: getRequiredAddress(taskArgs.counterparty, "counterparty address"),
    feeRecipient: getRequiredAddress(taskArgs.feeRecipient, "feeRecipient address"),
    name: (() => {
      const value = taskArgs.name;

      if (value === undefined || value === "") {
        throw new Error("Missing required name");
      }

      return value;
    })(),
    symbol: (() => {
      const value = taskArgs.symbol;

      if (value === undefined || value === "") {
        throw new Error("Missing required symbol");
      }

      return value;
    })(),
    firstRound: {
      openWindow,
      lockDuration,
      settleTimelockWindow,
      roundCap,
      perAddressCap,
      minSubscription,
      performanceFeeBps,
      maxLossBps,
      openedAt: getOptionalBigInt(taskArgs.openedAt, "openedAt") ?? 0n,
    },
  };
}

export const yvfAddCounterpartyTask = task(
  "yvf:add-counterparty",
  "Add a counterparty to YieldVaultFactory whitelist",
)
  /**
   * Usage:
   * pnpm hardhat yvf:add-counterparty --network testnet 0xc096F8e4B1cc222899752a5504fDE557A147c57b <counterpartyAddress>
   *
   * Example:
   * pnpm hardhat yvf:add-counterparty --network testnet \
   *   0xFactoryProxyAddress \
   *   0xCounterpartyAddress
   */
  .addPositionalArgument({
    name: "factory",
    description: "YieldVaultFactory proxy address",
  })
  .addPositionalArgument({
    name: "counterparty",
    description: "Counterparty address to whitelist",
  })
  .setInlineAction(async () => {
    throw new Error("Deprecated yvf:add-counterparty: direct legacy administration is disabled. Use independently reviewed Timelock calldata and the R4 governance process.");
  })
  .build();

export const yvfRemoveCounterpartyTask = task(
  "yvf:remove-counterparty",
  "Remove a counterparty from YieldVaultFactory whitelist",
)
  /**
   * Usage:
   * pnpm hardhat yvf:remove-counterparty --network testnet 0xc096F8e4B1cc222899752a5504fDE557A147c57b <counterpartyAddress>
   *
   * Example:
   * pnpm hardhat yvf:remove-counterparty --network testnet \
   *   0xFactoryProxyAddress \
   *   0xCounterpartyAddress
   */
  .addPositionalArgument({
    name: "factory",
    description: "YieldVaultFactory proxy address",
  })
  .addPositionalArgument({
    name: "counterparty",
    description: "Counterparty address to remove",
  })
  .setInlineAction(async () => {
    throw new Error("Deprecated yvf:remove-counterparty: direct legacy administration is disabled. Use independently reviewed Timelock calldata and the R4 governance process.");
  })
  .build();

export const yvfListProxiesTask = task(
  "yvf:list-proxies",
  "List all YieldVault proxy addresses created by the factory",
)
  /**
   * Usage:
   * pnpm hardhat yvf:list-proxies --network testnet 0xc096F8e4B1cc222899752a5504fDE557A147c57b
   *
   * Example:
   * pnpm hardhat yvf:list-proxies --network testnet \
   *   0xc096F8e4B1cc222899752a5504fDE557A147c57b
   */
  .addPositionalArgument({
    name: "factory",
    description: "YieldVaultFactory proxy address",
  })
  .setInlineAction(async ({ factory }, hre) => {
    const connection = await hre.network.getOrCreate();

    try {
      const factoryAddress = getRequiredAddress(factory, "factory address");
      const factoryContract = await connection.viem.getContractAt(
        "YieldVaultFactory",
        factoryAddress,
      );
      const [totalProxies, proxies] = await Promise.all([
        factoryContract.read.totalProxies(),
        factoryContract.read.getAllProxies(),
      ]);

      console.log(`[yvf:list-proxies] factory=${factoryAddress}`);
      console.log(`[yvf:list-proxies] total=${totalProxies}`);

      if (proxies.length === 0) {
        console.log("[yvf:list-proxies] proxies=none");
        return;
      }

      proxies.forEach((proxy, index) => {
        console.log(`[yvf:list-proxies] proxy[${index}]=${proxy}`);
      });
    } finally {
      await connection.close();
    }
  })
  .build();

export const yvfCreateTask = task(
  "yvf:create",
  "Create a YieldVault from a factory proxy",
)
  /**
   * Usage:
   * pnpm hardhat yvf:create --network testnet 0xc096F8e4B1cc222899752a5504fDE557A147c57b \
   *   --asset 0x5Dc91D0b17f1c5c60cAF2eAA7D93840Ce488dbB4 \
   *   --admin <adminAddress> \
   *   --counterparty <counterpartyAddress> \
   *   --feeRecipient <feeRecipientAddress> \
   *   --name "Faroo Yield Vault 001" \
   *   --symbol "FYV001" \
   *   --lockDuration 2592000 \
   *   --subscriptionWindow 604800 \
   *   --epochCap 1000000000000000000000 \
   *   --perAddressCap 100000000000000000000 \
   *   --minSubscription 1000000000000000000 \
   *   --performanceFeeBps 1000 \
   *   --maxLossBps 5000 \
   *   --settleTimelockWindow 86400
   *
   * Notes:
   * - factory is the YieldVaultFactory proxy address.
   * - asset is usually the deployed stPROS proxy address.
   * - admin defaults to factory.timelock(); creation emits calldata for a Timelock operation.
   * - numeric values should be passed in raw integer form.
   * - lockDuration and settleTimelockWindow must each be at least 1 second.
   */
  .addPositionalArgument({
    name: "factory",
    description: "YieldVaultFactory proxy address",
  })
  .addOption({
    name: "asset",
    description: "stPROS asset address",
    defaultValue: "",
  })
  .addOption({
    name: "admin",
    description: "Vault admin address, must equal factory.timelock()",
    defaultValue: "",
  })
  .addOption({ name: "operator", description: "Distinct OPS address (required)", defaultValue: "" })
  .addOption({
    name: "counterparty",
    description: "Counterparty address",
    defaultValue: "",
  })
  .addOption({
    name: "feeRecipient",
    description: "Performance fee recipient address",
    defaultValue: "",
  })
  .addOption({
    name: "name",
    description: "Vault token name",
    defaultValue: "",
  })
  .addOption({
    name: "symbol",
    description: "Vault token symbol",
    defaultValue: "",
  })
  .addOption({
    name: "lockDuration",
    description: "Lock duration in seconds",
    defaultValue: "",
  })
  .addOption({
    name: "subscriptionWindow",
    description: "Open window in seconds",
    defaultValue: "",
  })
  .addOption({
    name: "epochCap",
    description: "Epoch cap in asset units",
    defaultValue: "",
  })
  .addOption({
    name: "perAddressCap",
    description: "Per-address subscription cap in asset units",
    defaultValue: "",
  })
  .addOption({
    name: "minSubscription",
    description: "Minimum subscription amount",
    defaultValue: "",
  })
  .addOption({
    name: "performanceFeeBps",
    description: "Performance fee in basis points",
    defaultValue: "",
  })
  .addOption({
    name: "maxLossBps",
    description: "Maximum loss in basis points",
    defaultValue: "",
  })
  .addOption({
    name: "settleTimelockWindow",
    description: "Settlement timelock window in seconds",
    defaultValue: "",
  })
  .addOption({
    name: "openedAt",
    description: "First round open period start timestamp; 0 opens immediately",
    defaultValue: "",
  })
  .setInlineAction(async (taskArgs, hre) => {
    const connection = await hre.network.getOrCreate();
    try {
      const factoryAddress = getRequiredAddress(taskArgs.factory, "factory address");
      const factory = await connection.viem.getContractAt("YieldVaultFactory", factoryAddress) as any;
      const timelock = await factory.read.timelock() as Address;
      const params = resolveCreateYieldVaultParams(
        taskArgs as Parameters<typeof resolveCreateYieldVaultParams>[0], timelock,
      );
      if (params.admin.toLowerCase() !== timelock.toLowerCase()) throw new Error("admin must equal factory.timelock()");
      if ([timelock, params.counterparty, params.feeRecipient, "0x0000000000000000000000000000000000000000"].some(a => a.toLowerCase() === params.operator.toLowerCase())) {
        throw new Error("operator must be nonzero and distinct from governance, counterparty and fee recipient");
      }
      const artifact = await hre.artifacts.readArtifact("YieldVaultFactory");
      console.log(JSON.stringify({timelock, target: factoryAddress, value: "0", data:
        encodeFunctionData({abi: artifact.abi, functionName: "createYieldVault", args: [params]})}, null, 2));
    } finally { await connection.close(); }
  })
  .build();

export const yvfEmergencyCancelTask = task(
  "yvf:emergency-cancel",
  "Emergency-cancel a YieldVault and enable principal redemption",
)
  /**
   * Usage:
   * pnpm hardhat yvf:emergency-cancel --network testnet <factoryAddress> <vaultAddress>
   *
   * Example:
   * pnpm hardhat yvf:emergency-cancel --network testnet \
   *   0xFactoryProxyAddress \
   *   0xVaultProxyAddress
   *
   * Notes:
   * - Callable only by the factory owner.
   * - Vault must have been created by the factory.
   * - Vault enters SETTLED without profit/loss; users can redeem principal pro-rata.
   * - Any funded settlement proposal is refunded before cancellation.
   */
  .addPositionalArgument({
    name: "factory",
    description: "YieldVaultFactory proxy address",
  })
  .addPositionalArgument({
    name: "vault",
    description: "YieldVault proxy address to emergency-cancel",
  })
  .setInlineAction(async () => {
    throw new Error("Deprecated yvf:emergency-cancel: direct legacy administration is disabled. Use independently reviewed Timelock calldata and the R4 governance process.");
  })
  .build();

export const yvfTransferOwnerTask = task(
  "yvf:transfer-owner",
  "Transfer YieldVaultFactory ownership to a new address",
)
  /**
   * Usage:
   * pnpm hardhat yvf:transfer-owner --network testnet <factoryAddress> <newOwnerAddress>
   *
   * Example:
   * pnpm hardhat yvf:transfer-owner --network testnet \
   *   0xFactoryProxyAddress \
   *   0xNewOwnerAddress
   *
   * Notes:
   * - Callable only by the current factory owner.
   * - Transfers Ownable permissions: createYieldVault, upgradeBeaconTo, emergencyCancel, etc.
   */
  .addPositionalArgument({
    name: "factory",
    description: "YieldVaultFactory proxy address",
  })
  .addPositionalArgument({
    name: "newOwner",
    description: "New owner address",
  })
  .setInlineAction(async () => {
    throw new Error("Release tooling required: legacy ownership handoff is disabled; use the reviewed multi-actor release builders. Proxy admin changes and ownership renunciation are not supported.");
  })
  .build();

export const yvfUpgradeBeaconTask = task(
  "yvf:upgrade-beacon",
  "Upgrade YieldVault beacon to a new implementation",
)
  /**
   * Usage:
   * pnpm hardhat yvf:upgrade-beacon --network testnet <factoryAddress> <implementationAddress>
   * 0x181b0b0f4fde940e9fa18ead2d813ff95cd15c49
   * Example:
   * pnpm hardhat yvf:upgrade-beacon --network testnet \
   *   0xFactoryProxyAddress \
   *   0xNewYieldVaultImplementationAddress
   *
   * Notes:
   * - Callable only by the factory owner.
   * - Upgrades all existing YieldVault beacon proxies to the new implementation.
   * - Deploy the new YieldVault implementation first, then pass its address here.
   */
  .addPositionalArgument({
    name: "factory",
    description: "YieldVaultFactory proxy address",
  })
  .addPositionalArgument({
    name: "implementation",
    description: "New YieldVault implementation address",
  })
  .setInlineAction(async () => {
    throw new Error("Release tooling required: implementation-only/proxy/beacon upgrades are disabled. Build a reviewed atomic Timelock operation; this legacy task cannot execute.");
  })
  .build();

/** R4 retirement is governance calldata only; this task never sends a transaction. */
export const yvfRetireTask = task("yvf:retire", "Build Timelock calldata for terminal vault retirement")
  .addPositionalArgument({name: "factory", description: "Factory proxy"})
  .addPositionalArgument({name: "vault", description: "Registered vault"})
  .setInlineAction(async (args, hre) => {
    const target = getRequiredAddress(args.factory, "factory");
    const vault = getRequiredAddress(args.vault, "vault");
    const artifact = await hre.artifacts.readArtifact("YieldVaultFactory");
    console.log(JSON.stringify({target, value: "0", data: encodeFunctionData({abi: artifact.abi, functionName: "retireVault", args: [vault]})}, null, 2));
  }).build();

export const yvfCloseCreationTask = task("yvf:close-creation", "Build Timelock calldata to permanently close factory creation")
  .addPositionalArgument({name: "factory", description: "Factory proxy"})
  .setInlineAction(async (args, hre) => {
    const target = getRequiredAddress(args.factory, "factory");
    const artifact = await hre.artifacts.readArtifact("YieldVaultFactory");
    console.log(JSON.stringify({target, value: "0", data: encodeFunctionData({abi: artifact.abi, functionName: "closeCreation"})}, null, 2));
  }).build();
