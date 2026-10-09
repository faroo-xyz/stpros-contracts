// SPDX-License-Identifier: GPL-3.0
pragma solidity ^0.8.28;

import {Vm} from "forge-std/Vm.sol";
import {LegacyTopology, IWETH9Like} from "../helpers/LegacyTopology.sol";
import {WPROS9} from "../mocks/WPROS9.sol";

contract CheapReceiver {
    function unwrap(IWETH9Like w, uint256 wad) external {
        w.withdraw(wad);
    }

    receive() external payable {}
}

contract ExpensiveReceiver {
    uint256 public hits;

    function unwrap(IWETH9Like w, uint256 wad) external {
        w.withdraw(wad);
    }

    receive() external payable {
        hits += 1; // a cold SSTORE alone exceeds the 2300-gas stipend
    }
}

/// @title WPROS9 differential test (B0)
/// @notice Runs one scenario against the real mainnet WPROS runtime, the real testnet WPROS
///         runtime (both read from chain at the pinned block, test/fixtures/legacy/<chain>/WPROS.json)
///         and the WPROS9 mock, and requires identical observable behaviour.
contract WPROS9Test is LegacyTopology {
    bytes32 internal constant DEPOSIT_TOPIC = keccak256("Deposit(address,uint256)");
    bytes32 internal constant WITHDRAWAL_TOPIC = keccak256("Withdrawal(address,uint256)");
    bytes32 internal constant TRANSFER_TOPIC = keccak256("Transfer(address,address,uint256)");
    bytes32 internal constant APPROVAL_TOPIC = keccak256("Approval(address,address,uint256)");

    struct Observation {
        bool depositEvent;
        uint256 balanceAfterDeposit;
        uint256 balanceSlot3;
        bool totalSupplyIsNativeBalance;
        bool plainSendDeposits;
        bool unknownSelectorWithValueDeposits;
        bool withdrawToEoaPays;
        bool withdrawalEvent;
        bool cheapReceiverUnwraps;
        bool expensiveReceiverUnwraps;
        bool overdrawReverts;
        uint256 overdrawRevertDataLength;
        bool transferFromWithoutAllowanceReverts;
        uint256 allowanceAfterFiniteSpend;
        uint256 allowanceAfterMaxSpend;
        bool transferEvent;
        bool approvalEvent;
        uint8 decimals;
    }

    address internal user = makeAddr("wpros.user");
    address internal spender = makeAddr("wpros.spender");
    address internal sink = makeAddr("wpros.sink");

    function _observe(address w) internal returns (Observation memory o) {
        IWETH9Like weth = IWETH9Like(w);
        o.decimals = weth.decimals();
        vm.deal(user, 100 ether);

        vm.recordLogs();
        vm.prank(user);
        weth.deposit{value: 10 ether}();
        o.depositEvent = _hasTopic(DEPOSIT_TOPIC);
        o.balanceAfterDeposit = weth.balanceOf(user);
        o.balanceSlot3 = uint256(vm.load(w, keccak256(abi.encode(user, uint256(3)))));
        o.totalSupplyIsNativeBalance = weth.totalSupply() == w.balance;

        vm.prank(user);
        (bool ok,) = w.call{value: 1 ether}("");
        o.plainSendDeposits = ok && weth.balanceOf(user) == 11 ether;

        vm.prank(user);
        (ok,) = w.call{value: 1 ether}(abi.encodeWithSignature("doesNotExist()"));
        uint256 expected = o.plainSendDeposits ? 12 ether : 11 ether;
        o.unknownSelectorWithValueDeposits = ok && weth.balanceOf(user) == expected;

        uint256 before = user.balance;
        vm.recordLogs();
        vm.prank(user);
        weth.withdraw(1 ether);
        o.withdrawToEoaPays = user.balance == before + 1 ether;
        o.withdrawalEvent = _hasTopic(WITHDRAWAL_TOPIC);

        CheapReceiver cheap = new CheapReceiver();
        ExpensiveReceiver costly = new ExpensiveReceiver();
        vm.startPrank(user);
        weth.transfer(address(cheap), 1 ether);
        weth.transfer(address(costly), 1 ether);
        vm.stopPrank();
        try cheap.unwrap(weth, 1 ether) {
            o.cheapReceiverUnwraps = address(cheap).balance == 1 ether;
        } catch {}
        try costly.unwrap(weth, 1 ether) {
            o.expensiveReceiverUnwraps = true;
        } catch {}

        vm.prank(sink);
        try weth.withdraw(1) {}
        catch (bytes memory reason) {
            o.overdrawReverts = true;
            o.overdrawRevertDataLength = reason.length;
        }

        vm.prank(spender);
        (ok,) = w.call(abi.encodeWithSignature("transferFrom(address,address,uint256)", user, sink, 1));
        o.transferFromWithoutAllowanceReverts = !ok;

        vm.recordLogs();
        vm.prank(user);
        weth.approve(spender, 5);
        o.approvalEvent = _hasTopic(APPROVAL_TOPIC);
        vm.recordLogs();
        vm.prank(spender);
        (ok,) = w.call(abi.encodeWithSignature("transferFrom(address,address,uint256)", user, sink, 2));
        require(ok, "finite transferFrom");
        o.transferEvent = _hasTopic(TRANSFER_TOPIC);
        o.allowanceAfterFiniteSpend = weth.allowance(user, spender);

        vm.prank(user);
        weth.approve(spender, type(uint256).max);
        vm.prank(spender);
        (ok,) = w.call(abi.encodeWithSignature("transferFrom(address,address,uint256)", user, sink, 2));
        require(ok, "max transferFrom");
        o.allowanceAfterMaxSpend = weth.allowance(user, spender);
    }

    function _hasTopic(bytes32 topic) internal returns (bool) {
        Vm.Log[] memory logs = vm.getRecordedLogs();
        for (uint256 i = 0; i < logs.length; i++) {
            if (logs[i].topics.length > 0 && logs[i].topics[0] == topic) return true;
        }
        return false;
    }

    function _assertSame(Observation memory a, Observation memory b, string memory what) internal pure {
        assertEq(abi.encode(a), abi.encode(b), what);
    }

    function test_mock_matchesMainnetWPROS() public {
        Observation memory real = _observe(_etchWPROS("mainnet"));
        Observation memory mock = _observe(address(new WPROS9()));
        _assertSame(mock, real, "WPROS9 vs mainnet WPROS");
        _assertWeth9Semantics(real);
    }

    function test_mock_matchesTestnetWPROS() public {
        Observation memory real = _observe(_etchWPROS("testnet"));
        Observation memory mock = _observe(address(new WPROS9()));
        _assertSame(mock, real, "WPROS9 vs testnet WPROS");
    }

    /// @dev The facts the mock is documented to reproduce, stated independently.
    function _assertWeth9Semantics(Observation memory o) internal pure {
        assertTrue(o.depositEvent);
        assertEq(o.balanceAfterDeposit, 10 ether);
        assertEq(o.balanceSlot3, 10 ether, "balanceOf mapping at slot 3");
        assertTrue(o.totalSupplyIsNativeBalance);
        assertTrue(o.plainSendDeposits);
        assertTrue(o.withdrawToEoaPays);
        assertTrue(o.withdrawalEvent);
        assertTrue(o.cheapReceiverUnwraps);
        assertFalse(o.expensiveReceiverUnwraps, "2300-gas stipend");
        assertTrue(o.overdrawReverts);
        assertEq(o.overdrawRevertDataLength, 0, "empty revert data");
        assertTrue(o.transferFromWithoutAllowanceReverts);
        assertEq(o.allowanceAfterFiniteSpend, 3);
        assertEq(o.allowanceAfterMaxSpend, type(uint256).max, "infinite allowance not decremented");
        assertEq(o.decimals, 18);
    }
}
