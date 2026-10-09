// SPDX-License-Identifier: GPL-3.0
pragma solidity ^0.8.28;

import {Ownable2StepUpgradeable} from "@openzeppelin/contracts-upgradeable/access/Ownable2StepUpgradeable.sol";
import {PausableUpgradeable} from "@openzeppelin/contracts-upgradeable/utils/PausableUpgradeable.sol";

interface ITimelockDelay {
    function getMinDelay() external view returns (uint256);
}

/// @notice Guardian can pause; ownership, recovery and unpause remain with governance.
abstract contract GuardedOwnableUpgradeable is Ownable2StepUpgradeable, PausableUpgradeable {
    /// @custom:storage-location erc7201:faroo.storage.Guardian
    struct GuardianStorage {
        address guardian;
    }

    bytes32 private constant GUARDIAN_STORAGE = 0x8a182d357ef2af68f85f69b7302e5a52adb7e215ee455b89eee471e22cd8a700;

    error InvalidGuardian();
    error InvalidTimelock();
    error NotGuardianOrOwner();
    error OwnershipRenounceDisabled();

    event GuardianChanged(address indexed previousGuardian, address indexed newGuardian);

    function _guardianStorage() private pure returns (GuardianStorage storage s) {
        assembly ("memory-safe") {
            s.slot := GUARDIAN_STORAGE
        }
    }

    function __GuardedOwnable_init(address initialOwner, address initialGuardian) internal onlyInitializing {
        _validateTimelock(initialOwner);
        __Ownable_init(initialOwner);
        __Ownable2Step_init();
        __Pausable_init();
        _setGuardian(initialGuardian);
    }

    function guardian() public view returns (address) {
        return _guardianStorage().guardian;
    }

    function setGuardian(address newGuardian) external onlyOwner {
        _setGuardian(newGuardian);
    }

    function _setGuardian(address newGuardian) internal {
        if (newGuardian == address(0) || newGuardian == owner()) revert InvalidGuardian();
        emit GuardianChanged(guardian(), newGuardian);
        _guardianStorage().guardian = newGuardian;
    }

    function pause() external {
        if (msg.sender != owner() && msg.sender != guardian()) revert NotGuardianOrOwner();
        _pause();
    }

    function unpause() external onlyOwner {
        _unpause();
    }

    function renounceOwnership() public override onlyOwner {
        revert OwnershipRenounceDisabled();
    }

    function _validateTimelock(address timelock) internal view {
        if (timelock.code.length == 0) revert InvalidTimelock();
        if (ITimelockDelay(timelock).getMinDelay() == 0) revert InvalidTimelock();
    }

    /// @dev Does not reset an existing pause. Ownable2Step clears any pending owner.
    function _migrateGovernance(address newGuardian, address timelock) internal {
        _validateTimelock(timelock);
        _transferOwnership(timelock);
        _setGuardian(newGuardian);
    }
}
