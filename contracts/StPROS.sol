// SPDX-License-Identifier: GPL-3.0
pragma solidity ^0.8.28;

import {VToken} from "./VToken.sol";
import {IWPROS} from "./interfaces/IWPROS.sol";
import {IERC20} from "@openzeppelin/contracts/token/ERC20/IERC20.sol";
import {Oracle} from "./Oracle.sol";

contract StPROS is VToken {
    /// @custom:oz-upgrades-unsafe-allow constructor
    constructor() {
        _disableInitializers();
    }

    /// @notice Thrown when PROS is not sent
    error PROSNotSent();

    /// @notice Thrown when ETH is sent from non-asset address
    error OnlyAssetCanSendETH();

    error InvalidMigrationState();

    /// @notice Fresh R4 configuration is supplied atomically in the proxy constructor.
    function initialize(
        IERC20 asset_,
        address owner_,
        string memory name_,
        string memory symbol_,
        address oracle_,
        address slp_,
        address payable bridgeVault_,
        address guardian_
    ) public onlyConstructing reinitializer(4) {
        if (oracle_.code.length == 0) revert InvalidAddress();
        __VToken_init(asset_, owner_, name_, symbol_, guardian_);
        oracle = Oracle(oracle_);
        _setSlp(slp_);
        _setBridgeVault(bridgeVault_);
    }

    function migrateFromV1(address slp_, address payable bridgeVault_, address guardian_, address timelock_)
        external
        onlyProxyAdmin
        onlyFromVersion(1)
        reinitializer(4)
        nonReentrant
    {
        if (slp != address(0) || address(bridgeVault) != address(0) || slp_.code.length == 0) {
            revert InvalidMigrationState();
        }
        (uint256 t, uint256 v) = oracle.poolInfo(asset());
        if (t != 0 || v != 0 || oracle.tokenToVToken(asset()) != address(this)) revert InvalidMigrationState();
        _setSlp(slp_);
        _setBridgeVault(bridgeVault_);
        if (!bridgeVault.vTokenAddresses(address(this)) || bridgeVault.weth() != asset()) {
            revert InvalidMigrationState();
        }

        uint256 reserve = totalCanWithdrawAmount;
        uint256 wrapped = IERC20(asset()).balanceOf(address(this));
        uint256 escrow = balanceOf(address(this));
        if (
            reserve != queuedWithdrawal - completedWithdrawal || wrapped < reserve || escrow < reserve
                || wrapped - reserve < totalSupply() - escrow
        ) revert InvalidMigrationState();
        uint256 beforeBridge = bridgeVault_.balance;
        if (reserve != 0) {
            IWPROS(asset()).withdraw(reserve);
            (bool success,) = bridgeVault_.call{value: reserve}("");
            if (!success) revert TransferFailed();
        }
        totalCanWithdrawAmount = 0;
        if (wrapped > reserve) _sendTokenToSlp(wrapped - reserve, true);
        if (escrow != 0) _burn(address(this), escrow);
        oracle.initializePool(asset(), totalSupply(), totalSupply());
        _migrateGovernance(guardian_, timelock_);

        (t, v) = oracle.poolInfo(asset());
        if (
            IERC20(asset()).balanceOf(address(this)) != 0 || balanceOf(address(this)) != 0
                || totalCanWithdrawAmount != 0 || t != totalSupply() || v != totalSupply()
                || bridgeVault_.balance != beforeBridge + reserve || getTotalBalance() < queuedWithdrawal
        ) {
            revert InvalidMigrationState();
        }
    }

    function migrateFromV2(address guardian_, address timelock_)
        external
        onlyProxyAdmin
        onlyFromVersion(2)
        reinitializer(4)
    {
        _migrateGovernance(guardian_, timelock_);
    }

    /// @notice Receive native PROS from WPROS unwrapping.
    /// @dev WPROS/WETH-style `withdraw` pays out via `transfer`, forwarding only 2300 gas.
    /// Do not emit here or `withdraw` / migration will revert.
    receive() external payable override {
        if (msg.sender != address(asset())) {
            revert OnlyAssetCanSendETH();
        }
    }

    function depositWithPROS() external payable whenNotPaused nonReentrant returns (uint256) {
        if (msg.value == 0) {
            revert PROSNotSent();
        }
        uint256 shares = previewDeposit(msg.value);
        _mint(msg.sender, shares);
        oracle.onMint(address(asset()), msg.value, shares);
        _sendTokenToSlp(msg.value, false);
        emit Deposit(msg.sender, msg.sender, msg.value, shares);
        return shares;
    }
}
