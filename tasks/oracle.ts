import { task } from "hardhat/config";
import { isAddress, type Address } from "viem";

import { Mainnet, TESTNET } from "../contants/index.js";

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

function getNetworkOracle(networkName: string): Address | undefined {
  if (networkName === "mainnet") {
    return Mainnet.ORACLE as Address;
  }

  if (networkName === "testnet") {
    return TESTNET.ORACLE as Address;
  }

  return undefined;
}

export const oracleSetPoolInfoTask = task(
  "oracle:set-pool-info",
  "Deprecated in R4; refuses before connecting to a network",
)
  .addPositionalArgument({
    name: "token",
    description: "Underlying token address used as the pool key",
  })
  .addPositionalArgument({
    name: "tokenAmount",
    description: "Token side of the conversion ratio, in raw units",
  })
  .addPositionalArgument({
    name: "vTokenAmount",
    description: "VToken side of the conversion ratio, in raw units",
  })
  .addOption({
    name: "oracle",
    description: "Oracle proxy address; defaults to contants/index.ts on mainnet/testnet",
    defaultValue: "",
  })
  .setInlineAction(async () => {
    throw new Error(
      "Deprecated oracle:set-pool-info: R4 removed setPoolInfo. Use reviewed Timelock setTokenAmount(token, expectedOldT, newT) calldata; this command cannot send a transaction.",
    );
  })
  .build();

export const oraclePoolInfoTask = task(
  "oracle:pool-info",
  "Read Oracle pool conversion rate for a token",
)
  /**
   * Usage:
   * pnpm hardhat oracle:pool-info --network mainnet <tokenAddress>
   */
  .addPositionalArgument({
    name: "token",
    description: "Underlying token address used as the pool key",
  })
  .addOption({
    name: "oracle",
    description: "Oracle proxy address; defaults to contants/index.ts on mainnet/testnet",
    defaultValue: "",
  })
  .setInlineAction(async (taskArgs, hre) => {
    const connection = await hre.network.getOrCreate();

    try {
      const token = getRequiredAddress(taskArgs.token, "token address");
      const oracleAddress =
        getOptionalAddress(taskArgs.oracle, "oracle address")
        ?? getNetworkOracle(connection.networkName);

      if (oracleAddress === undefined) {
        throw new Error("Missing oracle address. Pass --oracle or use mainnet/testnet.");
      }

      const oracleContract = await connection.viem.getContractAt("Oracle", oracleAddress);
      const [owner, paused, poolInfo] = await Promise.all([
        oracleContract.read.owner(),
        oracleContract.read.paused(),
        oracleContract.read.poolInfo([token]),
      ]);

      console.log(`[oracle:pool-info] oracle=${oracleAddress}`);
      console.log(`[oracle:pool-info] owner=${owner}`);
      console.log(`[oracle:pool-info] paused=${paused}`);
      console.log(`[oracle:pool-info] token=${token}`);
      console.log(`[oracle:pool-info] tokenAmount=${poolInfo[0]}`);
      console.log(`[oracle:pool-info] vTokenAmount=${poolInfo[1]}`);
    } finally {
      await connection.close();
    }
  })
  .build();
