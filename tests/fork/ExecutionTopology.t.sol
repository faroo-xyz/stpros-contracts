// SPDX-License-Identifier: GPL-3.0
pragma solidity ^0.8.28;

import {Vm} from "forge-std/Vm.sol";
import {LegacyTopology, IWETH9Like} from "../helpers/LegacyTopology.sol";
import {OracleV3MigrationTest} from "../protocol/OracleV3Migration.t.sol";
import {
    ILegacyProxyAdminV4,
    ILegacyStPROSV1,
    ILegacyYieldVaultFactoryV1,
    ILegacyYieldVaultV1,
    LegacyYieldVaultV1Types,
    ILegacyYieldVaultFactoryV3,
    ILegacyYieldVaultV3,
    LegacyYieldVaultV3Types
} from "../helpers/LegacyInterfaces.sol";
import {SyntheticSafeSingleton, SyntheticSafeProxy, SyntheticMultiSendCallOnly} from "./synthetic/SyntheticSafe.sol";

/// @notice Writes a pre-P-0 local state for the execution driver (`tests/fork/driver/execution.py`):
///         the `vm.dumpState` account map and a metadata file with every address, actor and the
///         complete log history the fixture produced (abi-encoded `Vm.Log[]`), which the driver uses
///         as the synthetic history (a dumped state carries no logs).
/// @dev SYNTHETIC, class C. Recorded legacy runtimes from `tests/fixtures/legacy/` run the protocol;
///      every balance, holder, round, role and the GOV Safe are synthetic. E1/E2 are the real
///      addresses used as impersonated labels only. Nothing here is deployed, signed or broadcast,
///      and no Timelock, R4 implementation or assertion contract exists yet: P-0 is the driver's job.
library ExecutionExport {
    Vm internal constant VM = Vm(address(uint160(uint256(keccak256("hevm cheat code")))));
    string internal constant DIR = "cache_forge/rehearsal/fork-campaign/execution/";

    function finish(string memory name, string memory meta) internal {
        Vm.Log[] memory logs = VM.getRecordedLogs();
        string memory json = VM.serializeBytes(meta, "logs", abi.encode(logs));
        VM.createDir(DIR, true);
        VM.writeJson(json, string.concat(DIR, name, ".json"));
        VM.dumpState(string.concat(DIR, name, "-state.json"));
    }
}

contract ExecutionMainnetTopology is LegacyTopology {
    address internal constant E1 = 0x4597C97a43dFBb4a398E2b16AA9cE61f90d801DD;
    address internal constant E2 = 0xb29eaEA575bd4bae7d9a301446e6204C3A23AdF1;
    bytes32 internal constant OPERATOR = keccak256("OPERATOR_ROLE");
    string internal constant META = "execution-mainnet-v1";

    LegacyV1 internal core;
    LegacyVaultsV1 internal main;
    address internal factoryTestImpl;
    ILegacyYieldVaultFactoryV1 internal factoryTest;
    address internal vaultImplTest;
    ILegacyYieldVaultV1 internal vault001;
    ILegacyYieldVaultV1 internal testVault;
    address internal safe;
    address internal singleton;
    address internal multiSend;
    address[] internal owners;
    address[] internal users;
    address internal creationAdmin = makeAddr("mainnet.creationAdmin");
    address internal op001 = makeAddr("mainnet.vault001.operator");
    address internal opTest = makeAddr("mainnet.testVault.operator");
    address internal adminTest2 = makeAddr("mainnet.testVault.extraAdmin");
    address internal cpMain = makeAddr("mainnet.counterparty.main");
    address internal cpTest = makeAddr("mainnet.counterparty.test");

    function _safe() internal {
        for (uint256 i; i < 5; ++i) owners.push(makeAddr(string.concat("mainnet.gov.owner", vm.toString(i))));
        singleton = address(new SyntheticSafeSingleton());
        safe = address(new SyntheticSafeProxy(singleton));
        SyntheticSafeSingleton(payable(safe)).setup(owners, 3);
        multiSend = address(new SyntheticMultiSendCallOnly());
    }

    function _testFactory() internal {
        factoryTestImpl = _deployRecorded("mainnet", "YieldVaultFactory_V1");
        factoryTest = ILegacyYieldVaultFactoryV1(
            _deployTransparentProxyV4(
                "mainnet", factoryTestImpl, core.proxyAdmin, abi.encodeCall(ILegacyYieldVaultFactoryV1.initialize, (E1))
            )
        );
        vaultImplTest = _deployRecorded("mainnet", "YieldVault_V1");
        vm.startPrank(E1);
        factoryTest.upgradeBeaconTo(vaultImplTest);
        factoryTest.addCounterpartyToWhitelist(cpTest);
        testVault = ILegacyYieldVaultV1(
            factoryTest.createYieldVault(
                LegacyYieldVaultV1Types.InitParams(
                    address(core.stPROS), address(factoryTest), E2, cpTest, cpTest, "Faroo Test Vault", "fTEST",
                    _v1RoundParams(1 days, 7 days, 1_000_000 ether)
                )
            )
        );
        vm.stopPrank();
        vm.startPrank(E2);
        testVault.grantRole(OPERATOR, opTest);
        testVault.grantRole(bytes32(0), adminTest2);
        vm.stopPrank();
    }

    function _user(string memory label, uint256 amount) internal returns (address u) {
        u = makeAddr(label);
        users.push(u);
        _v1DepositWithPROS(core, u, amount);
    }

    function _intoVault(address u, ILegacyYieldVaultV1 v, uint256 amount) internal {
        vm.startPrank(u);
        core.stPROS.approve(address(v), amount);
        v.deposit(amount, u);
        vm.stopPrank();
    }

    function test_exportExecutionMainnetV1() public {
        vm.recordLogs();
        vm.chainId(1672);
        vm.warp(1_760_000_000);
        _safe();
        core = _deployMainnetV1(safe);
        main = _deployMainnetVaultsV1(core);
        vm.startPrank(safe);
        main.factory.addCounterpartyToWhitelist(cpMain);
        vault001 = ILegacyYieldVaultV1(
            main.factory.createYieldVault(
                LegacyYieldVaultV1Types.InitParams(
                    address(core.stPROS), address(main.factory), safe, cpMain, cpMain, "Faroo Vault 001", "fV001",
                    _v1RoundParams(1 days, 7 days, 1_000_000 ether)
                )
            )
        );
        vault001.grantRole(OPERATOR, op001);
        vault001.grantRole(bytes32(0), creationAdmin);
        vm.stopPrank();
        vm.prank(creationAdmin);
        vault001.renounceRole(bytes32(0), creationAdmin); // ever-held, not current
        _testFactory();

        // Holders: wrapped and native deposits; vault positions in both vaults.
        address alice = _user("mainnet.user.alice", 400 ether);
        address bob = _user("mainnet.user.bob", 250 ether);
        address carol = _user("mainnet.user.carol", 120 ether);
        address dave = makeAddr("mainnet.user.dave");
        users.push(dave);
        _v1DepositWPROS(core, dave, 80 ether);
        _intoVault(alice, vault001, 150 ether);
        _intoVault(bob, vault001, 60 ether);
        _intoVault(carol, testVault, 40 ether);
        _v1DepositWithPROS(core, E2, 50 ether); // E2 is a test-vault holder, as at the B0 pin
        _intoVault(E2, testVault, 50 ether);

        // Legacy withdrawal queue: several owners, several records each, one matured partial claim.
        _v1Redeem(core, alice, 30 ether);
        _v1Redeem(core, bob, 20 ether);
        vm.warp(block.timestamp + 1 days);
        _v1Redeem(core, alice, 15 ether);
        _v1Redeem(core, carol, 10 ether);
        _v1Redeem(core, dave, 5 ether);
        vm.warp(block.timestamp + 8 days); // records of day 0 and day 1 matured; both vaults LOCKED
        vm.prank(bob);
        core.stPROS.withdrawCompleteToPROS();
        _v1Redeem(core, alice, 7 ether); // immature at the export time
        _v1Redeem(core, dave, 3 ether);

        // vault 001: PROFIT(0) proposal and finalize: SETTLED (GC-01 A requires SETTLED).
        vm.prank(safe);
        vault001.proposeSettlement(LegacyYieldVaultV1Types.SettleMode.wrap(0), 0, address(0));
        vm.warp(block.timestamp + 600);
        vault001.finalize();
        // The test vault stays LOCKED (matured, never proposed): P-1 step 2 cancels it.

        vm.serializeString(META, "schema", "faroo-fork-execution-topology-v1");
        vm.serializeString(META, "chain", "mainnet");
        vm.serializeString(META, "evidenceClass", "SYNTHETIC");
        vm.serializeUint(META, "chainId", block.chainid);
        vm.serializeUint(META, "timestamp", block.timestamp);
        vm.serializeAddress(META, "proxyAdmin", core.proxyAdmin);
        vm.serializeAddress(META, "oracle", address(core.oracle));
        vm.serializeAddress(META, "oracleImpl", core.oracleImpl);
        vm.serializeAddress(META, "stpros", address(core.stPROS));
        vm.serializeAddress(META, "stprosImpl", core.stPROSImpl);
        vm.serializeAddress(META, "wpros", core.wpros);
        vm.serializeAddress(META, "factory", address(main.factory));
        vm.serializeAddress(META, "factoryImpl", main.factoryImpl);
        vm.serializeAddress(META, "beacon", main.beacon);
        vm.serializeAddress(META, "vaultImpl", main.vaultImpl);
        vm.serializeAddress(META, "vault001", address(vault001));
        vm.serializeAddress(META, "factoryTest", address(factoryTest));
        vm.serializeAddress(META, "factoryTestImpl", factoryTestImpl);
        vm.serializeAddress(META, "beaconTest", factoryTest.beacon());
        vm.serializeAddress(META, "vaultImplTest", vaultImplTest);
        vm.serializeAddress(META, "testVault", address(testVault));
        vm.serializeAddress(META, "safe", safe);
        vm.serializeAddress(META, "safeSingleton", singleton);
        vm.serializeAddress(META, "multiSendCallOnly", multiSend);
        vm.serializeAddress(META, "safeOwners", owners);
        vm.serializeUint(META, "safeThreshold", 3);
        vm.serializeAddress(META, "e1", E1);
        vm.serializeAddress(META, "e2", E2);
        vm.serializeAddress(META, "creationAdmin", creationAdmin);
        vm.serializeAddress(META, "vault001Operator", op001);
        vm.serializeAddress(META, "testVaultOperator", opTest);
        vm.serializeAddress(META, "testVaultExtraAdmin", adminTest2);
        vm.serializeAddress(META, "counterpartyMain", cpMain);
        vm.serializeAddress(META, "counterpartyTest", cpTest);
        vm.serializeAddress(META, "users", users);
        ExecutionExport.finish(META, META);
    }
}

/// @notice Testnet V3 pre-P-0 state on the recorded V3 runtimes (core from `OracleV3MigrationTest`):
///         E1 owns the ProxyAdmin and the four core proxies, E2 owns the Factory and administers
///         all 15 vaults. The vault phases follow the pattern of the B0 pin snapshot
///         (`snapshots/deployed/testnet/state.json`): SETTLE_PROPOSED with funded profit at indices
///         0, 3 and 11; unfunded proposals at 4 and 5; SETTLED at 6-9; LOCKED at 1, 2, 10, 12, 13, 14.
///         Amounts and holders are synthetic.
contract ExecutionTestnetTopology is OracleV3MigrationTest {
    address internal constant E1 = 0x4597C97a43dFBb4a398E2b16AA9cE61f90d801DD;
    address internal constant E2 = 0xb29eaEA575bd4bae7d9a301446e6204C3A23AdF1;
    bytes32 internal constant OPERATOR = keccak256("OPERATOR_ROLE");
    string internal constant META = "execution-testnet-v3";

    ILegacyYieldVaultFactoryV3 internal factory;
    address internal factoryImpl;
    address internal vaultImpl;
    address[] internal vaults;
    address[] internal users;
    address internal counterparty = makeAddr("testnet.counterparty");
    address internal opExtra = makeAddr("testnet.vault.operator");

    function _phaseOf(uint256 i) internal pure returns (uint8) {
        uint8[15] memory p = [2, 1, 1, 2, 2, 2, 3, 3, 3, 3, 1, 2, 1, 1, 1];
        return p[i];
    }

    function _funded(uint256 i) internal pure returns (bool) {
        return i == 0 || i == 3 || i == 11;
    }

    function test_exportExecutionTestnetV3() public {
        vm.recordLogs();
        setUp();
        vm.chainId(688689);
        // E1 owns the core and the ProxyAdmin (FS-2 starting state).
        vm.startPrank(gov);
        ILegacyProxyAdminV4(proxyAdmin).transferOwnership(E1);
        old.transferOwnership(E1);
        st.transferOwnership(E1);
        bridge.transferOwnership(E1);
        sink.transferOwnership(E1);
        vm.stopPrank();
        factoryImpl = _deployRecorded("testnet", "YieldVaultFactory_V3");
        factory = ILegacyYieldVaultFactoryV3(
            _deployTransparentProxyV4(
                "testnet", factoryImpl, proxyAdmin, abi.encodeCall(ILegacyYieldVaultFactoryV3.initialize, (E2))
            )
        );
        vaultImpl = _deployRecorded("testnet", "YieldVault_V3");
        LegacyYieldVaultV3Types.RoundParams memory round =
            LegacyYieldVaultV3Types.RoundParams(1 days, 7 days, 600, 1_000_000 ether, 0, 1 ether, 0, 5000);
        vm.startPrank(E2);
        factory.upgradeBeaconTo(vaultImpl);
        factory.addCounterpartyToWhitelist(counterparty);
        for (uint256 i; i < 15; ++i) {
            vaults.push(
                factory.createYieldVault(
                    LegacyYieldVaultV3Types.InitParams(
                        address(st), address(factory), E2, counterparty, counterparty, "Faroo testnet vault", "fTV", round
                    )
                )
            );
        }
        ILegacyYieldVaultV3(vaults[14]).grantRole(OPERATOR, opExtra);
        ILegacyYieldVaultV3(vaults[2]).grantRole(OPERATOR, opExtra);
        vm.stopPrank();

        // Holders: V3 deposits into StPROS, then into the non-empty vaults.
        for (uint256 i; i < 4; ++i) {
            address u = makeAddr(string.concat("testnet.user", vm.toString(i)));
            users.push(u);
            vm.deal(u, 200 ether);
            vm.prank(u);
            st.depositWithPROS{value: 200 ether}();
        }
        // The recorded V3 `proposeSettlement` funds a PROFIT proposal only from the proposing admin's
        // own balance (`fundFrom == msg.sender`), so E2 is the profit funder.
        vm.deal(E2, 50 ether);
        vm.prank(E2);
        st.depositWithPROS{value: 50 ether}();
        for (uint256 i; i < 15; ++i) {
            if (i == 8 || i == 10 || i == 13) continue; // empty vaults, as at the pin
            address u = users[i % 4];
            uint256 amount = (i + 1) * 1 ether;
            vm.startPrank(u);
            st.approve(vaults[i], amount);
            ILegacyYieldVaultV3(vaults[i]).deposit(amount, u);
            vm.stopPrank();
        }
        // V3 receiver-bound queue (unbonding 0): records for two owners with distinct receivers.
        vm.prank(users[0]);
        st.redeem(5 ether, makeAddr("testnet.receiver.a"), users[0]);
        vm.prank(users[1]);
        st.redeem(3 ether, makeAddr("testnet.receiver.b"), users[1]);
        vm.prank(users[0]);
        st.redeem(2 ether, users[0], users[0]);

        vm.warp(block.timestamp + 8 days); // all rounds LOCKED and matured
        for (uint256 i; i < 15; ++i) {
            uint8 p = _phaseOf(i);
            ILegacyYieldVaultV3 v = ILegacyYieldVaultV3(vaults[i]);
            if (p == 3) {
                vm.prank(E2);
                v.proposeSettlement(LegacyYieldVaultV3Types.SettleMode.wrap(0), 0, address(0));
            } else if (p == 2) {
                uint256 profit = _funded(i) ? (i + 1) * 0.1 ether : 0.05 ether;
                vm.startPrank(E2);
                if (_funded(i)) st.approve(address(v), profit);
                v.proposeSettlement(LegacyYieldVaultV3Types.SettleMode.wrap(0), profit, _funded(i) ? E2 : address(0));
                vm.stopPrank();
            }
        }
        vm.warp(block.timestamp + 600);
        for (uint256 i; i < 15; ++i) {
            if (_phaseOf(i) == 3) ILegacyYieldVaultV3(vaults[i]).finalize();
        }

        vm.serializeString(META, "schema", "faroo-fork-execution-topology-v1");
        vm.serializeString(META, "chain", "testnet");
        vm.serializeString(META, "evidenceClass", "SYNTHETIC");
        vm.serializeUint(META, "chainId", block.chainid);
        vm.serializeUint(META, "timestamp", block.timestamp);
        vm.serializeAddress(META, "proxyAdmin", proxyAdmin);
        vm.serializeAddress(META, "oracle", address(old));
        vm.serializeAddress(META, "oracleImpl", ILegacyProxyAdminV4(proxyAdmin).getProxyImplementation(address(old)));
        vm.serializeAddress(META, "stpros", address(st));
        vm.serializeAddress(META, "stprosImpl", ILegacyProxyAdminV4(proxyAdmin).getProxyImplementation(address(st)));
        vm.serializeAddress(META, "bridge", address(bridge));
        vm.serializeAddress(META, "bridgeImpl", ILegacyProxyAdminV4(proxyAdmin).getProxyImplementation(address(bridge)));
        vm.serializeAddress(META, "distributor", address(sink));
        vm.serializeAddress(META, "distributorImpl", ILegacyProxyAdminV4(proxyAdmin).getProxyImplementation(address(sink)));
        vm.serializeAddress(META, "wpros", wpros);
        vm.serializeAddress(META, "factory", address(factory));
        vm.serializeAddress(META, "factoryImpl", factoryImpl);
        vm.serializeAddress(META, "beacon", factory.beacon());
        vm.serializeAddress(META, "vaultImpl", vaultImpl);
        vm.serializeAddress(META, "vaults", vaults);
        vm.serializeAddress(META, "e1", E1);
        vm.serializeAddress(META, "e2", E2);
        vm.serializeAddress(META, "reporter", reporter);
        vm.serializeAddress(META, "treasury", treasury);
        vm.serializeAddress(META, "counterparty", counterparty);
        vm.serializeAddress(META, "profitFunder", E2);
        vm.serializeAddress(META, "vaultOperator", opExtra);
        vm.serializeAddress(META, "users", users);
        ExecutionExport.finish(META, META);
    }
}
