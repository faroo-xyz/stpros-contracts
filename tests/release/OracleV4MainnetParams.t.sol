// SPDX-License-Identifier: GPL-3.0
pragma solidity ^0.8.28;

import {ProtocolTestBase} from "../helpers/ProtocolTestBase.sol";
import {ERC1967Proxy} from "@openzeppelin/contracts/proxy/ERC1967/ERC1967Proxy.sol";

import {Oracle} from "../../contracts/Oracle.sol";
import {OracleVTokenHarness} from "../Oracle.t.sol";
import {WPROS9} from "../mocks/WPROS9.sol";

/// @notice The V4 mainnet Oracle parameters (product decision 2026-10-20) against the unchanged Oracle code:
/// maxAprPpm 300,000 (3 x the 10 % reference), maxUpdateAmount 80,000 PROS, updateInterval 72,000 s (20 h),
/// commission 100,000 ppm. `updateInterval` is a minimum spacing, not a reporting schedule; the absolute cap and the
/// APR allowance (pool x maxApr x min(elapsed, 7 days) / 365 days) apply together.
contract OracleV4MainnetParamsTest is ProtocolTestBase {
    uint256 internal constant MAX_APR = 300_000;
    uint256 internal constant MAX_UPDATE = 80_000 ether;
    uint256 internal constant INTERVAL = 72_000;
    uint256 internal constant COMMISSION = 100_000;

    Oracle internal oracle;
    OracleVTokenHarness internal vtoken;
    WPROS9 internal wpros;
    address internal owner;
    address internal reporter = makeAddr("reporter");
    address internal treasury = makeAddr("treasury");

    function _deploy(uint256 pool) internal {
        owner = _newTimelock();
        wpros = new WPROS9();
        oracle = Oracle(
            address(
                new ERC1967Proxy(
                    address(new Oracle()), abi.encodeWithSelector(Oracle.initialize.selector, owner, address(0x1234))
                )
            )
        );
        vtoken = OracleVTokenHarness(
            payable(
                address(
                    new ERC1967Proxy(
                        address(new OracleVTokenHarness()),
                        abi.encodeWithSelector(
                            OracleVTokenHarness.initialize.selector, address(wpros), owner, address(oracle)
                        )
                    )
                )
            )
        );
        vm.startPrank(owner);
        oracle.setVToken(address(vtoken), true);
        oracle.setCommissionVToken(address(wpros), address(vtoken));
        oracle.setSlp(reporter);
        oracle.setUpdateLimits(MAX_UPDATE, INTERVAL);
        oracle.setMaxAprPpm(MAX_APR);
        oracle.setCommissionConfig(treasury, COMMISSION);
        vm.stopPrank();
        vtoken.seedShares(makeAddr("holder"), pool);
        vm.prank(address(vtoken));
        oracle.initializePool(address(wpros), pool, pool);
    }

    function _allowance(uint256 pool, uint256 elapsed) internal pure returns (uint256) {
        if (elapsed > 7 days) elapsed = 7 days;
        return pool * MAX_APR * elapsed / (365 days * 1_000_000);
    }

    function _update(uint256 amount) internal {
        vm.prank(reporter);
        oracle.update(address(wpros), amount);
    }

    function test_ParametersAreStored() external {
        _deploy(1_000_000 ether);
        assertEq(oracle.maxAprPpm(), MAX_APR);
        assertEq(oracle.maxUpdateAmount(), MAX_UPDATE);
        assertEq(oracle.updateInterval(), INTERVAL);
        assertEq(oracle.commissionRatePpm(), COMMISSION);
    }

    function test_IntervalIsAMinimumSpacingNotASchedule() external {
        _deploy(1_000_000 ether);
        uint256 start = block.timestamp;
        vm.warp(start + INTERVAL - 1);
        vm.prank(reporter);
        vm.expectRevert(abi.encodeWithSelector(Oracle.UpdateTooFrequent.selector, start + INTERVAL));
        oracle.update(address(wpros), 1 ether);
        vm.warp(start + INTERVAL);
        _update(1 ether);                                       // exactly 20 h later
        uint256 last = block.timestamp;
        vm.warp(last + INTERVAL - 1);
        vm.prank(reporter);
        vm.expectRevert(abi.encodeWithSelector(Oracle.UpdateTooFrequent.selector, last + INTERVAL));
        oracle.update(address(wpros), 1 ether);
        vm.warp(last + 30 days);                                // no update for 30 days is allowed: not a schedule
        assertTrue(oracle.canUpdate(address(wpros), 1 ether));
        _update(1 ether);
    }

    function test_AprAllowanceBindsBelowTheAbsoluteCap() external {
        uint256 pool = 1_000_000 ether;
        _deploy(pool);
        vm.warp(block.timestamp + INTERVAL);
        uint256 allowed = _allowance(pool, INTERVAL);           // ~684.9 PROS for 20 h at 30 %
        assertLt(allowed, MAX_UPDATE);
        vm.prank(reporter);
        vm.expectRevert(abi.encodeWithSelector(Oracle.UpdateAmountTooLarge.selector, allowed + 1, allowed));
        oracle.update(address(wpros), allowed + 1);
        _update(allowed);
        (uint256 tokens,) = oracle.poolInfo(address(wpros));
        assertEq(tokens, pool + allowed);
    }

    function test_CatchUpIsCappedAtSevenDays() external {
        uint256 pool = 1_000_000 ether;
        _deploy(pool);
        vm.warp(block.timestamp + 30 days);
        uint256 allowed = _allowance(pool, 30 days);
        assertEq(allowed, _allowance(pool, 7 days));            // ~5,753 PROS: 30 days count as 7
        vm.prank(reporter);
        vm.expectRevert(abi.encodeWithSelector(Oracle.UpdateAmountTooLarge.selector, allowed + 1, allowed));
        oracle.update(address(wpros), allowed + 1);
        _update(allowed);
    }

    function test_AbsoluteCapBindsForALargePool() external {
        uint256 pool = 100_000_000 ether;
        _deploy(pool);
        vm.warp(block.timestamp + 7 days);
        assertGt(_allowance(pool, 7 days), MAX_UPDATE);         // APR would allow ~575,342 PROS
        vm.prank(reporter);
        vm.expectRevert(abi.encodeWithSelector(Oracle.UpdateAmountTooLarge.selector, MAX_UPDATE + 1, MAX_UPDATE));
        oracle.update(address(wpros), MAX_UPDATE + 1);
        _update(MAX_UPDATE);
    }

    function test_ContractAprCeilingIsUnchanged() external {
        _deploy(1_000_000 ether);
        assertEq(oracle.MAX_APR_PPM(), 500_000);
        vm.prank(owner);
        vm.expectRevert(Oracle.InvalidRateLimits.selector);
        oracle.setMaxAprPpm(500_001);
    }
}
