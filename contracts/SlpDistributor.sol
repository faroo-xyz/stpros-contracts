// SPDX-License-Identifier: GPL-3.0
pragma solidity ^0.8.28;

import {GuardedOwnableUpgradeable} from "./access/GuardedOwnableUpgradeable.sol";
import {MigrationGuard} from "./upgrade/MigrationGuard.sol";
import {ReentrancyGuardTransient} from "@openzeppelin/contracts/utils/ReentrancyGuardTransient.sol";
import {EnumerableSet} from "@openzeppelin/contracts/utils/structs/EnumerableSet.sol";

/**
 * @title SlpDistributor
 * @notice Relay between StPROS and SLP contracts.
 * StPROS forwards native PROS produced by minting to this contract (set as StPROS `slp`),
 * and the keeper later splits the balance across whitelisted SLP contracts.
 * @dev Owner manages the SLP whitelist and the keeper; the keeper can only pay whitelisted SLPs.
 */
contract SlpDistributor is GuardedOwnableUpgradeable, MigrationGuard, ReentrancyGuardTransient {
    using EnumerableSet for EnumerableSet.AddressSet;

    /// @notice One payout instruction
    struct Distribution {
        address slp;
        uint256 amount;
    }

    // =================== State variables ===================

    /// @notice Address allowed to call `distribute`
    address public keeper;

    /// @dev Whitelisted SLP contracts
    EnumerableSet.AddressSet private _slps;

    // =================== Events ===================

    /// @notice Emitted when native PROS is received
    event PROSReceived(address indexed from, uint256 amount);

    /// @notice Emitted when an SLP is added to or removed from the whitelist
    event SlpWhitelistUpdated(address indexed slp, bool allowed);

    /// @notice Emitted when the keeper changes
    event KeeperChanged(address indexed oldKeeper, address indexed newKeeper);

    /// @notice Emitted for each payout
    event Distributed(address indexed slp, uint256 amount);

    // =================== Errors ===================

    error InvalidAddress();
    error NotKeeper(address caller);
    error SlpNotWhitelisted(address slp);
    error ZeroAmount();
    error EmptyDistribution();
    error InsufficientBalance(uint256 requested, uint256 available);
    error TransferFailed(address to, uint256 amount);

    // =================== Modifiers ===================

    modifier onlyKeeper() {
        if (msg.sender != keeper) {
            revert NotKeeper(msg.sender);
        }
        _;
    }

    /// @custom:oz-upgrades-unsafe-allow constructor
    constructor() {
        _disableInitializers();
    }

    /// @param owner_ Contract owner
    /// @param keeper_ Initial keeper; may be zero and set later via `setKeeper`
    /// @param slps_ Initial SLP whitelist; may be empty
    function initialize(address owner_, address keeper_, address[] calldata slps_, address guardian_)
        external
        onlyConstructing
        reinitializer(4)
    {
        __GuardedOwnable_init(owner_, guardian_);
        if (keeper_ != address(0)) {
            _setKeeper(keeper_);
        }
        _setSlps(slps_, true);
    }

    function migrateToR4(address guardian_, address timelock_)
        external
        onlyProxyAdmin
        onlyFromVersion(1)
        reinitializer(4)
    {
        _migrateGovernance(guardian_, timelock_);
    }

    /// @notice Accept native PROS (e.g. forwarded by StPROS on mint)
    receive() external payable {
        emit PROSReceived(msg.sender, msg.value);
    }

    // =================== Owner ===================

    /// @notice Add or remove SLP contracts from the whitelist, owner only
    /// @param slps_ SLP addresses
    /// @param allowed true to whitelist, false to remove
    function setSlps(address[] calldata slps_, bool allowed) external onlyOwner {
        _setSlps(slps_, allowed);
    }

    /// @notice Set the keeper, owner only
    function setKeeper(address keeper_) external onlyOwner {
        if (keeper_ == address(0)) {
            revert InvalidAddress();
        }
        _setKeeper(keeper_);
    }

    // =================== Keeper ===================

    /// @notice Send native PROS to whitelisted SLPs, keeper only
    /// @param distributions List of (slp, amount); the same SLP may appear more than once
    function distribute(Distribution[] calldata distributions) external onlyKeeper whenNotPaused nonReentrant {
        uint256 length = distributions.length;
        if (length == 0) {
            revert EmptyDistribution();
        }

        uint256 total;
        for (uint256 i; i < length; ++i) {
            Distribution calldata d = distributions[i];
            if (!_slps.contains(d.slp)) {
                revert SlpNotWhitelisted(d.slp);
            }
            if (d.amount == 0) {
                revert ZeroAmount();
            }
            total += d.amount;
        }

        uint256 available = address(this).balance;
        if (total > available) {
            revert InsufficientBalance(total, available);
        }

        for (uint256 i; i < length; ++i) {
            Distribution calldata d = distributions[i];
            (bool success,) = d.slp.call{value: d.amount}("");
            if (!success) {
                revert TransferFailed(d.slp, d.amount);
            }
            emit Distributed(d.slp, d.amount);
        }
    }

    // =================== Views ===================

    /// @notice Whether `slp` is whitelisted
    function isSlp(address slp) external view returns (bool) {
        return _slps.contains(slp);
    }

    /// @notice All whitelisted SLPs
    function getSlps() external view returns (address[] memory) {
        return _slps.values();
    }

    /// @notice Number of whitelisted SLPs
    function slpCount() external view returns (uint256) {
        return _slps.length();
    }

    // =================== Internal ===================

    function _setSlps(address[] calldata slps_, bool allowed) internal {
        for (uint256 i; i < slps_.length; ++i) {
            address slp = slps_[i];
            if (slp == address(0) || (allowed && slp.code.length == 0)) {
                revert InvalidAddress();
            }
            bool changed = allowed ? _slps.add(slp) : _slps.remove(slp);
            if (changed) {
                emit SlpWhitelistUpdated(slp, allowed);
            }
        }
    }

    function _setKeeper(address keeper_) internal {
        address old = keeper;
        keeper = keeper_;
        emit KeeperChanged(old, keeper_);
    }
}
