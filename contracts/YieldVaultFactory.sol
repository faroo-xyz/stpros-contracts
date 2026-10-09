// SPDX-License-Identifier: GPL-3.0
pragma solidity ^0.8.28;

import {Initializable} from "@openzeppelin/contracts-upgradeable/proxy/utils/Initializable.sol";
import {GuardedOwnableUpgradeable} from "./access/GuardedOwnableUpgradeable.sol";
import {MigrationGuard} from "./upgrade/MigrationGuard.sol";

import {BeaconProxy} from "@openzeppelin/contracts/proxy/beacon/BeaconProxy.sol";
import {UpgradeableBeacon} from "@openzeppelin/contracts/proxy/beacon/UpgradeableBeacon.sol";

import {YieldVault} from "./YieldVault.sol";

interface IFactoryManagedYieldVault {
    function emergencyCancel() external;
    function retire() external;
}

contract YieldVaultFactory is Initializable, GuardedOwnableUpgradeable, MigrationGuard {
    /// @dev Uses a separately deployed implementation, keeping creation bytecode out of runtime (EIP-170).
    /// Existing beacons are untouched; fresh proxies get independent factory-owned beacons.
    address private immutable INITIAL_VAULT_IMPLEMENTATION;
    /// @notice Thrown when counterparty address is invalid

    error InvalidCounterparty();

    /// @notice Thrown when vault address is not created by factory
    error InvalidVault(address vault);

    /// @notice Thrown when implementation address has no code
    error InvalidImplementation();
    /// @notice Thrown when init params factory is not current factory
    error InvalidFactoryInParams(address factoryInParams);

    UpgradeableBeacon public beacon;

    address[] public allProxies;
    mapping(address => bool) public isFactoryVault;
    mapping(address => bool) public counterpartyWhitelist;
    // Appended and packed into slot 4.
    address public timelock;
    bool public creationClosed;

    error CreationClosed();
    error InvalidGovernance();

    event TimelockChanged(address indexed previousTimelock, address indexed newTimelock);
    event CreationPermanentlyClosed();
    event VaultRetired(address indexed vault);

    event BeaconCreated(address indexed beacon, address indexed implementation);
    event YieldVaultCreated(address indexed proxy, address indexed creator);
    event BeaconUpgraded(address indexed oldImplementation, address indexed newImplementation);
    event CounterpartyWhitelistUpdated(address indexed counterparty, bool allowed);
    event EmergencyCancelled(address indexed vault, address indexed operator);

    /// @custom:oz-upgrades-unsafe-allow constructor
    constructor(address initialVaultImplementation) {
        _disableInitializers();
        if (initialVaultImplementation.code.length == 0) revert InvalidImplementation();
        INITIAL_VAULT_IMPLEMENTATION = initialVaultImplementation;
    }

    function initialize(address initialOwner, address guardian_, address timelock_)
        external
        onlyConstructing
        reinitializer(4)
    {
        if (initialOwner != timelock_) revert InvalidGovernance();
        __GuardedOwnable_init(initialOwner, guardian_);
        _setTimelock(timelock_);

        address implementation = INITIAL_VAULT_IMPLEMENTATION;

        beacon = new UpgradeableBeacon(implementation, address(this));

        emit BeaconCreated(address(beacon), implementation);
    }

    function migrateToR4(address guardian_, address timelock_)
        external
        onlyProxyAdmin
        onlyFromVersion(1)
        reinitializer(4)
    {
        _migrateGovernance(guardian_, timelock_);
        _setTimelock(timelock_);
    }

    function setTimelock(address timelock_) external onlyOwner {
        _setTimelock(timelock_);
    }

    function _setTimelock(address timelock_) internal {
        _validateTimelock(timelock_);
        emit TimelockChanged(timelock, timelock_);
        timelock = timelock_;
    }

    function closeCreation() external onlyOwner {
        creationClosed = true;
        emit CreationPermanentlyClosed();
    }

    function retireVault(address vault) external onlyOwner {
        if (!isFactoryVault[vault]) revert InvalidVault(vault);
        IFactoryManagedYieldVault(vault).retire();
        emit VaultRetired(vault);
    }

    /// @notice Add counterparty address into whitelist
    function addCounterpartyToWhitelist(address counterparty) external onlyOwner {
        if (counterparty == address(0)) {
            revert InvalidCounterparty();
        }

        counterpartyWhitelist[counterparty] = true;
        emit CounterpartyWhitelistUpdated(counterparty, true);
    }

    /// @notice Remove counterparty address from whitelist
    function removeCounterpartyFromWhitelist(address counterparty) external onlyOwner {
        if (counterparty == address(0)) {
            revert InvalidCounterparty();
        }

        counterpartyWhitelist[counterparty] = false;
        emit CounterpartyWhitelistUpdated(counterparty, false);
    }

    function createYieldVault(YieldVault.InitParams calldata params)
        external
        onlyOwner
        whenNotPaused
        returns (address proxyAddr)
    {
        if (creationClosed) revert CreationClosed();
        if (
            params.admin != timelock || params.operator == address(0) || params.operator == params.admin
                || params.operator == params.counterparty || params.operator == params.feeRecipient
                || params.counterparty == params.admin
        ) revert InvalidGovernance();
        if (params.factory != address(this)) {
            revert InvalidFactoryInParams(params.factory);
        }
        if (!counterpartyWhitelist[params.counterparty]) {
            revert InvalidCounterparty();
        }

        bytes memory initData = abi.encodeWithSelector(YieldVault.initialize.selector, params);

        BeaconProxy proxy = new BeaconProxy(address(beacon), initData);

        proxyAddr = address(proxy);

        allProxies.push(proxyAddr);
        isFactoryVault[proxyAddr] = true;

        emit YieldVaultCreated(proxyAddr, msg.sender);

        return proxyAddr;
    }

    /// @notice Cancel an unsettled round, refund funded profit and reopen redemption
    function emergencyCancel(address vault) external onlyOwner {
        if (!isFactoryVault[vault]) {
            revert InvalidVault(vault);
        }

        IFactoryManagedYieldVault(vault).emergencyCancel();
        emit EmergencyCancelled(vault, msg.sender);
    }

    function upgradeBeaconTo(address newImplementation) external onlyOwner {
        if (newImplementation.code.length == 0) {
            revert InvalidImplementation();
        }

        address oldImplementation = beacon.implementation();

        beacon.upgradeTo(newImplementation);

        emit BeaconUpgraded(oldImplementation, newImplementation);
    }

    function getAllProxies() external view returns (address[] memory) {
        return allProxies;
    }

    function totalProxies() external view returns (uint256) {
        return allProxies.length;
    }

    function currentImplementation() external view returns (address) {
        return beacon.implementation();
    }
}
