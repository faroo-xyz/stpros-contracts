// SPDX-License-Identifier: GPL-3.0
pragma solidity ^0.8.28;

import {R4UnitTest} from "./helpers/R4UnitTest.sol";
import {ERC1967Proxy} from "@openzeppelin/contracts/proxy/ERC1967/ERC1967Proxy.sol";
import {IERC20} from "@openzeppelin/contracts/token/ERC20/IERC20.sol";

import {BridgeVault} from "../contracts/BridgeVault.sol";
import {VToken} from "../contracts/VToken.sol";
import {WPROS9} from "./mocks/WPROS9.sol";

contract BridgeVaultVTokenHarness is VToken {
    function initialize(IERC20 asset_, address owner_) external initializer {
        __VToken_init(asset_, owner_, "Staked PROS", "stPROS", address(0x1234));
    }
}

contract BridgeVaultTest is R4UnitTest {
    WPROS9 internal wpros;
    BridgeVault internal bridgeVault;
    BridgeVaultVTokenHarness internal vtoken;

    address internal owner = makeAddr("owner");
    address internal alice = makeAddr("alice");
    address internal outsider = makeAddr("outsider");

    function setUp() external {
        owner = _newTimelock();
        wpros = new WPROS9();

        BridgeVaultVTokenHarness vtokenImplementation = new BridgeVaultVTokenHarness();
        bytes memory vtokenInitData =
            abi.encodeWithSelector(BridgeVaultVTokenHarness.initialize.selector, address(wpros), owner);
        ERC1967Proxy vtokenProxy = new ERC1967Proxy(address(vtokenImplementation), vtokenInitData);
        vtoken = BridgeVaultVTokenHarness(payable(address(vtokenProxy)));

        BridgeVault bridgeVaultImplementation = new BridgeVault();
        bytes memory bridgeVaultInitData =
            abi.encodeWithSelector(BridgeVault.initialize.selector, owner, address(vtoken), false, address(0x1234));
        ERC1967Proxy bridgeVaultProxy = new ERC1967Proxy(address(bridgeVaultImplementation), bridgeVaultInitData);
        bridgeVault = BridgeVault(payable(address(bridgeVaultProxy)));
    }

    function test_Initialize_ShouldRegisterVToken() external view {
        assertTrue(bridgeVault.vTokenAddresses(address(vtoken)), "registered");
        assertEq(bridgeVault.vTokenByAsset(address(wpros)), address(vtoken), "asset mapping");
        assertEq(bridgeVault.vTokenAddressCount(), 1, "count");
        assertEq(bridgeVault.weth(), address(0), "weth unset");
    }

    function test_WithdrawToken_ShouldTransferWpros_WhenNotIsWeth() external {
        _fundWrapped(wpros, address(bridgeVault), 50 ether);

        vm.prank(address(vtoken));
        bridgeVault.withdrawToken(address(wpros), alice, 20 ether);

        assertEq(wpros.balanceOf(alice), 20 ether, "recipient balance");
        assertEq(wpros.balanceOf(address(bridgeVault)), 30 ether, "vault balance");
    }

    function test_WithdrawToken_ShouldPayNative_WhenIsWeth() external {
        BridgeVault nativeVaultImplementation = new BridgeVault();
        bytes memory initData =
            abi.encodeWithSelector(BridgeVault.initialize.selector, owner, address(vtoken), true, address(0x1234));
        ERC1967Proxy nativeVaultProxy = new ERC1967Proxy(address(nativeVaultImplementation), initData);
        BridgeVault nativeVault = BridgeVault(payable(address(nativeVaultProxy)));

        vm.deal(address(nativeVault), 25 ether);

        assertEq(nativeVault.getBalance(address(wpros)), 25 ether, "native balance tracked");
        assertEq(wpros.balanceOf(address(nativeVault)), 0, "erc20 balance ignored");

        uint256 aliceBefore = alice.balance;
        vm.prank(address(vtoken));
        nativeVault.withdrawToken(address(wpros), alice, 10 ether);

        assertEq(alice.balance - aliceBefore, 10 ether, "native payout");
        assertEq(nativeVault.getBalance(address(wpros)), 15 ether, "remaining native");
    }

    function test_WithdrawToken_ShouldRevert_WhenNotVToken() external {
        vm.prank(outsider);
        vm.expectRevert(abi.encodeWithSelector(BridgeVault.NotVTokenContract.selector, outsider));
        bridgeVault.withdrawToken(address(wpros), alice, 1 ether);
    }

    function test_WithdrawToken_ShouldRevert_WhenWrongToken() external {
        address wrongToken = makeAddr("wrongToken");

        vm.prank(address(vtoken));
        vm.expectRevert(abi.encodeWithSelector(BridgeVault.InvalidWithdrawToken.selector, wrongToken, address(wpros)));
        bridgeVault.withdrawToken(wrongToken, alice, 1 ether);
    }

    function test_SetVToken_ShouldUnregisterAndClearWeth() external {
        BridgeVault nativeVaultImplementation = new BridgeVault();
        bytes memory initData =
            abi.encodeWithSelector(BridgeVault.initialize.selector, owner, address(vtoken), true, address(0x1234));
        ERC1967Proxy nativeVaultProxy = new ERC1967Proxy(address(nativeVaultImplementation), initData);
        BridgeVault nativeVault = BridgeVault(payable(address(nativeVaultProxy)));

        assertEq(nativeVault.weth(), address(wpros), "weth configured");

        vm.prank(owner);
        nativeVault.setVToken(address(vtoken), false, true);

        assertFalse(nativeVault.vTokenAddresses(address(vtoken)), "unregistered");
        assertEq(nativeVault.weth(), address(0), "weth cleared");
        assertEq(nativeVault.vTokenAddressCount(), 0, "count cleared");
    }

    function test_EmergencyWithdraw_ShouldTransferBalance() external {
        _fundWrapped(wpros, address(bridgeVault), 15 ether);

        vm.prank(owner);
        bridgeVault.emergencyWithdraw(address(wpros), alice, 15 ether);

        assertEq(wpros.balanceOf(alice), 15 ether, "emergency payout");
        assertEq(wpros.balanceOf(address(bridgeVault)), 0, "vault empty");
    }
}
