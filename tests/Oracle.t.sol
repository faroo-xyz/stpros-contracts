// SPDX-License-Identifier: GPL-3.0
pragma solidity ^0.8.28;

import {ProtocolTestBase} from "./helpers/ProtocolTestBase.sol";
import {ERC1967Proxy} from "@openzeppelin/contracts/proxy/ERC1967/ERC1967Proxy.sol";
import {IERC20} from "@openzeppelin/contracts/token/ERC20/IERC20.sol";
import {IERC4626} from "@openzeppelin/contracts/interfaces/IERC4626.sol";

import {Oracle} from "../contracts/Oracle.sol";
import {VToken} from "../contracts/VToken.sol";
import {WPROS9} from "./mocks/WPROS9.sol";

contract OracleVTokenHarness is VToken {
    function seedShares(address who, uint256 amount) external {
        _mint(who, amount);
    }

    function initialize(IERC20 asset_, address owner_, address oracle_) external initializer {
        __VToken_init(asset_, owner_, "Staked PROS", "stPROS", address(0x1234));
        oracle = Oracle(oracle_);
    }
}

contract OracleTest is ProtocolTestBase {
    Oracle internal oracle;
    OracleVTokenHarness internal vtoken;
    WPROS9 internal wpros;

    address internal owner = makeAddr("owner");
    address internal slp = makeAddr("slp");
    address internal alice = makeAddr("alice");
    address internal commissionAccount = makeAddr("commissionAccount");

    uint256 internal constant MAX_UPDATE = 100 ether;
    uint256 internal constant UPDATE_INTERVAL = 1 hours;

    function setUp() external {
        owner = _newTimelock();
        wpros = new WPROS9();

        Oracle oracleImplementation = new Oracle();
        bytes memory oracleInitData = abi.encodeWithSelector(Oracle.initialize.selector, owner, address(0x1234));
        ERC1967Proxy oracleProxy = new ERC1967Proxy(address(oracleImplementation), oracleInitData);
        oracle = Oracle(address(oracleProxy));

        OracleVTokenHarness vtokenImplementation = new OracleVTokenHarness();
        bytes memory vtokenInitData =
            abi.encodeWithSelector(OracleVTokenHarness.initialize.selector, address(wpros), owner, address(oracle));
        ERC1967Proxy vtokenProxy = new ERC1967Proxy(address(vtokenImplementation), vtokenInitData);
        vtoken = OracleVTokenHarness(payable(address(vtokenProxy)));

        vm.prank(owner);
        oracle.setVToken(address(vtoken), true);
        vm.prank(owner);
        oracle.setCommissionVToken(address(wpros), address(vtoken));
        vtoken.seedShares(alice, 1_000 ether);
        vm.prank(address(vtoken));
        oracle.initializePool(address(wpros), 1_000 ether, 1_000 ether);
    }

    function _upgradeOracleToV2() internal {
        vm.startPrank(owner);
        oracle.setSlp(slp);
        oracle.setUpdateLimits(MAX_UPDATE, UPDATE_INTERVAL);
        oracle.setMaxAprPpm(500_000);
        vm.stopPrank();
    }

    function _commissionMappings() internal view returns (address[] memory tokens, address[] memory vTokens) {
        tokens = new address[](1);
        vTokens = new address[](1);
        tokens[0] = address(wpros);
        vTokens[0] = address(vtoken);
    }

    function _upgradeOracleToV3(uint256 ratePpm) internal {
        vm.prank(owner);
        oracle.setCommissionConfig(commissionAccount, ratePpm);
    }

    function test_Governance_ShouldConfigureSlpVTokenAndLimits() external {
        _upgradeOracleToV2();

        assertEq(oracle.slp(), slp, "slp");
        assertTrue(oracle.vTokenAddresses(address(vtoken)), "vToken registered");
        assertEq(oracle.maxUpdateAmount(), MAX_UPDATE, "max update");
        assertEq(oracle.updateInterval(), UPDATE_INTERVAL, "interval");
    }

    function test_RemovedInitializeV2_ShouldRevert() external {
        _upgradeOracleToV2();
        vm.prank(owner);
        (bool ok,) = address(oracle).call(
            abi.encodeWithSignature(
                "initializeV2(address,address,uint256,uint256)", slp, address(vtoken), MAX_UPDATE, UPDATE_INTERVAL
            )
        );
        assertFalse(ok, "removed migration initializer must not be callable");
    }

    function test_Governance_ShouldConfigureCommission() external {
        _upgradeOracleToV2();
        _upgradeOracleToV3(1_000);

        assertEq(oracle.commissionAccount(), commissionAccount, "commission account");
        assertEq(oracle.commissionRatePpm(), 1_000, "commission rate");
        assertEq(oracle.tokenToVToken(address(wpros)), address(vtoken), "commission vToken");
    }

    function test_Governance_ShouldConfigureR4AndRejectOldInitializer() external {
        _upgradeOracleToV2();
        _upgradeOracleToV3(10_000);
        assertEq(oracle.slp(), slp);
        assertTrue(oracle.vTokenAddresses(address(vtoken)));
        assertEq(oracle.maxUpdateAmount(), MAX_UPDATE);
        assertEq(oracle.updateInterval(), UPDATE_INTERVAL);
        assertEq(oracle.commissionAccount(), commissionAccount);
        assertEq(oracle.commissionRatePpm(), 10_000);
        assertEq(oracle.tokenToVToken(address(wpros)), address(vtoken));
        vm.prank(owner);
        (bool ok,) = address(oracle).call(
            abi.encodeWithSignature(
                "initializeV3(address,uint256,address[],address[])",
                commissionAccount,
                10_000,
                new address[](0),
                new address[](0)
            )
        );
        assertFalse(ok);
    }

    function test_Update_ShouldIncreaseTokenAmount_WhenCalledBySlp() external {
        _upgradeOracleToV2();
        vm.warp(block.timestamp + 7 days);

        assertTrue(oracle.canUpdate(address(wpros), 1 ether), "preview allows update");

        vm.prank(slp);
        oracle.update(address(wpros), 1 ether);

        (uint256 tokenAmount,) = oracle.poolInfo(address(wpros));
        assertEq(tokenAmount, 1_001 ether, "token amount increased");
        assertEq(oracle.lastUpdateAt(address(wpros)), block.timestamp, "timestamp updated");
    }

    function test_Update_ShouldMintCommissionUsingExactPostUpdateFormula() external {
        _upgradeOracleToV2();
        _upgradeOracleToV3(10_000);
        vm.prank(owner);
        oracle.setTokenAmount(address(wpros), 1_000 ether, 2_000 ether);
        vm.warp(block.timestamp + 7 days);
        vm.prank(slp);
        oracle.update(address(wpros), 10 ether);
        uint256 expected = uint256(0.1 ether) * 1_000 ether / (2_010 ether - 0.1 ether);
        (uint256 t, uint256 v) = oracle.poolInfo(address(wpros));
        assertEq(t, 2_010 ether);
        assertEq(v, 1_000 ether + expected);
        assertEq(vtoken.balanceOf(commissionAccount), expected);
        assertLe(expected * t / v, 0.1 ether, "exact post-update commission bound");
    }

    function test_Update_ShouldRevert_WhenCommissionMappingMissing() external {
        _upgradeOracleToV2();
        vm.prank(owner);
        oracle.setCommissionConfig(commissionAccount, 10_000);
        vm.prank(owner);
        oracle.setCommissionVToken(address(wpros), address(0));
        vm.warp(block.timestamp + 7 days);

        assertFalse(oracle.canUpdate(address(wpros), 1 ether), "missing commission mapping");

        vm.prank(slp);
        vm.expectRevert(abi.encodeWithSelector(Oracle.MissingCommissionVToken.selector, address(wpros)));
        oracle.update(address(wpros), 1 ether);
    }

    function test_Update_ShouldNotMintCommission_WhenRoundedToZero() external {
        _upgradeOracleToV2();
        _upgradeOracleToV3(10);
        vm.warp(block.timestamp + 7 days);

        vm.prank(slp);
        oracle.update(address(wpros), 1);

        (, uint256 vTokenAmount) = oracle.poolInfo(address(wpros));
        assertEq(vTokenAmount, 1_000 ether, "vToken side unchanged");
        assertEq(vtoken.balanceOf(commissionAccount), 0, "no commission minted");
    }

    function test_SetCommissionConfig_ShouldRevert_WhenRateAboveMax() external {
        _upgradeOracleToV2();

        vm.prank(owner);
        vm.expectRevert(abi.encodeWithSelector(Oracle.InvalidCommissionRate.selector, 1_000_001, 1_000_000));
        oracle.setCommissionConfig(commissionAccount, 1_000_001);
    }

    function test_SetCommissionVToken_ShouldValidateAsset() external {
        _upgradeOracleToV2();
        address wrongToken = makeAddr("wrongToken");
        address expectedAsset = IERC4626(address(vtoken)).asset();

        vm.prank(owner);
        vm.expectRevert(abi.encodeWithSelector(Oracle.InvalidVTokenAsset.selector, wrongToken, expectedAsset));
        oracle.setCommissionVToken(wrongToken, address(vtoken));
    }

    function test_Update_ShouldRevert_WhenNotSlp() external {
        _upgradeOracleToV2();

        vm.prank(alice);
        vm.expectRevert(abi.encodeWithSelector(Oracle.NotSlp.selector, alice));
        oracle.update(address(wpros), 1 ether);
    }

    function test_Update_ShouldRevert_WhenExceedsMax() external {
        _upgradeOracleToV2();

        vm.prank(slp);
        vm.expectRevert(abi.encodeWithSelector(Oracle.UpdateAmountTooLarge.selector, MAX_UPDATE + 1, MAX_UPDATE));
        oracle.update(address(wpros), MAX_UPDATE + 1);
    }

    function test_Update_ShouldRevert_WhenTooFrequent() external {
        _upgradeOracleToV2();
        vm.warp(block.timestamp + 7 days);

        vm.prank(slp);
        oracle.update(address(wpros), 1 ether);

        assertFalse(oracle.canUpdate(address(wpros), 1 ether), "preview blocks frequent update");

        vm.prank(slp);
        vm.expectRevert();
        oracle.update(address(wpros), 1 ether);
    }

    function test_CanUpdate_ShouldReturnFalse_WhenPaused() external {
        _upgradeOracleToV2();
        vm.warp(block.timestamp + 7 days);

        vm.prank(owner);
        oracle.pause();

        assertFalse(oracle.canUpdate(address(wpros), 1 ether), "paused");
    }

    function test_CanUpdate_ShouldReturnFalse_WhenAmountExceedsMax() external {
        _upgradeOracleToV2();
        vm.warp(block.timestamp + 7 days);

        assertFalse(oracle.canUpdate(address(wpros), MAX_UPDATE + 1), "amount too large");
    }

    function test_CanUpdate_ShouldReturnFalse_WhenTokenIsZero() external {
        _upgradeOracleToV2();

        assertFalse(oracle.canUpdate(address(0), 1 ether), "zero token");
    }

    function test_CanUpdate_ShouldReturnFalse_WhenAmountIsZero() external {
        _upgradeOracleToV2();

        assertFalse(oracle.canUpdate(address(wpros), 0), "zero amount");
    }

    function test_CanUpdate_ShouldReturnFalse_WhenUpdateDisabled() external {
        vm.prank(owner);
        oracle.setUpdateLimits(0, UPDATE_INTERVAL);

        assertFalse(oracle.canUpdate(address(wpros), 1 ether), "maxUpdateAmount zero");
    }

    function test_OnMint_ShouldIncreasePool() external {
        _upgradeOracleToV2();

        vm.prank(address(vtoken));
        oracle.onMint(address(wpros), 20 ether, 20 ether);

        (uint256 tokenAmount, uint256 vTokenAmount) = oracle.poolInfo(address(wpros));
        assertEq(tokenAmount, 1_020 ether, "token side");
        assertEq(vTokenAmount, 1_020 ether, "vToken side");
    }

    function test_OnRedeem_ShouldDecreasePool() external {
        _upgradeOracleToV2();

        vm.prank(address(vtoken));
        oracle.onRedeem(address(wpros), 30 ether, 30 ether);

        (uint256 tokenAmount, uint256 vTokenAmount) = oracle.poolInfo(address(wpros));
        assertEq(tokenAmount, 970 ether, "token side");
        assertEq(vTokenAmount, 970 ether, "vToken side");
    }

    function test_OnRedeem_ShouldRevert_WhenInsufficientPool() external {
        _upgradeOracleToV2();

        vm.prank(address(vtoken));
        vm.expectRevert(Oracle.InsufficientPoolAmount.selector);
        oracle.onRedeem(address(wpros), 2_000 ether, 2_000 ether);
    }

    function test_RemovedPoolSetter_ByOwner_ShouldRevert() external {
        (uint256 t, uint256 v) = oracle.poolInfo(address(wpros));
        vm.prank(owner);
        (bool ok,) = address(oracle).call(
            abi.encodeWithSignature("setPoolInfo(address,uint256,uint256)", makeAddr("otherToken"), 3 ether, 4 ether)
        );
        assertFalse(ok, "arbitrary pool setter removed in R4");
        (uint256 afterT, uint256 afterV) = oracle.poolInfo(address(wpros));
        assertEq(t, afterT);
        assertEq(v, afterV);
    }

    function test_RemovedPoolSetter_ByVToken_ShouldRevert() external {
        (uint256 t, uint256 v) = oracle.poolInfo(address(wpros));
        vm.prank(address(vtoken));
        (bool ok,) = address(oracle).call(
            abi.encodeWithSignature("setPoolInfo(address,uint256,uint256)", address(wpros), 3 ether, 4 ether)
        );
        assertFalse(ok, "arbitrary pool setter removed in R4");
        (uint256 afterT, uint256 afterV) = oracle.poolInfo(address(wpros));
        assertEq(t, afterT);
        assertEq(v, afterV);
    }

    function test_SetPoolInfo_ByVToken_ShouldRevert_WrongAsset() external {
        (uint256 t, uint256 v) = oracle.poolInfo(address(wpros));
        vm.prank(address(vtoken));
        (bool ok,) = address(oracle).call(
            abi.encodeWithSignature("setPoolInfo(address,uint256,uint256)", makeAddr("wrongToken"), 3 ether, 4 ether)
        );
        assertFalse(ok, "arbitrary pool setter removed in R4");
        (uint256 afterT, uint256 afterV) = oracle.poolInfo(address(wpros));
        assertEq(t, afterT);
        assertEq(v, afterV);
    }

    function test_SetPoolInfo_ByUnregisteredCaller_ShouldRevert() external {
        (uint256 t, uint256 v) = oracle.poolInfo(address(wpros));
        vm.prank(alice);
        (bool ok,) = address(oracle).call(
            abi.encodeWithSignature("setPoolInfo(address,uint256,uint256)", address(wpros), 3 ether, 4 ether)
        );
        assertFalse(ok, "arbitrary pool setter removed in R4");
        (uint256 afterT, uint256 afterV) = oracle.poolInfo(address(wpros));
        assertEq(t, afterT);
        assertEq(v, afterV);
    }
}
