/////////////////////////////////////////////////////////////////////////////////
//   ____   _____  ______  _____    ____    _____  _______     _____   ____    //
//  |  _ \ |_   _||  ____||  __ \  / __ \  / ____||__   __|   |_   _| / __ \   //
//  | |_) |  | |  | |__   | |__) || |  | || (___     | |        | |  | |  | |  //
//  |  _ <   | |  |  __|  |  _  / | |  | | \___ \    | |        | |  | |  | |  //
//  | |_) | _| |_ | |     | | \ \ | |__| | ____) |   | |    _  _| |_ | |__| |  //
//  |____/ |_____||_|     |_|  \_\ \____/ |_____/    |_|   (_)|_____| \____/   //
//                                                                             //
/////////////////////////////////////////////////////////////////////////////////
// SPDX-License-Identifier: GPL-3.0
pragma solidity 0.8.28;

import {IStaking} from "../interfaces/pharos/IStaking.sol";
import {Address} from "@openzeppelin/contracts/utils/Address.sol";
import {
    AccessControlEnumerableUpgradeable
} from "@openzeppelin/contracts-upgradeable/access/extensions/AccessControlEnumerableUpgradeable.sol";
import {ReentrancyGuardUpgradeable} from "@openzeppelin/contracts-upgradeable/utils/ReentrancyGuardUpgradeable.sol";
import {EnumerableSet} from "@openzeppelin/contracts/utils/structs/EnumerableSet.sol";

/// @title SlpPharosProxy
/// @notice A contract that manages Pharos's bonding, unbonding, claiming, and transferring back assets
///         for whitelisted poolIds using a specified collateral token and a Hyperbridge handler.
/// @author Bifrost Team
contract SlpPharosProxy is AccessControlEnumerableUpgradeable, ReentrancyGuardUpgradeable {
    using EnumerableSet for EnumerableSet.Bytes32Set;

    /// @notice Storage structure for the SlpPharosProxy contract.
    struct SlpPharosProxyStorage {
        /// @notice Set of whitelisted validator pool Ids.
        EnumerableSet.Bytes32Set _poolIds;
        /// @notice Address of the bridge vault contract.
        address _bridgeVault;
        /// @notice Address of the rewards vault contract.
        address _rewardsVault;
        /// @notice Total amount bonded.
        uint256 _totalBonded;
        /// @notice Total amount unbonded.
        uint256 _totalUnbonded;
    }

    /// @notice Pharos Staking contract address.
    IStaking public constant STAKING = IStaking(0x4100000000000000000000000000000000000000);

    // keccak256(abi.encode(uint256(keccak256("bifrost.storage.SlpPharosProxy")) - 1)) & ~bytes32(uint256(0xff))
    bytes32 private constant _STORAGE_LOCATION = 0xa0268512d1ad72d0bd2136fed798eeda2422eb8b727c15d33589fbbf5c9a8d00;

    /// @notice Returns the storage location for SlpPharosProxy.
    /// @return $ The storage location for SlpPharosProxy.
    function getSlpPharosProxyStorage() private pure returns (SlpPharosProxyStorage storage $) {
        // solhint-disable-next-line no-inline-assembly
        assembly {
            $.slot := _STORAGE_LOCATION
        }
    }

    /// @notice The role required to perform actions in this contract.
    bytes32 public constant WHITELIST_ROLE = keccak256("WHITELIST_ROLE");

    /// @notice Emitted when a pool id is whitelisted.
    /// @param sender The address that whitelisted the pool id.
    /// @param poolId The address of the whitelisted validator.
    event PoolIdRegistered(address indexed sender, bytes32 indexed poolId);
    /// @notice Emitted when a validator is removed from the whitelist.
    /// @param sender The address that removed the validator.
    /// @param poolId The address of the removed validator.
    event PoolIdUnRegistered(address indexed sender, bytes32 indexed poolId);
    /// @notice Emitted when collateral is bonded to a validator.
    /// @param sender The address that bonded the collateral.
    /// @param poolId The pool id of the validator to which collateral was bonded.
    /// @param amount The amount of collateral deposited.
    event Bonded(address indexed sender, bytes32 indexed poolId, uint256 amount);
    /// @notice Emitted when collateral is unbonded from a validator.
    /// @param sender The address that unbonded the collateral.
    /// @param poolId The pool id of the validator to which collateral was bonded.
    /// @param amount Total amount to withdraw (stake + unclaimed rewards).
    /// @param unlockEpoch Epoch number when the funds become withdrawable
    event Unbonded(address indexed sender, bytes32 indexed poolId, uint256 amount, uint256 indexed unlockEpoch);
    /// @notice Emitted when collateral is claimed after unbonding.
    /// @param sender The address that claimed the collateral.
    /// @param poolId The pool id of the validator from which collateral was claimed.
    /// @param amount The amount of collateral claimed.
    event Claimed(address indexed sender, bytes32 indexed poolId, uint256 amount);
    /// @notice Emitted when rewards are claimed for a validator.
    /// @param sender The address that claimed the rewards.
    /// @param poolId The pool id of the validator for which rewards were claimed.
    /// @param amount The amount of rewards claimed.
    event RewardClaimed(address indexed sender, bytes32 indexed poolId, uint256 amount);
    /// @notice Emitted when collateral is transferred back using the Hyperbridge handler.
    /// @param sender The address that initiated the transfer back.
    /// @param amount The amount of collateral transferred back.
    event TransferredBack(address indexed sender, uint256 amount);
    /// @notice Emitted when ETH is deposited to the contract.
    /// @param sender The address that deposited ETH.
    /// @param amount The amount of ETH deposited.
    event EthDeposited(address indexed sender, uint256 amount);
    /// @notice Emitted when the bridge vault address is updated.
    /// @param sender The address that updated the bridge vault.
    /// @param bridgeVault The new bridge vault address.
    event BridgeVaultSet(address indexed sender, address indexed bridgeVault);

    /// @notice Custom errors for amount exceeding limits.
    error AmountExceeds();
    /// @notice Custom errors for insufficient balance.
    error InsufficientBalance();
    /// @notice Custom error for invalid address.
    error InvalidAddress();
    /// @notice Custom error for pool id already registered.
    error PoolIdAlreadyRegistered();
    /// @notice Custom error for pool id not registered.
    error PoolIdNotRegistered();
    /// @notice Custom error for mismatched array lengths in batch operations.
    error MismatchLength();

    constructor() {
        _disableInitializers();
    }

    modifier checkPoolIdExists(bytes32 poolId) {
        SlpPharosProxyStorage storage $ = getSlpPharosProxyStorage();
        require($._poolIds.contains(poolId), PoolIdNotRegistered());
        _;
    }

    /// @notice Fallback function to receive ETH deposits.
    receive() external payable {
        emit EthDeposited(msg.sender, msg.value);
    }

    /// @notice Initializes the SlpPharosProxy contract.
    /// @param _initialOwner The initial owner of the contract with DEFAULT_ADMIN_ROLE.
    /// @param _whitelists An array of addresses to be granted the WHITELIST_ROLE.
    /// @param _bridgeVault The address of the bridge vault contract.
    /// @param _rewardsVault The address of the rewards vault contract.
    /// @dev This function can only be called once during the contract's lifetime.
    function initialize(
        address _initialOwner,
        address[] memory _whitelists,
        address _bridgeVault,
        address _rewardsVault
    ) public initializer {
        require(_initialOwner != address(0), InvalidAddress());
        require(_bridgeVault != address(0), InvalidAddress());
        require(_rewardsVault != address(0), InvalidAddress());

        __AccessControlEnumerable_init();
        __ReentrancyGuard_init();

        // Granting the DEFAULT_ADMIN_ROLE
        _grantRole(DEFAULT_ADMIN_ROLE, _initialOwner);
        // Granting the WHITELIST_ROLE
        for (uint256 i = 0; i < _whitelists.length; ++i) {
            require(_whitelists[i] != address(0), InvalidAddress());
            _grantRole(WHITELIST_ROLE, _whitelists[i]);
        }

        SlpPharosProxyStorage storage $ = getSlpPharosProxyStorage();
        $._bridgeVault = _bridgeVault;
        $._rewardsVault = _rewardsVault;
    }

    /// @notice Registers a pool id to the whitelist.
    /// @param poolId The pool id to be registered.
    /// @dev Only accounts with the WHITELIST_ROLE can call this function.
    function registerPoolId(bytes32 poolId) external nonReentrant onlyRole(WHITELIST_ROLE) {
        SlpPharosProxyStorage storage $ = getSlpPharosProxyStorage();
        require($._poolIds.add(poolId), PoolIdAlreadyRegistered());

        emit PoolIdRegistered(msg.sender, poolId);
    }

    /// @notice Unregisters a pool id from the whitelist.
    /// @param poolId The pool id to be unregistered.
    /// @dev Only accounts with the WHITELIST_ROLE can call this function.
    function unregisterPoolId(bytes32 poolId) external nonReentrant onlyRole(WHITELIST_ROLE) {
        SlpPharosProxyStorage storage $ = getSlpPharosProxyStorage();
        require($._poolIds.remove(poolId), PoolIdNotRegistered());

        emit PoolIdUnRegistered(msg.sender, poolId);
    }

    /// @notice Bonds a specified amount of collateral to a validator pool.
    /// @param poolId The pool id of the validator to which collateral will be bonded.
    /// @param amount The amount of collateral to bond.
    /// @dev Only accounts with the WHITELIST_ROLE can call this function.
    function bond(bytes32 poolId, uint256 amount)
        external
        nonReentrant
        onlyRole(WHITELIST_ROLE)
        checkPoolIdExists(poolId)
    {
        require(amount <= address(this).balance, AmountExceeds());
        STAKING.delegate{value: amount}(poolId);

        SlpPharosProxyStorage storage $ = getSlpPharosProxyStorage();
        $._totalBonded += amount;

        emit Bonded(msg.sender, poolId, amount);
    }

    /// @notice Bonds collateral to multiple validator pools in a single transaction.
    /// @param poolIds The pool ids of the validators to which collateral will be bonded.
    /// @param amounts The amount of collateral to bond to each pool, index-aligned with `poolIds`.
    /// @dev Every pool id is validated and the total amount is checked against the contract balance
    ///      before any delegation is made, so the call either bonds to every pool or reverts entirely.
    /// @dev Emits one Bonded event per pool.
    /// @dev Only accounts with the WHITELIST_ROLE can call this function.
    function batchBond(bytes32[] calldata poolIds, uint256[] calldata amounts)
        external
        nonReentrant
        onlyRole(WHITELIST_ROLE)
    {
        require(poolIds.length == amounts.length, MismatchLength());

        SlpPharosProxyStorage storage $ = getSlpPharosProxyStorage();
        uint256 totalAmount = 0;
        for (uint256 i = 0; i < poolIds.length; ++i) {
            require($._poolIds.contains(poolIds[i]), PoolIdNotRegistered());
            totalAmount += amounts[i];
        }
        require(totalAmount <= address(this).balance, AmountExceeds());

        for (uint256 i = 0; i < poolIds.length; ++i) {
            STAKING.delegate{value: amounts[i]}(poolIds[i]);
            emit Bonded(msg.sender, poolIds[i], amounts[i]);
        }

        $._totalBonded += totalAmount;
    }

    /// @notice Unbonds collateral from a validator pool.
    /// @param poolId The pool id of the validator from which collateral will be unbond
    /// @dev Unbonding will trigger a cooldown period defined by Pharos, after which the collateral can be claimed.
    /// @dev Only accounts with the WHITELIST_ROLE can call this function.
    /// @dev pooId unbond should waiting about 14 days on mainnet before claiming
    function unbond(bytes32 poolId) external nonReentrant onlyRole(WHITELIST_ROLE) checkPoolIdExists(poolId) {
        (uint256 amount, uint256 unlockEpoch) = STAKING.undelegate(poolId);

        SlpPharosProxyStorage storage $ = getSlpPharosProxyStorage();
        $._totalUnbonded += amount;

        emit Unbonded(msg.sender, poolId, amount, unlockEpoch);
    }

    /// @notice Unbonds collateral from multiple validator pools in a single transaction.
    /// @param poolIds The pool ids of the validators from which collateral will be unbonded.
    /// @dev Unbonding will trigger a cooldown period defined by Pharos, after which the collateral can be claimed.
    /// @dev Emits one Unbonded event per pool; the call reverts entirely if any pool id is not registered.
    /// @dev Only accounts with the WHITELIST_ROLE can call this function.
    function batchUnbond(bytes32[] calldata poolIds) external nonReentrant onlyRole(WHITELIST_ROLE) {
        SlpPharosProxyStorage storage $ = getSlpPharosProxyStorage();
        uint256 totalAmount = 0;

        for (uint256 i = 0; i < poolIds.length; ++i) {
            require($._poolIds.contains(poolIds[i]), PoolIdNotRegistered());
            (uint256 amount, uint256 unlockEpoch) = STAKING.undelegate(poolIds[i]);
            totalAmount += amount;

            emit Unbonded(msg.sender, poolIds[i], amount, unlockEpoch);
        }

        $._totalUnbonded += totalAmount;
    }

    /// @notice Claims collateral after the unbonding cooldown period has passed.
    /// @param poolId The pool id of the validator from which collateral will be claimed.
    /// @dev The collateral can only be claimed after the unlock epoch has been reached.
    /// @dev Only accounts with the WHITELIST_ROLE can call this function.
    function claim(bytes32 poolId) external nonReentrant onlyRole(WHITELIST_ROLE) checkPoolIdExists(poolId) {
        uint256 beforeClaimBalance = address(this).balance;
        STAKING.claimStake(poolId);
        uint256 afterClaimBalance = address(this).balance;
        uint256 claimAmount = afterClaimBalance - beforeClaimBalance;

        emit Claimed(msg.sender, poolId, claimAmount);
    }

    /// @notice Claims collateral from multiple validator pools after their unbonding cooldown periods have passed.
    /// @param poolIds The pool ids of the validators from which collateral will be claimed.
    /// @dev The collateral can only be claimed after each pool's unlock epoch has been reached; the call
    ///      reverts entirely if any pool id is not registered or any single claim fails.
    /// @dev Emits one Claimed event per pool.
    /// @dev Only accounts with the WHITELIST_ROLE can call this function.
    function batchClaim(bytes32[] calldata poolIds) external nonReentrant onlyRole(WHITELIST_ROLE) {
        SlpPharosProxyStorage storage $ = getSlpPharosProxyStorage();

        for (uint256 i = 0; i < poolIds.length; ++i) {
            require($._poolIds.contains(poolIds[i]), PoolIdNotRegistered());
            uint256 beforeClaimBalance = address(this).balance;
            STAKING.claimStake(poolIds[i]);
            uint256 afterClaimBalance = address(this).balance;
            uint256 claimAmount = afterClaimBalance - beforeClaimBalance;

            emit Claimed(msg.sender, poolIds[i], claimAmount);
        }
    }

    /// @notice Claims rewards for a specified validator pool and transfer to rewards vault
    /// @param poolId The pool id of the validator for which rewards will be claimed.
    function claimRewardToRewardsVault(bytes32 poolId) external nonReentrant onlyRole(WHITELIST_ROLE) checkPoolIdExists(poolId) {
        uint256 rewardAmount = STAKING.claimReward(poolId);
        Address.sendValue(payable(getSlpPharosProxyStorage()._rewardsVault), rewardAmount);

        emit RewardClaimed(msg.sender, poolId, rewardAmount);
    }

    /// @notice Claims rewards from multiple validator pools and forwards them to the rewards vault in a single transaction.
    /// @param poolIds The pool ids of the validators for which rewards will be claimed.
    /// @dev Rewards are claimed pool by pool and forwarded to the rewards vault as one aggregated transfer at the end,
    ///      so the vault sees a single deposit while one RewardClaimed event is still emitted per pool.
    /// @dev The call reverts entirely if any pool id is not registered or any single claim fails.
    /// @dev Only accounts with the WHITELIST_ROLE can call this function.
    function batchClaimRewardToRewardsVault(bytes32[] calldata poolIds) external nonReentrant onlyRole(WHITELIST_ROLE) {
        SlpPharosProxyStorage storage $ = getSlpPharosProxyStorage();
        uint256 totalRewardAmount = 0;

        for (uint256 i = 0; i < poolIds.length; ++i) {
            require($._poolIds.contains(poolIds[i]), PoolIdNotRegistered());
            uint256 rewardAmount = STAKING.claimReward(poolIds[i]);
            totalRewardAmount += rewardAmount;

            emit RewardClaimed(msg.sender, poolIds[i], rewardAmount);
        }

        Address.sendValue(payable($._rewardsVault), totalRewardAmount);
    }

    /// @notice Compounds rewards for a specified validator pool.
    /// @param poolId The pool id of the validator for which rewards will be compounded.
    function compoundRewards(bytes32 poolId) external nonReentrant onlyRole(WHITELIST_ROLE) checkPoolIdExists(poolId) {
        STAKING.compoundRewards(poolId);
    }

    /// @notice Settles rewards for a specified validator pool.
    /// @param poolId The pool id of the validator for which rewards will be settled.
    function settleReward(bytes32 poolId) external nonReentrant onlyRole(WHITELIST_ROLE) checkPoolIdExists(poolId) {
        STAKING.settleReward(poolId);
    }

    /// @notice Settles rewards for multiple validator pools in a single transaction.
    /// @param poolIds The pool ids of the validators for which rewards will be settled.
    /// @dev Only refreshes the staking contract's reward accounting, no funds are moved; the call reverts
    ///      entirely if any pool id is not registered.
    /// @dev Only accounts with the WHITELIST_ROLE can call this function.
    function batchSettleReward(bytes32[] calldata poolIds) external nonReentrant onlyRole(WHITELIST_ROLE) {
        SlpPharosProxyStorage storage $ = getSlpPharosProxyStorage();

        for (uint256 i = 0; i < poolIds.length; ++i) {
            require($._poolIds.contains(poolIds[i]), PoolIdNotRegistered());
            STAKING.settleReward(poolIds[i]);
        }
    }

    /// @notice Transfers a specified amount of collateral back to a given address using the bridge vault.
    /// @param amount The amount of collateral to transfer back.
    /// @dev Only accounts with the WHITELIST_ROLE can call this function.
    /// @dev Emits a TransferredBack event upon successful transfer.
    function transferBack(uint256 amount)
        external
        nonReentrant
        onlyRole(WHITELIST_ROLE)
    {
        require(amount <= address(this).balance, InsufficientBalance());
        SlpPharosProxyStorage storage $ = getSlpPharosProxyStorage();
        Address.sendValue(payable($._bridgeVault), amount);

        emit TransferredBack(msg.sender, amount);
    }

    /// @notice Updates the bridge vault address used by transferBack.
    /// @param _bridgeVault The new bridge vault address.
    /// @dev Only accounts with the DEFAULT_ADMIN_ROLE can call this function.
    function setBridgeVault(address _bridgeVault) external onlyRole(DEFAULT_ADMIN_ROLE) {
        require(_bridgeVault != address(0), InvalidAddress());
        getSlpPharosProxyStorage()._bridgeVault = _bridgeVault;

        emit BridgeVaultSet(msg.sender, _bridgeVault);
    }

    /// @notice Checks if a given pool id is registered in the whitelist.
    /// @param poolId The pool id to check.
    /// @return True if the pool id is registered, false otherwise.
    function isPoolIdRegistered(bytes32 poolId) external view returns (bool) {
        return getSlpPharosProxyStorage()._poolIds.contains(poolId);
    }

    /// @notice Returns the list of registered pool ids in the whitelist.
    /// @return An array of registered pool ids.
    function registeredPools() external view returns (bytes32[] memory) {
        return getSlpPharosProxyStorage()._poolIds.values();
    }

    /// @notice Returns the address of the bridge vault contract.
    /// @return The address of the bridge vault contract.
    function getBridgeVault() external view returns (address) {
        return getSlpPharosProxyStorage()._bridgeVault;
    }

    /// @notice Returns the address of the rewards vault contract.
    /// @return The address of the rewards vault contract.
    function getRewardsVault() external view returns (address) {
        return getSlpPharosProxyStorage()._rewardsVault;
    }

    /// @notice Returns the total amount bonded.
    /// @return The total bonded amount.
    function totalBonded() external view returns (uint256) {
        return getSlpPharosProxyStorage()._totalBonded;
    }

    /// @notice Returns the total amount unbonded.
    /// @return The total unbonded amount.
    function totalUnbonded() external view returns (uint256) {
        return getSlpPharosProxyStorage()._totalUnbonded;
    }
}
