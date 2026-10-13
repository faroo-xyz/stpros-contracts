// SPDX-License-Identifier: GPL-3.0
pragma solidity ^0.8.28;

import {ERC20} from "@openzeppelin/contracts/token/ERC20/ERC20.sol";
import {IWPROS} from "../../contracts/interfaces/IWPROS.sol";

contract MockWPROS is ERC20, IWPROS {
    constructor() ERC20("Wrapped PROS", "WPROS") {}

    receive() external payable {}

    function deposit() external payable override {
        _mint(msg.sender, msg.value);
    }

    function withdraw(uint256 value) external override {
        _burn(msg.sender, value);
        (bool success,) = msg.sender.call{value: value}("");
        require(success, "withdraw failed");
    }

    function mint(address to, uint256 amount) external {
        _mint(to, amount);
    }
}
