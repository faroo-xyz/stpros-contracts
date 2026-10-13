// SPDX-License-Identifier: GPL-3.0
pragma solidity ^0.8.28;

import {LegacyTopology, IWETH9Like} from "../../helpers/LegacyTopology.sol";
import {ILegacyProxyAdminV4} from "../../helpers/LegacyInterfaces.sol";
import {TimelockController} from "@openzeppelin/contracts/governance/TimelockController.sol";
import {StPROS} from "../../../contracts/StPROS.sol";
import {Oracle} from "../../../contracts/Oracle.sol";
import {BridgeVault} from "../../../contracts/BridgeVault.sol";
import {SlpDistributor} from "../../../contracts/SlpDistributor.sol";
import {CustodyTargets} from "../oracles/CustodyOracle.sol";
import {WithdrawalQueueModel} from "../oracles/WithdrawalQueueModel.sol";

/// @dev Forces native PROS into a contract without calling it.
contract ForcedNative {
    constructor(address payable target) payable {
        selfdestruct(target);
    }
}

/// @title SyntheticV1Migration
/// @notice SYNTHETIC (class C) mainnet-shaped state for harness validation: recorded V1 StPROS /
///         Oracle bytecode behind recorded v4 proxies, the real mainnet WPROS runtime (2300-gas
///         unwrap), several users with multiple legacy withdrawal records and one V1 claim. The
///         migration executes the real current `StPROS.migrateFromV1` and `Oracle.migrateFromV1`.
///         Balances, users and actors are synthetic; this is not chain state.
/// @dev The independent ghost queue is driven by the *actions* (request amounts returned by each
///      redemption, claim calls) and never seeded from queue getters.
abstract contract SyntheticV1Migration is LegacyTopology {
    uint256 internal constant V1_UNBONDING = 7 days;

    LegacyV1 internal legacy;
    StPROS internal st;
    Oracle internal oracle;
    BridgeVault internal bridge;
    SlpDistributor internal sink;
    TimelockController internal tl;
    WithdrawalQueueModel internal ghost;

    address internal gov = makeAddr("synthetic.gov");
    address internal grd = makeAddr("synthetic.guardian");
    address internal reporter = makeAddr("synthetic.reporter");
    address internal treasury = makeAddr("synthetic.treasury");
    address internal alice = makeAddr("synthetic.alice");
    address internal bob = makeAddr("synthetic.bob");
    address internal carol = makeAddr("synthetic.carol");

    function _buildV1State() internal {
        vm.warp(1_000_000);
        address[] memory actors = new address[](1);
        actors[0] = gov;
        tl = new TimelockController(1 days, actors, actors, address(0));
        legacy = _deployMainnetV1(gov);
        st = StPROS(payable(address(legacy.stPROS)));
        oracle = Oracle(address(legacy.oracle));
        ghost = new WithdrawalQueueModel(WithdrawalQueueModel.Rule.CORRECT);

        _v1DepositWithPROS(legacy, alice, 100 ether);
        _v1DepositWPROS(legacy, bob, 50 ether);
        _v1DepositWithPROS(legacy, carol, 30 ether);
        _request(alice, 10 ether);
        _request(bob, 20 ether);
        _request(carol, 30 ether);
        vm.warp(block.timestamp + 1 days);
        _request(alice, 5 ether);
        vm.warp(block.timestamp + V1_UNBONDING);
        // A matured V1 claim: the V1 reserve backs exactly Q − C, so the ghost's liquidity is its
        // own Q − C, not a getter.
        uint256 predicted = ghost.claim(carol, carol, 0, block.timestamp, ghost.queued() - ghost.completed());
        uint256 paid = _v1ClaimToPROS(legacy, carol);
        require(paid == predicted && paid == 30 ether, "fixture: V1 claim differs from ghost");
        _request(alice, 3 ether); // immature at migration

        bridge = BridgeVault(
            payable(
                _deployTransparentProxyV4(
                    "mainnet",
                    address(new BridgeVault()),
                    legacy.proxyAdmin,
                    abi.encodeCall(BridgeVault.initialize, (address(tl), address(st), true, grd))
                )
            )
        );
        sink = SlpDistributor(
            payable(
                _deployTransparentProxyV4(
                    "mainnet",
                    address(new SlpDistributor()),
                    legacy.proxyAdmin,
                    abi.encodeCall(SlpDistributor.initialize, (address(tl), reporter, new address[](0), grd))
                )
            )
        );
    }

    function _request(address owner, uint256 shares) internal {
        uint256 assets = _v1Redeem(legacy, owner, shares);
        ghost.request(owner, assets, address(0), block.timestamp, V1_UNBONDING);
    }

    function _targets() internal view returns (CustodyTargets memory) {
        return CustodyTargets(address(st), legacy.wpros, address(bridge), address(sink), address(oracle));
    }

    /// @notice Positive-donation variant (FS-1a v): forced native at StPROS and BridgeVault,
    ///         donated WPROS and stPROS at StPROS, WPROS at BridgeVault.
    function _donate() internal {
        address donor = makeAddr("synthetic.donor");
        vm.deal(donor, 10 ether);
        vm.startPrank(donor);
        new ForcedNative{value: 3}(payable(address(st)));
        new ForcedNative{value: 7}(payable(address(bridge)));
        IWETH9Like(legacy.wpros).deposit{value: 2 ether + 1}();
        IWETH9Like(legacy.wpros).transfer(address(st), 1 ether + 1);
        IWETH9Like(legacy.wpros).transfer(address(bridge), 1 ether);
        vm.stopPrank();
        vm.prank(alice);
        legacy.stPROS.transfer(address(st), 1);
    }

    /// @notice The U-frame migration pair (Oracle then StPROS) executed by the ProxyAdmin owner.
    ///         Test-only stand-in for the TL-executed U batch; the custody frame is the same.
    function _migrate() internal {
        Oracle.OracleV1Migration memory p = Oracle.OracleV1Migration(
            reporter, address(st), 100 ether, 1 hours, 500_000, treasury, 0, grd, address(tl)
        );
        address oi = address(new Oracle());
        address si = address(new StPROS());
        vm.startPrank(gov);
        ILegacyProxyAdminV4(legacy.proxyAdmin).upgradeAndCall(address(oracle), oi, abi.encodeCall(Oracle.migrateFromV1, (p)));
        ILegacyProxyAdminV4(legacy.proxyAdmin).upgradeAndCall(
            address(st),
            si,
            abi.encodeCall(StPROS.migrateFromV1, (address(sink), payable(address(bridge)), grd, address(tl)))
        );
        vm.stopPrank();
    }
}
