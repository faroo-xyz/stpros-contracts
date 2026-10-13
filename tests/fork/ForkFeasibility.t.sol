// SPDX-License-Identifier: GPL-3.0
pragma solidity ^0.8.28;

import {Test} from "forge-std/Test.sol";

import {ILegacyProxyAdminV4, ILegacyStPROSV1, LegacyVTokenV1Types} from "../helpers/LegacyInterfaces.sol";

/// @title CB-1 fork feasibility on Pharos mainnet (B0)
/// @notice Skipped unless FAROO_FORK_RPC_MAINNET is set. Pass only the public endpoint and only
///         through the .env-free export:
///           tools/verify/run.sh "$dest" FAROO_FORK_RPC_MAINNET=https://rpc.pharos.xyz \
///             forge test --match-path tests/fork/ForkFeasibility.t.sol
///         Forks at the pinned block (snapshots/deployed/pins.json), checks the forked state against
///         snapshots/deployed/mainnet/state.json and the recorded V1 bytecode, then runs an
///         impersonated no-op upgrade and a fresh V1 user cycle on the local fork only.
contract ForkFeasibilityTest is Test {
    bytes32 internal constant IMPL_SLOT = 0x360894a13ba1a3210667c828492db98dca3e2076cc3735a920a3ca505d382bbc;
    address internal constant STPROS = 0x6b0A44c64190279f7034b77c13a566E914FE5Ec4;
    address internal constant PROXY_ADMIN = 0x4238Ea4AdFa2bD6a5fC9B5E245dc1900cF0258aa;

    string internal url;
    string internal state;
    uint256 internal pin;

    function setUp() public {
        url = vm.envOr("FAROO_FORK_RPC_MAINNET", string(""));
        if (bytes(url).length == 0) return;
        pin = vm.parseJsonUint(vm.readFile(string.concat(vm.projectRoot(), "/snapshots/deployed/pins.json")), ".mainnet");
        state = vm.readFile(string.concat(vm.projectRoot(), "/snapshots/deployed/mainnet/state.json"));
        vm.createSelectFork(url, pin);
    }

    modifier forkOnly() {
        if (bytes(url).length == 0) {
            vm.skip(true);
        }
        _;
    }

    function _u(string memory key) internal view returns (uint256) {
        return vm.parseUint(vm.parseJsonString(state, key));
    }

    function test_fork_mainnet_stateMatchesSnapshotAndRecordedBytecode() public forkOnly {
        assertEq(block.number, pin);
        ILegacyStPROSV1 st = ILegacyStPROSV1(payable(STPROS));
        assertEq(st.totalSupply(), _u(".stpros.S_totalSupply"));
        assertEq(st.queuedWithdrawal(), _u(".stpros.Q_queuedWithdrawal"));
        assertEq(st.completedWithdrawal(), _u(".stpros.C_completedWithdrawal"));
        assertEq(st.totalCanWithdrawAmount(), st.queuedWithdrawal() - st.completedWithdrawal(), "R == Q - C");

        address impl = address(uint160(uint256(vm.load(STPROS, IMPL_SLOT))));
        string memory fx = vm.readFile(string.concat(vm.projectRoot(), "/tests/fixtures/legacy/mainnet/StPROS_V1.json"));
        assertEq(keccak256(impl.code), vm.parseJsonBytes32(fx, ".runtimeKeccak"), "live impl == recorded V1 runtime");
        string memory tup =
            vm.readFile(string.concat(vm.projectRoot(), "/tests/fixtures/legacy/mainnet/TransparentUpgradeableProxy_V4.json"));
        assertEq(keccak256(STPROS.code), vm.parseJsonBytes32(tup, ".runtimeKeccak"), "proxy == recorded v4 TUP");
    }

    function test_fork_mainnet_impersonatedNoopUpgrade_localOnly() public forkOnly {
        address safe = ILegacyProxyAdminV4(PROXY_ADMIN).owner();
        address impl = address(uint160(uint256(vm.load(STPROS, IMPL_SLOT))));
        vm.prank(safe);
        uint256 g = gasleft();
        ILegacyProxyAdminV4(PROXY_ADMIN).upgrade(STPROS, impl);
        emit log_named_uint("gas:fork.ProxyAdmin.upgrade(noop)", g - gasleft());
        assertEq(address(uint160(uint256(vm.load(STPROS, IMPL_SLOT)))), impl);
    }

    /// @dev Testnet: FAROO_FORK_RPC_TESTNET=https://atlantic.dplabs-internal.com
    function test_fork_testnet_v3ImplementationsMatchFixtures() public {
        string memory turl = vm.envOr("FAROO_FORK_RPC_TESTNET", string(""));
        if (bytes(turl).length == 0) {
            vm.skip(true);
        }
        uint256 tpin =
            vm.parseJsonUint(vm.readFile(string.concat(vm.projectRoot(), "/snapshots/deployed/pins.json")), ".testnet");
        string memory tstate = vm.readFile(string.concat(vm.projectRoot(), "/snapshots/deployed/testnet/state.json"));
        vm.createSelectFork(turl, tpin);
        assertEq(block.number, tpin);
        address[3] memory proxies = [
            0xb1437aEEa18189eb6D02Dc46cd4D28613D582e9A, // StPROS
            0x6bD39d03d2fBBF14aee362977a09C293B282B0bc, // Oracle
            0xB79db65038a11fa8F5A361e5FF265842b3619Ddc // BridgeVault
        ];
        string[3] memory names = ["StPROS_V3", "Oracle_V3", "BridgeVault_V3"];
        for (uint256 i = 0; i < 3; i++) {
            address impl = address(uint160(uint256(vm.load(proxies[i], IMPL_SLOT))));
            string memory fx =
                vm.readFile(string.concat(vm.projectRoot(), "/tests/fixtures/legacy/testnet/", names[i], ".json"));
            assertEq(keccak256(impl.code), vm.parseJsonBytes32(fx, ".runtimeKeccak"), names[i]);
        }
        assertEq(
            ILegacyStPROSV1(payable(proxies[0])).totalSupply(),
            vm.parseUint(vm.parseJsonString(tstate, ".stpros.S_totalSupply"))
        );
    }

    function test_fork_mainnet_freshV1UserCycle_localOnly() public forkOnly {
        ILegacyStPROSV1 st = ILegacyStPROSV1(payable(STPROS));
        address user = makeAddr("fork.user");
        vm.deal(user, 5 ether);
        uint256 qBefore = st.queuedWithdrawal();
        vm.startPrank(user);
        uint256 shares = st.depositWithPROS{value: 5 ether}();
        st.redeem(shares, user, user);
        vm.stopPrank();
        LegacyVTokenV1Types.Withdrawal[] memory recs = st.getWithdrawals(user);
        assertEq(recs.length, 1);
        assertEq(recs[0].queued, qBefore, "baseline is the live Q");
        vm.warp(block.timestamp + st.unbondingPeriod());
        vm.prank(user);
        assertEq(st.withdrawCompleteToPROS(), 5 ether);
        assertEq(user.balance, 5 ether, "native payout through the real WPROS unwrap");
    }
}
