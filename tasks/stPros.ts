import { task } from "hardhat/config";
import { formatUnits, isAddress, type Address } from "viem";

const ERC20_METADATA_ABI = [
  {
    type: "function",
    name: "decimals",
    stateMutability: "view",
    inputs: [],
    outputs: [{ name: "", type: "uint8" }],
  },
] as const;

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

export const stProsPreviewDepositTask = task(
  "stpros:preview-deposit",
  "Preview StPROS shares minted for a deposit amount",
)
  /**
   * Usage:
   * pnpm hardhat stpros:preview-deposit --network testnet <stProsAddress> <assets>
   *
   * Example:
   * pnpm hardhat stpros:preview-deposit --network testnet \
 *   0x5Dc91D0b17f1c5c60cAF2eAA7D93840Ce488dbB4 \
   *   1000000000000000000
   *
   * Notes:
 * - Current asset on testnet: 0x838800b758277CC111B2d48Ab01e5E164f8E9471
   * - assets must be passed in raw asset units.
   * - 1000000000000000000 means 1 token when decimals = 18.
   */
  .addPositionalArgument({
    name: "stPros",
    description: "StPROS proxy address",
  })
  .addPositionalArgument({
    name: "assets",
    description: "Asset amount in raw units",
  })
  .setInlineAction(async ({ stPros, assets }, hre) => {
    const connection = await hre.network.getOrCreate();

    try {
      const publicClient = await connection.viem.getPublicClient();
      const stProsAddress = getRequiredAddress(stPros, "stPros address");
      const assetsAmount = getRequiredBigInt(assets, "assets amount");
      const stProsContract = await connection.viem.getContractAt("StPROS", stProsAddress);
      const assetAddress = await stProsContract.read.asset();
      const shareDecimals = await stProsContract.read.decimals();
      const assetDecimals = await publicClient.readContract({
        address: assetAddress,
        abi: ERC20_METADATA_ABI,
        functionName: "decimals",
      });
      const shares = await stProsContract.read.previewDeposit([assetsAmount]);

      console.log(`[stpros:preview-deposit] stPROS=${stProsAddress}`);
      console.log(`[stpros:preview-deposit] asset=${assetAddress}`);
      console.log(`[stpros:preview-deposit] assetsRaw=${assetsAmount}`);
      console.log(
        `[stpros:preview-deposit] assetsFormatted=${formatUnits(assetsAmount, assetDecimals)}`,
      );
      console.log(`[stpros:preview-deposit] sharesRaw=${shares}`);
      console.log(
        `[stpros:preview-deposit] sharesFormatted=${formatUnits(shares, shareDecimals)}`,
      );
    } finally {
      await connection.close();
    }
  })
  .build();

export const stProsPreviewWithdrawTask = task(
  "stpros:preview-withdraw",
  "Preview StPROS shares burned for a withdraw amount",
)
  /**
   * Usage:
   * pnpm hardhat stpros:preview-withdraw --network testnet <stProsAddress> <assets>
   *
   * Example:
   * pnpm hardhat stpros:preview-withdraw --network testnet \
 *   0x5Dc91D0b17f1c5c60cAF2eAA7D93840Ce488dbB4 \
   *   1000000000000000000
   *
   * Notes:
 * - Current asset on testnet: 0x838800b758277CC111B2d48Ab01e5E164f8E9471
   * - assets must be passed in raw asset units.
   * - 1000000000000000000 means 1 token when decimals = 18.
   */
  .addPositionalArgument({
    name: "stPros",
    description: "StPROS proxy address",
  })
  .addPositionalArgument({
    name: "assets",
    description: "Asset amount in raw units",
  })
  .setInlineAction(async ({ stPros, assets }, hre) => {
    const connection = await hre.network.getOrCreate();

    try {
      const publicClient = await connection.viem.getPublicClient();
      const stProsAddress = getRequiredAddress(stPros, "stPros address");
      const assetsAmount = getRequiredBigInt(assets, "assets amount");
      const stProsContract = await connection.viem.getContractAt("StPROS", stProsAddress);
      const assetAddress = await stProsContract.read.asset();
      const shareDecimals = await stProsContract.read.decimals();
      const assetDecimals = await publicClient.readContract({
        address: assetAddress,
        abi: ERC20_METADATA_ABI,
        functionName: "decimals",
      });
      const shares = await stProsContract.read.previewWithdraw([assetsAmount]);

      console.log(`[stpros:preview-withdraw] stPROS=${stProsAddress}`);
      console.log(`[stpros:preview-withdraw] asset=${assetAddress}`);
      console.log(`[stpros:preview-withdraw] assetsRaw=${assetsAmount}`);
      console.log(
        `[stpros:preview-withdraw] assetsFormatted=${formatUnits(assetsAmount, assetDecimals)}`,
      );
      console.log(`[stpros:preview-withdraw] sharesRaw=${shares}`);
      console.log(
        `[stpros:preview-withdraw] sharesFormatted=${formatUnits(shares, shareDecimals)}`,
      );
    } finally {
      await connection.close();
    }
  })
  .build();

export const stProsDepositWithProsTask = task(
  "stpros:deposit-with-pros",
  "Deposit native PROS into StPROS through depositWithPROS",
)
  /**
   * Usage:
   * pnpm hardhat stpros:deposit-with-pros --network testnet <stProsAddress> <amount>
   *
   * Example:
   * pnpm hardhat stpros:deposit-with-pros --network testnet \
   *   0x5Dc91D0b17f1c5c60cAF2eAA7D93840Ce488dbB4 \
   *   1000000000000000000
   *
   * Notes:
   * - amount must be passed in raw native-token units.
   * - 1000000000000000000 means 1 PROS when decimals = 18.
   * - The caller pays native PROS as transaction value.
   */
  .addPositionalArgument({
    name: "stPros",
    description: "StPROS proxy address",
  })
  .addPositionalArgument({
    name: "amount",
    description: "Native PROS amount in raw units",
  })
  .setInlineAction(async ({ stPros, amount }, hre) => {
    const connection = await hre.network.getOrCreate();

    try {
      const publicClient = await connection.viem.getPublicClient();
      const [signer] = await connection.viem.getWalletClients();

      if (signer?.account?.address === undefined) {
        throw new Error("No signer account available for the selected network");
      }

      const stProsAddress = getRequiredAddress(stPros, "stPros address");
      const amountRaw = getRequiredBigInt(amount, "amount");
      const stProsContract = await connection.viem.getContractAt("StPROS", stProsAddress, {
        client: {
          wallet: signer,
        },
      });
      const [assetAddress, shareDecimals, expectedShares] = await Promise.all([
        stProsContract.read.asset(),
        stProsContract.read.decimals(),
        stProsContract.read.previewDeposit([amountRaw]),
      ]);
      const assetDecimals = await publicClient.readContract({
        address: assetAddress,
        abi: ERC20_METADATA_ABI,
        functionName: "decimals",
      });
      const hash = await stProsContract.write.depositWithPROS({
        value: amountRaw,
      });

      await publicClient.waitForTransactionReceipt({ hash });

      console.log(`[stpros:deposit-with-pros] stPROS=${stProsAddress}`);
      console.log(`[stpros:deposit-with-pros] asset=${assetAddress}`);
      console.log(`[stpros:deposit-with-pros] caller=${signer.account.address}`);
      console.log(`[stpros:deposit-with-pros] amountRaw=${amountRaw}`);
      console.log(
        `[stpros:deposit-with-pros] amountFormatted=${formatUnits(amountRaw, assetDecimals)}`,
      );
      console.log(`[stpros:deposit-with-pros] expectedSharesRaw=${expectedShares}`);
      console.log(
        `[stpros:deposit-with-pros] expectedSharesFormatted=${formatUnits(expectedShares, shareDecimals)}`,
      );
      console.log(`[stpros:deposit-with-pros] txHash=${hash}`);
    } finally {
      await connection.close();
    }
  })
  .build();
