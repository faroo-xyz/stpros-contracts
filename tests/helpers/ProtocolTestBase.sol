// SPDX-License-Identifier: GPL-3.0
pragma solidity ^0.8.28;

import {Test} from "forge-std/Test.sol";
import {TimelockController} from "@openzeppelin/contracts/governance/TimelockController.sol";
import {WPROS9} from "../mocks/WPROS9.sol";

/// @notice Unit-test governance uses a real timelock; historical tests use LegacyTopology.
abstract contract ProtocolTestBase is Test {
    function _newTimelock() internal returns (address) {
        address[] memory actors = new address[](1);
        actors[0] = address(this);
        return address(new TimelockController(1 days, actors, actors, address(0)));
    }

    function _fundWrapped(WPROS9 token, address to, uint256 amount) internal {
        vm.deal(address(this), address(this).balance + amount);
        token.deposit{value: amount}();
        token.transfer(to, amount);
    }
}
