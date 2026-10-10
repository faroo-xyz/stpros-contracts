import { task } from "hardhat/config";
import { encodeFunctionData, formatUnits, isAddress, type Address } from "viem";

const ERC20_METADATA_ABI = [
  {
    type: "function",
    name: "decimals",
    stateMutability: "view",
    inputs: [],
    outputs: [{ name: "", type: "uint8" }],
  },
  {
    type: "function",
    name: "symbol",
    stateMutability: "view",
    inputs: [],
    outputs: [{ name: "", type: "string" }],
  },
] as const;

const ERC20_APPROVE_ABI = [
  {
    type: "function",
    name: "approve",
    stateMutability: "nonpayable",
    inputs: [
      { name: "spender", type: "address" },
      { name: "amount", type: "uint256" },
    ],
    outputs: [{ name: "", type: "bool" }],
  },
  {
    type: "function",
    name: "allowance",
    stateMutability: "view",
    inputs: [
      { name: "owner", type: "address" },
      { name: "spender", type: "address" },
    ],
    outputs: [{ name: "", type: "uint256" }],
  },
] as const;

const FACTORY_PAUSED_ABI = [
  {
    type: "function",
    name: "paused",
    stateMutability: "view",
    inputs: [],
    outputs: [{ name: "", type: "bool" }],
  },
] as const;

const MAX_PERFORMANCE_FEE_BPS = 10_000n;
const MIN_LOCK_DURATION = 1n;
const MIN_SETTLE_TIMELOCK = 1n;
const DEFAULT_ADMIN_ROLE =
  "0x0000000000000000000000000000000000000000000000000000000000000000";
const PHASE_NAMES = [
  "OPEN",
  "LOCKED",
  "SETTLE_PROPOSED",
  "SETTLED",
] as const;
const SETTLE_MODE_NAMES = ["PROFIT", "LOSS"] as const;

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
  if (value === undefined || value === "") {
    throw new Error(`Missing required ${label}`);
  }

  if (!isAddress(value)) {
    throw new Error(`Invalid ${label}: ${value}`);
  }

  return value;
}

function getRequiredBigInt(value: string | undefined, label: string): bigint {
  if (value === undefined || value.trim() === "") {
    throw new Error(`Missing required ${label}`);
  }

  return BigInt(value);
}

function getOptionalBigInt(value: string | undefined, label: string): bigint | undefined {
  if (value === undefined || value.trim() === "") {
    return undefined;
  }

  return BigInt(value);
}

function getEnumName(names: readonly string[], index: number): string {
  return names[index] ?? `UNKNOWN(${index})`;
}

type RoundParams = {
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

function resolveOpenNextRoundParams(taskArgs: {
  lockDuration: string | undefined;
  subscriptionWindow: string | undefined;
  epochCap: string | undefined;
  perAddressCap: string | undefined;
  minSubscription: string | undefined;
  performanceFeeBps: string | undefined;
  maxLossBps: string | undefined;
  settleTimelockWindow: string | undefined;
  openedAt: string | undefined;
}): RoundParams {
  const openWindow = getRequiredBigInt(taskArgs.subscriptionWindow, "subscriptionWindow");
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
    openWindow,
    lockDuration,
    settleTimelockWindow,
    roundCap,
    perAddressCap,
    minSubscription,
    performanceFeeBps,
    maxLossBps,
    openedAt: getOptionalBigInt(taskArgs.openedAt, "openedAt") ?? 0n,
  };
}

export const yieldVaultInfoTask = task(
  "yv:info",
  "Query core YieldVault state and configuration",
)
  /**
   * Usage:
   * pnpm hardhat yv:info --network testnet <vaultAddress>
   *
   * Example:
   * pnpm hardhat yv:info --network testnet \
   *   0x422afe88191dE8df3b30852E6aa166250E7AA8D1
   */
  .addPositionalArgument({
    name: "vault",
    description: "YieldVault proxy address",
  })
  .setInlineAction(async ({ vault }, hre) => {
    const connection = await hre.network.getOrCreate();

    try {
      const publicClient = await connection.viem.getPublicClient();
      const vaultAddress = getRequiredAddress(vault, "vault address");
      const vaultContract = await connection.viem.getContractAt(
        "YieldVault",
        vaultAddress,
      ) as any;

      const [
        name,
        symbol,
        shareDecimals,
        assetAddress,
        factory,
        counterparty,
        feeRecipient,
        roundIndex,
        vaultClosed,
        openWindow,
        lockDuration,
        roundCap,
        perAddressCap,
        minSubscription,
        performanceFeeBps,
        settleTimelockWindow,
        yieldTarget,
        phase,
        settleMode,
        openedAt,
        openDeadline,
        lockedAt,
        settleProposedAt,
        settledAt,
        settleAmount,
        profitFunded,
        profitFunder,
        totalManagedAssets,
        totalSupply,
        totalAssets,
      ] = await Promise.all([
        vaultContract.read.name(),
        vaultContract.read.symbol(),
        vaultContract.read.decimals(),
        vaultContract.read.asset(),
        vaultContract.read.factory(),
        vaultContract.read.counterparty(),
        vaultContract.read.feeRecipient(),
        vaultContract.read.roundIndex(),
        vaultContract.read.vaultClosed(),
        vaultContract.read.openWindow(),
        vaultContract.read.lockDuration(),
        vaultContract.read.roundCap(),
        vaultContract.read.perAddressCap(),
        vaultContract.read.minSubscription(),
        vaultContract.read.performanceFeeBps(),
        vaultContract.read.settleTimelockWindow(),
        vaultContract.read.yieldTarget(),
        vaultContract.read.phase(),
        vaultContract.read.settleMode(),
        vaultContract.read.openedAt(),
        vaultContract.read.openDeadline(),
        vaultContract.read.lockedAt(),
        vaultContract.read.settleProposedAt(),
        vaultContract.read.settledAt(),
        vaultContract.read.settleAmount(),
        vaultContract.read.profitFunded(),
        vaultContract.read.profitFunder(),
        vaultContract.read.totalManagedAssets(),
        vaultContract.read.totalSupply(),
        vaultContract.read.totalAssets(),
      ]);

      const [assetDecimals, assetSymbol, factoryPaused, block] = await Promise.all([
        publicClient.readContract({
          address: assetAddress,
          abi: ERC20_METADATA_ABI,
          functionName: "decimals",
        }),
        publicClient.readContract({
          address: assetAddress,
          abi: ERC20_METADATA_ABI,
          functionName: "symbol",
        }),
        publicClient.readContract({
          address: factory,
          abi: FACTORY_PAUSED_ABI,
          functionName: "paused",
        }),
        publicClient.getBlock(),
      ]);

      console.log(`[yv:info] vault=${vaultAddress}`);
      console.log(`[yv:info] name=${name}`);
      console.log(`[yv:info] symbol=${symbol}`);
      console.log(`[yv:info] shareDecimals=${shareDecimals}`);
      console.log(`[yv:info] asset=${assetAddress}`);
      console.log(`[yv:info] assetSymbol=${assetSymbol}`);
      console.log(`[yv:info] assetDecimals=${assetDecimals}`);
      console.log(`[yv:info] factory=${factory}`);
      console.log(`[yv:info] factoryPaused=${factoryPaused}`);
      console.log(`[yv:info] counterparty=${counterparty}`);
      console.log(`[yv:info] feeRecipient=${feeRecipient}`);
      console.log(`[yv:info] roundIndex=${roundIndex}`);
      console.log(`[yv:info] phase=${phase} (${getEnumName(PHASE_NAMES, Number(phase))})`);
      console.log(`[yv:info] vaultClosed=${vaultClosed}`);
      console.log(
        `[yv:info] settleMode=${settleMode} (${getEnumName(SETTLE_MODE_NAMES, Number(settleMode))})`,
      );
      console.log(`[yv:info] currentTimestamp=${block.timestamp}`);
      console.log(`[yv:info] openedAt=${openedAt}`);
      console.log(`[yv:info] openDeadline=${openDeadline}`);
      console.log(`[yv:info] lockedAt=${lockedAt}`);
      console.log(`[yv:info] settleProposedAt=${settleProposedAt}`);
      console.log(`[yv:info] settledAt=${settledAt}`);
      console.log(`[yv:info] openWindow=${openWindow}`);
      console.log(`[yv:info] lockDuration=${lockDuration}`);
      console.log(`[yv:info] settleTimelockWindow=${settleTimelockWindow}`);
      console.log(`[yv:info] roundCapRaw=${roundCap}`);
      console.log(`[yv:info] roundCapFormatted=${formatUnits(roundCap, assetDecimals)}`);
      console.log(`[yv:info] perAddressCapRaw=${perAddressCap}`);
      console.log(
        `[yv:info] perAddressCapFormatted=${formatUnits(perAddressCap, assetDecimals)}`,
      );
      console.log(`[yv:info] minSubscriptionRaw=${minSubscription}`);
      console.log(
        `[yv:info] minSubscriptionFormatted=${formatUnits(minSubscription, assetDecimals)}`,
      );
      console.log(`[yv:info] performanceFeeBps=${performanceFeeBps}`);
      console.log(`[yv:info] profitFunded=${profitFunded}`);
      console.log(`[yv:info] profitFunder=${profitFunder}`);
      console.log(`[yv:info] settleAmountRaw=${settleAmount}`);
      console.log(
        `[yv:info] settleAmountFormatted=${formatUnits(settleAmount, assetDecimals)}`,
      );
      console.log(`[yv:info] yieldTarget=${yieldTarget}`);
      console.log(`[yv:info] totalManagedAssetsRaw=${totalManagedAssets}`);
      console.log(
        `[yv:info] totalManagedAssetsFormatted=${formatUnits(totalManagedAssets, assetDecimals)}`,
      );
      console.log(`[yv:info] totalSupplyRaw=${totalSupply}`);
      console.log(`[yv:info] totalSupplyFormatted=${formatUnits(totalSupply, shareDecimals)}`);
      console.log(`[yv:info] totalAssetsRaw=${totalAssets}`);
      console.log(
        `[yv:info] totalAssetsFormatted=${formatUnits(totalAssets, assetDecimals)}`,
      );
    } finally {
      await connection.close();
    }
  })
  .build();

export const yieldVaultAccountTask = task(
  "yv:account",
  "Query a user account against a YieldVault",
)
  /**
   * Usage:
   * pnpm hardhat yv:account --network testnet <vaultAddress> <accountAddress>
   *
   * Example:
   * pnpm hardhat yv:account --network testnet \
   *   0x422afe88191dE8df3b30852E6aa166250E7AA8D1 \
   *   0xYourAccountAddress
   */
  .addPositionalArgument({
    name: "vault",
    description: "YieldVault proxy address",
  })
  .addPositionalArgument({
    name: "account",
    description: "User address",
  })
  .setInlineAction(async ({ vault, account }, hre) => {
    const connection = await hre.network.getOrCreate();

    try {
      const publicClient = await connection.viem.getPublicClient();
      const vaultAddress = getRequiredAddress(vault, "vault address");
      const accountAddress = getRequiredAddress(account, "account address");
      const vaultContract = await connection.viem.getContractAt(
        "YieldVault",
        vaultAddress,
      );
      const [assetAddress, shareDecimals] = await Promise.all([
        vaultContract.read.asset(),
        vaultContract.read.decimals(),
      ]);
      const [assetDecimals, assetSymbol] = await Promise.all([
        publicClient.readContract({
          address: assetAddress,
          abi: ERC20_METADATA_ABI,
          functionName: "decimals",
        }),
        publicClient.readContract({
          address: assetAddress,
          abi: ERC20_METADATA_ABI,
          functionName: "symbol",
        }),
      ]);
      const [
        shareBalance,
        accountAssets,
        maxDeposit,
        maxMint,
        maxRedeem,
        maxWithdraw,
        isAdmin,
      ] = await Promise.all([
        vaultContract.read.balanceOf([accountAddress]),
        vaultContract.read.convertToAssets([await vaultContract.read.balanceOf([accountAddress])]),
        vaultContract.read.maxDeposit([accountAddress]),
        vaultContract.read.maxMint([accountAddress]),
        vaultContract.read.maxRedeem([accountAddress]),
        vaultContract.read.maxWithdraw([accountAddress]),
        vaultContract.read.hasRole([DEFAULT_ADMIN_ROLE, accountAddress]),
      ]);

      console.log(`[yv:account] vault=${vaultAddress}`);
      console.log(`[yv:account] account=${accountAddress}`);
      console.log(`[yv:account] asset=${assetAddress}`);
      console.log(`[yv:account] assetSymbol=${assetSymbol}`);
      console.log(`[yv:account] shareBalanceRaw=${shareBalance}`);
      console.log(
        `[yv:account] shareBalanceFormatted=${formatUnits(shareBalance, shareDecimals)}`,
      );
      console.log(`[yv:account] accountAssetsRaw=${accountAssets}`);
      console.log(
        `[yv:account] accountAssetsFormatted=${formatUnits(accountAssets, assetDecimals)}`,
      );
      console.log(`[yv:account] maxDepositRaw=${maxDeposit}`);
      console.log(
        `[yv:account] maxDepositFormatted=${formatUnits(maxDeposit, assetDecimals)}`,
      );
      console.log(`[yv:account] maxMintRaw=${maxMint}`);
      console.log(`[yv:account] maxMintFormatted=${formatUnits(maxMint, shareDecimals)}`);
      console.log(`[yv:account] maxRedeemRaw=${maxRedeem}`);
      console.log(
        `[yv:account] maxRedeemFormatted=${formatUnits(maxRedeem, shareDecimals)}`,
      );
      console.log(`[yv:account] maxWithdrawRaw=${maxWithdraw}`);
      console.log(
        `[yv:account] maxWithdrawFormatted=${formatUnits(maxWithdraw, assetDecimals)}`,
      );
      console.log(`[yv:account] defaultAdmin=${isAdmin}`);
    } finally {
      await connection.close();
    }
  })
  .build();

export const yieldVaultPreviewDepositTask = task(
  "yv:preview-deposit",
  "Preview YieldVault shares minted for an asset deposit",
)
  /**
   * Usage:
   * pnpm hardhat yv:preview-deposit --network testnet <vaultAddress> <assets>
   *
   * Example:
   * pnpm hardhat yv:preview-deposit --network testnet \
   *   0x422afe88191dE8df3b30852E6aa166250E7AA8D1 \
   *   1000000000000000000
   */
  .addPositionalArgument({
    name: "vault",
    description: "YieldVault proxy address",
  })
  .addPositionalArgument({
    name: "assets",
    description: "Asset amount in raw units",
  })
  .setInlineAction(async ({ vault, assets }, hre) => {
    const connection = await hre.network.getOrCreate();

    try {
      const publicClient = await connection.viem.getPublicClient();
      const vaultAddress = getRequiredAddress(vault, "vault address");
      const assetsAmount = getRequiredBigInt(assets, "assets amount");
      const vaultContract = await connection.viem.getContractAt(
        "YieldVault",
        vaultAddress,
      );
      const [assetAddress, shareDecimals] = await Promise.all([
        vaultContract.read.asset(),
        vaultContract.read.decimals(),
      ]);
      const assetDecimals = await publicClient.readContract({
        address: assetAddress,
        abi: ERC20_METADATA_ABI,
        functionName: "decimals",
      });
      const shares = await vaultContract.read.previewDeposit([assetsAmount]);

      console.log(`[yv:preview-deposit] vault=${vaultAddress}`);
      console.log(`[yv:preview-deposit] asset=${assetAddress}`);
      console.log(`[yv:preview-deposit] assetsRaw=${assetsAmount}`);
      console.log(
        `[yv:preview-deposit] assetsFormatted=${formatUnits(assetsAmount, assetDecimals)}`,
      );
      console.log(`[yv:preview-deposit] sharesRaw=${shares}`);
      console.log(
        `[yv:preview-deposit] sharesFormatted=${formatUnits(shares, shareDecimals)}`,
      );
    } finally {
      await connection.close();
    }
  })
  .build();

export const yieldVaultPreviewWithdrawTask = task(
  "yv:preview-withdraw",
  "Preview YieldVault shares burned for an asset withdraw",
)
  /**
   * Usage:
   * pnpm hardhat yv:preview-withdraw --network testnet <vaultAddress> <assets>
   *
   * Example:
   * pnpm hardhat yv:preview-withdraw --network testnet \
   *   0x422afe88191dE8df3b30852E6aa166250E7AA8D1 \
   *   1000000000000000000
   */
  .addPositionalArgument({
    name: "vault",
    description: "YieldVault proxy address",
  })
  .addPositionalArgument({
    name: "assets",
    description: "Asset amount in raw units",
  })
  .setInlineAction(async ({ vault, assets }, hre) => {
    const connection = await hre.network.getOrCreate();

    try {
      const publicClient = await connection.viem.getPublicClient();
      const vaultAddress = getRequiredAddress(vault, "vault address");
      const assetsAmount = getRequiredBigInt(assets, "assets amount");
      const vaultContract = await connection.viem.getContractAt(
        "YieldVault",
        vaultAddress,
      );
      const [assetAddress, shareDecimals] = await Promise.all([
        vaultContract.read.asset(),
        vaultContract.read.decimals(),
      ]);
      const assetDecimals = await publicClient.readContract({
        address: assetAddress,
        abi: ERC20_METADATA_ABI,
        functionName: "decimals",
      });
      const shares = await vaultContract.read.previewWithdraw([assetsAmount]);

      console.log(`[yv:preview-withdraw] vault=${vaultAddress}`);
      console.log(`[yv:preview-withdraw] asset=${assetAddress}`);
      console.log(`[yv:preview-withdraw] assetsRaw=${assetsAmount}`);
      console.log(
        `[yv:preview-withdraw] assetsFormatted=${formatUnits(assetsAmount, assetDecimals)}`,
      );
      console.log(`[yv:preview-withdraw] sharesRaw=${shares}`);
      console.log(
        `[yv:preview-withdraw] sharesFormatted=${formatUnits(shares, shareDecimals)}`,
      );
    } finally {
      await connection.close();
    }
  })
  .build();

export const yieldVaultApproveTask = task(
  "yv:approve",
  "Approve a YieldVault to spend its underlying asset",
)
  /**
   * Usage:
   * pnpm hardhat yv:approve --network testnet <vaultAddress> <amount>
   *
   * Example:
   * pnpm hardhat yv:approve --network testnet \
   *   0x422afe88191dE8df3b30852E6aa166250E7AA8D1 \
   *   1000000000000000000
   *
   * Notes:
   * - amount must be passed in raw asset units.
   * - The spender is the vault address itself.
   * - Call this before yv:deposit.
   */
  .addPositionalArgument({
    name: "vault",
    description: "YieldVault proxy address",
  })
  .addPositionalArgument({
    name: "amount",
    description: "Approval amount in raw units",
  })
  .setInlineAction(async ({ vault, amount }, hre) => {
    const connection = await hre.network.getOrCreate();

    try {
      const publicClient = await connection.viem.getPublicClient();
      const [signer] = await connection.viem.getWalletClients();

      if (signer?.account?.address === undefined) {
        throw new Error("No signer account available for the selected network");
      }

      const vaultAddress = getRequiredAddress(vault, "vault address");
      const amountRaw = getRequiredBigInt(amount, "amount");
      const vaultContract = await connection.viem.getContractAt("YieldVault", vaultAddress);
      const assetAddress = await vaultContract.read.asset();
      const [assetDecimals, assetSymbol, allowanceBefore] = await Promise.all([
        publicClient.readContract({
          address: assetAddress,
          abi: ERC20_METADATA_ABI,
          functionName: "decimals",
        }),
        publicClient.readContract({
          address: assetAddress,
          abi: ERC20_METADATA_ABI,
          functionName: "symbol",
        }),
        publicClient.readContract({
          address: assetAddress,
          abi: ERC20_APPROVE_ABI,
          functionName: "allowance",
          args: [signer.account.address, vaultAddress],
        }),
      ]);
      const hash = await signer.writeContract({
        address: assetAddress,
        abi: ERC20_APPROVE_ABI,
        functionName: "approve",
        args: [vaultAddress, amountRaw],
        account: signer.account,
      });

      await publicClient.waitForTransactionReceipt({ hash });

      const allowanceAfter = await publicClient.readContract({
        address: assetAddress,
        abi: ERC20_APPROVE_ABI,
        functionName: "allowance",
        args: [signer.account.address, vaultAddress],
      });

      console.log(`[yv:approve] vault=${vaultAddress}`);
      console.log(`[yv:approve] asset=${assetAddress}`);
      console.log(`[yv:approve] assetSymbol=${assetSymbol}`);
      console.log(`[yv:approve] owner=${signer.account.address}`);
      console.log(`[yv:approve] spender=${vaultAddress}`);
      console.log(`[yv:approve] amountRaw=${amountRaw}`);
      console.log(`[yv:approve] amountFormatted=${formatUnits(amountRaw, assetDecimals)}`);
      console.log(`[yv:approve] allowanceBeforeRaw=${allowanceBefore}`);
      console.log(
        `[yv:approve] allowanceBeforeFormatted=${formatUnits(allowanceBefore, assetDecimals)}`,
      );
      console.log(`[yv:approve] allowanceAfterRaw=${allowanceAfter}`);
      console.log(
        `[yv:approve] allowanceAfterFormatted=${formatUnits(allowanceAfter, assetDecimals)}`,
      );
      console.log(`[yv:approve] txHash=${hash}`);
    } finally {
      await connection.close();
    }
  })
  .build();

export const yieldVaultDepositTask = task(
  "yv:deposit",
  "Deposit asset into a YieldVault",
)
  /**
   * Usage:
   * pnpm hardhat yv:deposit --network testnet <vaultAddress> <assets> [--receiver <receiverAddress>]
   *
   * Example:
   * pnpm hardhat yv:deposit --network testnet \
   *   0x422afe88191dE8df3b30852E6aa166250E7AA8D1 \
   *   1000000000000000000 \
   *   --receiver 0xYourReceiverAddress
   *
   * Notes:
   * - assets must be passed in raw asset units.
   * - The caller must approve the vault to spend the asset before calling deposit.
   * - receiver defaults to the current signer if omitted.
   */
  .addPositionalArgument({
    name: "vault",
    description: "YieldVault proxy address",
  })
  .addPositionalArgument({
    name: "assets",
    description: "Asset amount in raw units",
  })
  .addOption({
    name: "receiver",
    description: "Receiver address, defaults to signer",
    defaultValue: "",
  })
  .setInlineAction(async ({ vault, assets, receiver }, hre) => {
    const connection = await hre.network.getOrCreate();

    try {
      const publicClient = await connection.viem.getPublicClient();
      const [signer] = await connection.viem.getWalletClients();

      if (signer?.account?.address === undefined) {
        throw new Error("No signer account available for the selected network");
      }

      const vaultAddress = getRequiredAddress(vault, "vault address");
      const assetsAmount = getRequiredBigInt(assets, "assets amount");
      const receiverAddress =
        getOptionalAddress(receiver, "receiver address") ?? signer.account.address;
      const vaultContract = await connection.viem.getContractAt("YieldVault", vaultAddress, {
        client: {
          wallet: signer,
        },
      });
      const [assetAddress, shareDecimals, expectedShares] = await Promise.all([
        vaultContract.read.asset(),
        vaultContract.read.decimals(),
        vaultContract.read.previewDeposit([assetsAmount]),
      ]);
      const [assetDecimals, assetSymbol] = await Promise.all([
        publicClient.readContract({
          address: assetAddress,
          abi: ERC20_METADATA_ABI,
          functionName: "decimals",
        }),
        publicClient.readContract({
          address: assetAddress,
          abi: ERC20_METADATA_ABI,
          functionName: "symbol",
        }),
      ]);
      const hash = await vaultContract.write.deposit([assetsAmount, receiverAddress]);

      await publicClient.waitForTransactionReceipt({ hash });

      console.log(`[yv:deposit] vault=${vaultAddress}`);
      console.log(`[yv:deposit] asset=${assetAddress}`);
      console.log(`[yv:deposit] assetSymbol=${assetSymbol}`);
      console.log(`[yv:deposit] caller=${signer.account.address}`);
      console.log(`[yv:deposit] receiver=${receiverAddress}`);
      console.log(`[yv:deposit] assetsRaw=${assetsAmount}`);
      console.log(`[yv:deposit] assetsFormatted=${formatUnits(assetsAmount, assetDecimals)}`);
      console.log(`[yv:deposit] expectedSharesRaw=${expectedShares}`);
      console.log(
        `[yv:deposit] expectedSharesFormatted=${formatUnits(expectedShares, shareDecimals)}`,
      );
      console.log(`[yv:deposit] txHash=${hash}`);
    } finally {
      await connection.close();
    }
  })
  .build();

export const yieldVaultOpenNextRoundTask = task(
  "yv:open-next-round",
  "Open the next YieldVault round after settlement",
)
  /**
   * Usage:
   * pnpm hardhat yv:open-next-round --network testnet <vaultAddress> \
   *   --lockDuration 2592000 \
   *   --subscriptionWindow 604800 \
   *   --epochCap 1000000000000000000000 \
   *   --perAddressCap 100000000000000000000 \
   *   --minSubscription 1000000000000000000 \
   *   --performanceFeeBps 1000 \
   *   --maxLossBps 5000 \
   *   --settleTimelockWindow 86400
   *
   * Example:
   * pnpm hardhat yv:open-next-round --network testnet \
   *   0x422afe88191dE8df3b30852E6aa166250E7AA8D1 \
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
   * - Callable only by vault OPERATOR_ROLE.
   * - Vault must be in SETTLED phase.
   * - epochCap must be >= current totalManagedAssets.
   * - numeric values should be passed in raw integer form.
   */
  .addPositionalArgument({
    name: "vault",
    description: "YieldVault proxy address",
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
    description: "Round cap in asset units",
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
    description: "Open period start timestamp; 0 opens immediately",
    defaultValue: "",
  })
  .setInlineAction(async (taskArgs, hre) => {
    const connection = await hre.network.getOrCreate();

    try {
      const publicClient = await connection.viem.getPublicClient();
      const [signer] = await connection.viem.getWalletClients();

      if (signer?.account?.address === undefined) {
        throw new Error("No signer account available for the selected network");
      }

      const vaultAddress = getRequiredAddress(taskArgs.vault, "vault address");
      const params = resolveOpenNextRoundParams(
        taskArgs as Parameters<typeof resolveOpenNextRoundParams>[0],
      );
      const vaultContract = await connection.viem.getContractAt("YieldVault", vaultAddress, {
        client: {
          wallet: signer,
        },
      });
      const [previousPhase, previousRoundIndex, totalManagedAssets] = await Promise.all([
        vaultContract.read.phase(),
        vaultContract.read.roundIndex(),
        vaultContract.read.totalManagedAssets(),
      ]);

      if (previousPhase !== 3) {
        throw new Error(
          `Vault must be SETTLED before opening the next round; current phase=${previousPhase} (${getEnumName(PHASE_NAMES, Number(previousPhase))})`,
        );
      }

      if (params.roundCap < totalManagedAssets) {
        throw new Error(
          `epochCap (${params.roundCap}) must be >= totalManagedAssets (${totalManagedAssets})`,
        );
      }

      const hash = await vaultContract.write.openNextRound([params]);
      const receipt = await publicClient.waitForTransactionReceipt({ hash });
      const [
        roundIndex,
        phase,
        openedAt,
        openDeadline,
        roundCap,
      ] = await Promise.all([
        vaultContract.read.roundIndex(),
        vaultContract.read.phase(),
        vaultContract.read.openedAt(),
        vaultContract.read.openDeadline(),
        vaultContract.read.roundCap(),
      ]);

      console.log(`[yv:open-next-round] vault=${vaultAddress}`);
      console.log(`[yv:open-next-round] caller=${signer.account.address}`);
      console.log(`[yv:open-next-round] previousRoundIndex=${previousRoundIndex}`);
      console.log(`[yv:open-next-round] roundIndex=${roundIndex}`);
      console.log(
        `[yv:open-next-round] phase=${phase} (${getEnumName(PHASE_NAMES, Number(phase))})`,
      );
      console.log(`[yv:open-next-round] openWindow=${params.openWindow}`);
      console.log(`[yv:open-next-round] lockDuration=${params.lockDuration}`);
      console.log(`[yv:open-next-round] settleTimelockWindow=${params.settleTimelockWindow}`);
      console.log(`[yv:open-next-round] roundCapRaw=${roundCap}`);
      console.log(`[yv:open-next-round] perAddressCapRaw=${params.perAddressCap}`);
      console.log(`[yv:open-next-round] minSubscriptionRaw=${params.minSubscription}`);
      console.log(`[yv:open-next-round] performanceFeeBps=${params.performanceFeeBps}`);
      console.log(`[yv:open-next-round] maxLossBps=${params.maxLossBps}`);
      console.log(`[yv:open-next-round] scheduledOpenedAt=${params.openedAt}`);
      console.log(`[yv:open-next-round] openedAt=${openedAt}`);
      console.log(`[yv:open-next-round] openDeadline=${openDeadline}`);
      console.log(`[yv:open-next-round] txHash=${hash}`);
      console.log(`[yv:open-next-round] blockNumber=${receipt.blockNumber}`);
    } finally {
      await connection.close();
    }
  })
  .build();

/** LOSS is authorized only when executed by factory.timelock(); never submit directly. */
export const yieldVaultLossCalldataTask = task("yield-vault:loss-calldata", "Build Timelock LOSS proposal calldata")
  .addPositionalArgument({name: "vault", description: "YieldVault proxy"})
  .addPositionalArgument({name: "amount", description: "Loss amount in raw asset units"})
  .setInlineAction(async (args, hre) => {
    const target = getRequiredAddress(args.vault, "vault");
    const amount = BigInt(args.amount);
    if (amount < 0n) throw new Error("amount must be nonnegative");
    const artifact = await hre.artifacts.readArtifact("YieldVault");
    console.log(JSON.stringify({target, value: "0", data: encodeFunctionData({abi: artifact.abi,
      functionName: "proposeSettlement", args: [1, amount, "0x0000000000000000000000000000000000000000"]})}, null, 2));
  }).build();
