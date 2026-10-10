import "dotenv/config";

import hardhatToolboxViemPlugin from "@nomicfoundation/hardhat-toolbox-viem";
import { configVariable, defineConfig } from "hardhat/config";
import HardhatDeploy from "hardhat-deploy";
import { appTasks } from "./tasks/index.js";

export default defineConfig({
  plugins: [HardhatDeploy, hardhatToolboxViemPlugin],
  tasks: appTasks,
  test: {
    solidity: {
      // Legacy bytecode fixtures and deployed-state snapshots are read-only test inputs.
      fsPermissions: { readDirectory: ["./test/fixtures", "./snapshots/deployed"] },
    },
  },
  solidity: {
    profiles: {
      default: {
        version: "0.8.28",
      },
      production: {
        version: "0.8.28",
        settings: {
          optimizer: {
            enabled: true,
            runs: 200,
          },
        },
      },
    },
    npmFilesToBuild: [
      "@openzeppelin/contracts/governance/TimelockController.sol",
      "@openzeppelin/contracts/proxy/transparent/ProxyAdmin.sol",
      "@openzeppelin/contracts/proxy/transparent/TransparentUpgradeableProxy.sol",
    ],
  },
  networks: {
    hardhatMainnet: {
      type: "edr-simulated",
      chainType: "l1",
    },
    hardhatOp: {
      type: "edr-simulated",
      chainType: "op",
    },
    sepolia: {
      type: "http",
      chainType: "l1",
      url: configVariable("SEPOLIA_RPC_URL"),
      accounts: [configVariable("SEPOLIA_PRIVATE_KEY")],
    },
    testnet: {
      type: "http",
      chainType: "l1",
      url: configVariable("PHAROS_TESTNET_RPC_URL"),
      accounts: [configVariable("TEST_PRIVATE_KEY")],
    },
    mainnet: {
      type: "http",
      chainType: "l1",
      url: configVariable("PHAROS_MAINNET_RPC_URL"),
      accounts: [configVariable("MAINNET_PRIVATE_KEY")],
    },
  }
});
