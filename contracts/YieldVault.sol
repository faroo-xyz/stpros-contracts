// SPDX-License-Identifier: GPL-3.0
pragma solidity ^0.8.28;

import {IERC20} from "@openzeppelin/contracts/token/ERC20/IERC20.sol";
import {SafeERC20} from "@openzeppelin/contracts/token/ERC20/utils/SafeERC20.sol";
import {Math} from "@openzeppelin/contracts/utils/math/Math.sol";
import {Initializable} from "@openzeppelin/contracts-upgradeable/proxy/utils/Initializable.sol";
import {ERC4626Upgradeable} from "@openzeppelin/contracts-upgradeable/token/ERC20/extensions/ERC4626Upgradeable.sol";
import {AccessControlUpgradeable} from "@openzeppelin/contracts-upgradeable/access/AccessControlUpgradeable.sol";
import {ReentrancyGuardTransient} from "@openzeppelin/contracts/utils/ReentrancyGuardTransient.sol";

interface IYieldVaultFactory {
    /// @notice Global factory pause switch; when true, subscriptions and profit funding are blocked
    function paused() external view returns (bool);
    function timelock() external view returns (address);
    function approvedPerformanceFeeBps() external view returns (uint16);
}

/// @notice Rolling ERC-4626 vault. OPS manages PROFIT and rounds; only the factory's
/// timelock may authorize LOSS. Redemption is never paused. A1 bounds proposals by
/// a fixed per-round deadline; binding proposals still depend on governance unpause.
contract YieldVault is Initializable, ERC4626Upgradeable, AccessControlUpgradeable, ReentrancyGuardTransient {
    using SafeERC20 for IERC20;
    using Math for uint256;

    /// @custom:oz-upgrades-unsafe-allow constructor
    constructor() {
        _disableInitializers();
    }

    bytes32 public constant OPERATOR_ROLE = keccak256("OPERATOR_ROLE");
    // Owner-approved round bounds and A1 deadline policy.
    uint256 public constant MIN_OPEN_WINDOW = 1 days;
    uint256 public constant MAX_OPEN_WINDOW = 90 days;
    uint256 public constant MAX_LOCK_DURATION = 365 days;
    uint256 public constant MAX_OPEN_DELAY = 30 days;
    uint256 public constant SETTLEMENT_GRACE = 30 days;
    uint256 public constant MIN_PROPOSAL_WINDOW = 7 days;
    uint256 public constant MAX_SETTLE_TIMELOCK = SETTLEMENT_GRACE - MIN_PROPOSAL_WINDOW;
    uint256 public constant MAX_LOSS_BPS = 5_000;
    uint256 private constant BPS_DENOMINATOR = 10_000;
    /// @notice Hard ceiling for newly opened rounds; historical fee snapshots remain valid.
    uint256 public constant MAX_PERFORMANCE_FEE_BPS = 3_000;
    /// @notice Minimum lock period duration for each round
    uint256 public constant MIN_LOCK_DURATION = 1;
    /// @notice Minimum settlement timelock duration for each round
    uint256 public constant MIN_SETTLE_TIMELOCK = 1;
    /// @notice Maximum single extension allowed for an active open period
    uint256 public constant MAX_OPEN_EXTENSION = 30 days;
    /// @notice Per-round lifecycle phase (see state diagram in contract docs)

    enum Phase {
        OPEN, // Open period: deposit + redeem allowed until openDeadline, then automatically LOCKED
        LOCKED, // Lock period: deposit / redeem blocked; stays until governance submits settlement proposal
        SETTLE_PROPOSED, // Settlement proposal window: fundProfit / cancel / finalize
        SETTLED // Redemption period: redeem allowed; stays until governance opens the next round

    }

    /// @notice Settlement mode
    enum SettleMode {
        PROFIT, // Profit: externally funded yield added to managed assets and distributed pro-rata
        LOSS // Loss: managed assets reduced by loss amount and paid to counterparty

    }

    /// @notice Per-round parameters configured by governance when opening a round
    struct RoundParams {
        /// @notice Open period duration in seconds
        uint256 openWindow;
        /// @notice Lock period duration in seconds
        uint256 lockDuration;
        /// @notice Settlement timelock window in seconds
        uint256 settleTimelockWindow;
        /// @notice Total running cap for this round, in asset units (cumulative inventory cap)
        uint256 roundCap;
        /// @notice Per-address subscription cap in asset units; 0 means no limit
        uint256 perAddressCap;
        /// @notice Minimum subscription size per transaction, in asset units
        uint256 minSubscription;
        /// @notice Performance fee rate in basis points, where 10000 = 100%
        uint256 performanceFeeBps;
        /// @notice Maximum single-round loss rate in basis points
        uint256 maxLossBps;
        /// @notice Open period start timestamp; 0 starts immediately when the round is opened
        uint256 openedAt;
    }

    /// @notice Initialization parameters written once at vault creation
    struct InitParams {
        /// @notice Underlying asset address (stPROS)
        address asset;
        /// @notice Factory contract address used to read the global pause state
        address factory;
        /// @notice Default admin / governance (`DEFAULT_ADMIN_ROLE`)
        address admin;
        /// @notice Counterparty bound to this vault; receives loss deduction on LOSS settlement
        address counterparty;
        /// @notice Performance fee recipient
        address feeRecipient;
        /// @notice Vault share token name
        string name;
        /// @notice Vault share token symbol
        string symbol;
        /// @notice First-round parameters; the first round is configured during initialization
        RoundParams firstRound;
        address operator;
    }

    // =================== Errors ===================
    /// @notice Invalid address parameter such as zero address or mismatched caller
    error InvalidAddress();
    /// @notice Current phase does not match the expected phase
    error InvalidPhase(Phase current);
    /// @notice Factory is paused and sensitive actions are blocked
    error FactoryPaused();
    /// @notice Subscription amount is below the minimum
    error BelowMinSubscription(uint256 amount, uint256 min);
    /// @notice Lock period has not matured yet
    error VaultNotMatured();
    /// @notice Invalid settlement input
    error InvalidSettlementInput();
    /// @notice Loss amount exceeds the allowed managed-asset loss
    error LossExceedManaged(uint256 loss, uint256 managed);
    /// @notice Profit proposal has already been funded
    error ProfitAlreadyFunded();
    /// @notice Profit proposal has not been funded yet and cannot be finalized
    error ProfitNotFunded();
    /// @notice Settlement timelock has not expired
    error TimelockNotExpired(uint256 unlockAt);
    /// @notice Invalid settlement mode for the current flow
    error InvalidSettleMode();
    /// @notice Invalid round parameters
    error InvalidRoundParams();
    error UnapprovedPerformanceFee(uint256 requestedFeeBps, uint256 approvedFeeBps);

    // =================== Events ===================
    /// @notice New round entered the open period
    /// @param roundIndex Round index (starts at 1)
    /// @param openedAt Open period start timestamp
    /// @param openDeadline Open period end timestamp
    /// @param roundCap Total running cap for this round
    event RoundOpened(uint256 indexed roundIndex, uint256 openedAt, uint256 openDeadline, uint256 roundCap);
    /// @notice Open period deadline extended by governance
    /// @param roundIndex Round index
    /// @param previousOpenDeadline Previous open period end timestamp
    /// @param newOpenDeadline New open period end timestamp
    event OpenPeriodExtended(uint256 indexed roundIndex, uint256 previousOpenDeadline, uint256 newOpenDeadline);
    /// @notice Open period closed; round entered lock period
    /// @param roundIndex Round index
    /// @param lockedAt Lock period start timestamp
    /// @param lockMaturity Lock period maturity timestamp
    event RoundLocked(uint256 indexed roundIndex, uint256 lockedAt, uint256 lockMaturity);
    /// @notice Settlement proposal submitted
    /// @param roundIndex Round index
    /// @param mode Settlement mode
    /// @param amount Settlement amount
    /// @param proposedAt Proposal timestamp
    /// @param funded Whether the profit proposal is already funded
    /// @param funder Profit funder address
    event SettlementProposed(
        uint256 indexed roundIndex,
        SettleMode indexed mode,
        uint256 amount,
        uint256 proposedAt,
        bool funded,
        address indexed funder
    );
    /// @notice Profit funding succeeded
    /// @param funder Payer address
    /// @param amount Funded amount
    event ProfitFunded(address indexed funder, uint256 amount);
    /// @notice Refund completed when a settlement proposal is cancelled
    /// @param funder Refund recipient, i.e. the original funder
    /// @param amount Refund amount
    event ProfitRefunded(address indexed funder, uint256 amount);
    /// @notice Settlement proposal cancelled
    /// @param roundIndex Round index
    /// @param at Cancellation timestamp
    event SettlementCancelled(uint256 indexed roundIndex, uint256 at);
    /// @notice Settlement finalized
    /// @param roundIndex Round index
    /// @param mode Settlement mode
    /// @param amount Settlement amount
    /// @param netProfit Net profit in PROFIT mode
    /// @param fee Performance fee in PROFIT mode
    /// @param settledAt Finalization timestamp
    event SettlementFinalized(
        uint256 indexed roundIndex,
        SettleMode indexed mode,
        uint256 amount,
        uint256 netProfit,
        uint256 fee,
        uint256 settledAt
    );
    /// @notice Loss deduction paid to counterparty on LOSS settlement
    /// @param counterparty Counterparty address
    /// @param amount Payment amount
    event CounterpartyProceedsPaid(address indexed counterparty, uint256 amount);
    /// @notice Vault emergency-cancelled and left in SETTLED
    /// @param at Cancellation timestamp
    event EmergencyCancelled(uint256 at);
    // =================== State ===================
    /// @notice Factory address for reading global pause state and triggering emergency cancel

    address public factory;
    /// @notice Counterparty that receives loss proceeds on LOSS settlement
    address public counterparty;
    /// @notice Fee recipient; used only in PROFIT mode
    address public feeRecipient;

    /// @notice Current round index (starts at 1 after the first round opens)
    uint256 public roundIndex;
    /// @notice Current business phase
    Phase private _phase;

    // --- Current round parameters (set at open, immutable within the round) ---
    /// @notice Current round open period duration in seconds
    uint256 public openWindow;
    /// @notice Current round lock period duration in seconds
    uint256 public lockDuration;
    /// @notice Current round settlement timelock window in seconds
    uint256 public settleTimelockWindow;
    /// @notice Current round total running cap in asset units
    uint256 public roundCap;
    /// @notice Per-address subscription cap in asset units (0 = no limit)
    uint256 public perAddressCap;
    /// @notice Current round minimum subscription size
    uint256 public minSubscription;
    /// @notice Current round performance fee rate in bps
    uint256 public performanceFeeBps;
    /// @notice Current round maximum loss rate in bps
    uint256 public maxLossBps;

    // --- Current round timeline ---
    /// @notice Current open period start timestamp
    uint256 public openedAt;
    /// @notice Current open period end timestamp
    uint256 public openDeadline;
    /// @notice Current lock period start timestamp
    uint256 private _lockedAt;
    /// @notice Current settlement proposal submission timestamp
    uint256 public settleProposedAt;
    /// @notice Timestamp when the current round finalized into SETTLED
    uint256 public settledAt;

    // --- Current round settlement ---
    /// @notice Settlement mode for the current proposed / finalized round
    SettleMode public settleMode;
    /// @notice Proposed settlement amount (profit or loss)
    uint256 public settleAmount;
    /// @notice Whether the current PROFIT proposal has been funded
    bool public profitFunded;
    /// @notice Funder address used for refund if a profit proposal is cancelled
    address public profitFunder;

    /// @notice Single cumulative managed-asset figure backing ERC-4626 `totalAssets()`
    uint256 public totalManagedAssets;
    /// @notice Appended slot 22; retirement cannot be undone by stale roles.
    bool public retired;

    error VaultRetired();
    error NotTimelock();
    error InvalidRoleRecipient();
    error ProposalCutoffPassed();

    event Retired(uint256 at);
    event ParSettlementForced(uint256 indexed roundIndex, uint256 at);

    // =================== Modifiers ===================
    modifier whenFactoryNotPaused() {
        if (IYieldVaultFactory(factory).paused()) {
            revert FactoryPaused();
        }
        _;
    }

    modifier onlyFactory() {
        if (_msgSender() != factory) {
            revert InvalidAddress();
        }
        _;
    }

    // =================== Lifecycle ===================
    /// @notice Initialize the vault, grant governance roles, and open the first round
    /// @param p Initialization parameters including first-round config
    function initialize(InitParams calldata p) external initializer {
        if (
            p.asset == address(0) || p.factory == address(0) || p.admin == address(0) || p.counterparty == address(0)
                || p.feeRecipient == address(0) || p.operator == address(0)
        ) {
            revert InvalidAddress();
        }

        if (msg.sender != p.factory) revert InvalidAddress();
        // A1.5: authority dependencies must exist before guarded role grants.
        factory = p.factory;
        counterparty = p.counterparty;
        feeRecipient = p.feeRecipient;
        address tl = IYieldVaultFactory(factory).timelock();
        if (tl == address(0) || p.admin != tl || counterparty == tl) revert InvalidAddress();
        __ERC20_init(p.name, p.symbol);
        __ERC4626_init(IERC20(p.asset));
        __AccessControl_init();
        _grantRole(DEFAULT_ADMIN_ROLE, p.admin);
        _grantRole(OPERATOR_ROLE, p.operator);

        // Configure the first round (roundIndex becomes 1).
        _openRound(p.firstRound);
    }

    /// @notice Governance opens the next round with new parameters while in SETTLED
    /// @dev Callable only by OPS. Reverts after permanent retirement; emergency cancel alone is not terminal.
    /// @param params Parameters for the round being opened
    function openNextRound(RoundParams calldata params) external onlyRole(OPERATOR_ROLE) {
        if (retired) revert VaultRetired();
        Phase currentPhase = _currentPhase();
        if (currentPhase != Phase.SETTLED) {
            revert InvalidPhase(currentPhase);
        }
        _openRound(params);
    }

    /// @notice Governance extends the active open period by a duration in seconds
    /// @dev Callable only by OPS before the current open window expires.
    /// @param extension Additional seconds to add to the current open window
    function extendOpenPeriod(uint256 extension) external onlyRole(OPERATOR_ROLE) {
        if (_phase != Phase.OPEN || block.timestamp >= openDeadline) {
            revert InvalidPhase(_currentPhase());
        }
        if (extension == 0 || extension > MAX_OPEN_EXTENSION || openWindow + extension > MAX_OPEN_WINDOW) {
            revert InvalidRoundParams();
        }

        uint256 previousOpenDeadline = openDeadline;
        openWindow += extension;
        openDeadline = previousOpenDeadline + extension;

        emit OpenPeriodExtended(roundIndex, previousOpenDeadline, openDeadline);
    }

    /// @notice Governance submits a settlement proposal for a matured locked round
    /// @dev PROFIT requires OPS; any LOSS proposal or replacement requires Timelock. Allowed from LOCKED (after maturity) or to replace a SETTLE_PROPOSED proposal.
    /// @param mode Settlement mode: PROFIT or LOSS
    /// @param amount Settlement amount, i.e. profit or loss
    /// @param fundFrom Optional immediate funder in PROFIT mode; must be address(0) in LOSS mode
    function proposeSettlement(SettleMode mode, uint256 amount, address fundFrom)
        external
        whenFactoryNotPaused
        nonReentrant
    {
        bool needsTimelock =
            mode == SettleMode.LOSS || (_phase == Phase.SETTLE_PROPOSED && settleMode == SettleMode.LOSS);
        if (needsTimelock) {
            if (msg.sender != IYieldVaultFactory(factory).timelock()) revert NotTimelock();
        } else {
            _checkRole(OPERATOR_ROLE);
        }
        _syncOpenPeriodIfNeeded();
        Phase currentPhase = _phase;
        if (currentPhase != Phase.LOCKED && currentPhase != Phase.SETTLE_PROPOSED) {
            revert InvalidPhase(currentPhase);
        }
        // When proposing from LOCKED, the lock period must have matured first.
        if (currentPhase == Phase.LOCKED && block.timestamp < _lockedAt + lockDuration) {
            revert VaultNotMatured();
        }
        if (block.timestamp > settlementDeadline() || settleTimelockWindow > settlementDeadline() - block.timestamp) {
            revert ProposalCutoffPassed();
        }
        // Replacing an existing proposal: refund first, then overwrite.
        if (currentPhase == Phase.SETTLE_PROPOSED) {
            _cancelProposedSettlement(false);
        }

        settleMode = mode;
        settleAmount = amount;
        settleProposedAt = block.timestamp;
        profitFunded = false;
        profitFunder = address(0);

        if (mode == SettleMode.PROFIT) {
            if (fundFrom != address(0) && amount > 0) {
                // Governance may only fund from its own balance, not third-party allowances.
                if (fundFrom != _msgSender()) {
                    revert InvalidSettlementInput();
                }
                IERC20(address(asset())).safeTransferFrom(fundFrom, address(this), amount);
                profitFunded = true;
                profitFunder = fundFrom;
            } else if (amount == 0) {
                profitFunded = true;
            }
        } else if (mode == SettleMode.LOSS) {
            if (fundFrom != address(0)) {
                revert InvalidSettlementInput();
            }
            if (amount > totalManagedAssets.mulDiv(maxLossBps, BPS_DENOMINATOR, Math.Rounding.Floor)) {
                revert LossExceedManaged(amount, totalManagedAssets);
            }
            profitFunded = true;
        } else {
            revert InvalidSettleMode();
        }

        _phase = Phase.SETTLE_PROPOSED;
        emit SettlementProposed(roundIndex, mode, amount, settleProposedAt, profitFunded, profitFunder);
    }

    /// @notice Anyone may fund a PROFIT proposal once
    function fundProfit() external whenFactoryNotPaused nonReentrant {
        if (_phase != Phase.SETTLE_PROPOSED) {
            revert InvalidPhase(_phase);
        }
        if (settleMode != SettleMode.PROFIT) {
            revert InvalidSettleMode();
        }
        if (profitFunded) {
            revert ProfitAlreadyFunded();
        }
        if (settleAmount > 0) {
            IERC20(address(asset())).safeTransferFrom(_msgSender(), address(this), settleAmount);
        }
        profitFunded = true;
        profitFunder = _msgSender();
        emit ProfitFunded(_msgSender(), settleAmount);
    }

    /// @notice Governance cancels the current proposal; any funded profit is refunded
    function cancelProposedSettlement() external nonReentrant {
        if (msg.sender != IYieldVaultFactory(factory).timelock()) {
            if (settleMode == SettleMode.LOSS) revert NotTimelock();
            _checkRole(OPERATOR_ROLE);
        }
        if (_phase != Phase.SETTLE_PROPOSED) {
            revert InvalidPhase(_phase);
        }
        _cancelProposedSettlement(true);
    }

    /// @notice Finalize settlement after timelock expiry; permissionless
    /// @dev Applies settlement accounting and advances the round to SETTLED.
    function finalize() external whenFactoryNotPaused nonReentrant {
        if (_phase != Phase.SETTLE_PROPOSED) {
            revert InvalidPhase(_phase);
        }
        uint256 unlockAt = settleProposedAt + settleTimelockWindow;
        if (block.timestamp < unlockAt) {
            revert TimelockNotExpired(unlockAt);
        }
        if (!profitFunded) {
            revert ProfitNotFunded();
        }

        _phase = Phase.SETTLED;
        settledAt = block.timestamp;

        uint256 fee;
        uint256 netProfit;

        if (settleMode == SettleMode.PROFIT) {
            fee = settleAmount.mulDiv(performanceFeeBps, BPS_DENOMINATOR, Math.Rounding.Floor);
            netProfit = settleAmount - fee;
            // Net profit is added to managed assets (remaining holders auto-compound).
            totalManagedAssets += netProfit;
            if (fee > 0) {
                IERC20(address(asset())).safeTransfer(feeRecipient, fee);
            }
        } else {
            // Loss is deducted from managed assets and paid to counterparty.
            totalManagedAssets -= settleAmount;
            if (settleAmount > 0) {
                IERC20(address(asset())).safeTransfer(counterparty, settleAmount);
                emit CounterpartyProceedsPaid(counterparty, settleAmount);
            }
        }

        emit SettlementFinalized(roundIndex, settleMode, settleAmount, netProfit, fee, settledAt);
    }

    /// @notice Factory-triggered emergency cancel; leaves vault in SETTLED at principal pro-rata
    /// @dev Callable before settlement. No profit / loss is applied, so redemption returns principal.
    function emergencyCancel() external onlyFactory nonReentrant {
        Phase currentPhase = _currentPhase();
        if (currentPhase == Phase.SETTLED) {
            revert InvalidPhase(currentPhase);
        }
        // Refund any funded profit proposal before settling into SETTLED.
        if (_phase == Phase.SETTLE_PROPOSED) {
            _cancelProposedSettlement(false);
        }

        _phase = Phase.SETTLED;
        settledAt = block.timestamp;
        emit EmergencyCancelled(block.timestamp);
    }

    function settlementDeadline() public view returns (uint256) {
        return openDeadline + lockDuration + SETTLEMENT_GRACE;
    }

    /// @notice Non-pausable escape for non-binding rounds. No asset transfer or refund.
    function forceParSettlement() external nonReentrant {
        _syncOpenPeriodIfNeeded();
        if (block.timestamp < settlementDeadline()) revert VaultNotMatured();
        if (_phase != Phase.LOCKED && !(_phase == Phase.SETTLE_PROPOSED && !profitFunded)) {
            revert InvalidPhase(_phase);
        }
        settleMode = SettleMode.PROFIT;
        settleAmount = 0;
        settleProposedAt = 0;
        profitFunded = false;
        profitFunder = address(0);
        _phase = Phase.SETTLED;
        settledAt = block.timestamp;
        emit ParSettlementForced(roundIndex, block.timestamp);
    }

    /// @notice Terminal settlement, including refunds of any funded PROFIT proposal.
    function retire() external onlyFactory nonReentrant {
        if (retired) revert VaultRetired();
        if (_phase == Phase.SETTLE_PROPOSED) _cancelProposedSettlement(false);
        _phase = Phase.SETTLED;
        settledAt = block.timestamp;
        retired = true;
        emit Retired(block.timestamp);
    }

    function _grantRole(bytes32 role, address account) internal override returns (bool) {
        address tl = IYieldVaultFactory(factory).timelock();
        if (role == DEFAULT_ADMIN_ROLE && (tl == address(0) || account != tl)) revert InvalidRoleRecipient();
        if (
            role == OPERATOR_ROLE
                && (account == address(0) || account == tl || account == counterparty || account == feeRecipient)
        ) {
            revert InvalidRoleRecipient();
        }
        return super._grantRole(role, account);
    }

    function _redeemWindowOpen() internal view returns (bool) {
        return (_phase == Phase.OPEN && block.timestamp < openDeadline) || _phase == Phase.SETTLED;
    }

    // =================== ERC4626 ===================
    /// @notice ERC-4626 total assets backed by the cumulative managed-asset figure
    function totalAssets() public view override returns (uint256) {
        return totalManagedAssets;
    }

    /// @notice Increase virtual share precision to reduce first-depositor inflation attack feasibility
    function _decimalsOffset() internal pure override returns (uint8) {
        return 6;
    }

    /// @notice Current business phase, with OPEN treated as LOCKED after openDeadline.
    function phase() public view returns (Phase) {
        return _currentPhase();
    }

    /// @notice Current lock period start timestamp, set to openDeadline once OPEN has expired.
    function lockedAt() public view returns (uint256) {
        return _currentLockedAt();
    }

    function maxDeposit(address receiver) public view override returns (uint256) {
        if (!_isOpenPeriodActive() || IYieldVaultFactory(factory).paused()) {
            return 0;
        }
        uint256 managed = totalManagedAssets;
        uint256 roundLeft = roundCap > managed ? roundCap - managed : 0;
        if (perAddressCap == 0) {
            return roundLeft;
        }
        uint256 userValue = convertToAssets(balanceOf(receiver));
        uint256 userLeft = perAddressCap > userValue ? perAddressCap - userValue : 0;
        return Math.min(roundLeft, userLeft);
    }

    function maxMint(address receiver) public view override returns (uint256) {
        return convertToShares(maxDeposit(receiver));
    }

    function maxRedeem(address owner) public view override returns (uint256) {
        if (_redeemWindowOpen() && totalManagedAssets > 0) {
            return balanceOf(owner);
        }
        return 0;
    }

    function maxWithdraw(address owner) public view override returns (uint256) {
        if (!_redeemWindowOpen()) {
            return 0;
        }
        return previewRedeem(balanceOf(owner));
    }

    /// @notice Deposit assets and mint shares; only available during the open period
    function deposit(uint256 assets, address receiver)
        public
        override
        whenFactoryNotPaused
        nonReentrant
        returns (uint256)
    {
        if (!_isOpenPeriodActive()) {
            revert InvalidPhase(_currentPhase());
        }
        if (assets < minSubscription) {
            revert BelowMinSubscription(assets, minSubscription);
        }
        return super.deposit(assets, receiver);
    }

    /// @notice Mint shares for a target amount; only available during the open period
    function mint(uint256 shares, address receiver)
        public
        override
        whenFactoryNotPaused
        nonReentrant
        returns (uint256)
    {
        if (!_isOpenPeriodActive()) {
            revert InvalidPhase(_currentPhase());
        }
        uint256 assets = previewMint(shares);
        if (assets < minSubscription) {
            revert BelowMinSubscription(assets, minSubscription);
        }
        return super.mint(shares, receiver);
    }

    /// @notice Redeem shares for assets; only available in OPEN and SETTLED
    function redeem(uint256 shares, address receiver, address owner) public override nonReentrant returns (uint256) {
        if (!_redeemWindowOpen()) {
            revert InvalidPhase(_currentPhase());
        }
        return super.redeem(shares, receiver, owner);
    }

    /// @notice Withdraw a target amount of assets; only available in OPEN and SETTLED
    function withdraw(uint256 assets, address receiver, address owner) public override nonReentrant returns (uint256) {
        if (!_redeemWindowOpen()) {
            revert InvalidPhase(_currentPhase());
        }
        return super.withdraw(assets, receiver, owner);
    }

    // =================== Internal helpers ===================
    /// @dev Track managed assets on deposit, then run standard ERC-4626 flow.
    function _deposit(address caller, address receiver, uint256 assets, uint256 shares) internal override {
        super._deposit(caller, receiver, assets, shares);
        totalManagedAssets += assets;
    }

    /// @dev Reduce managed assets on withdrawal, then run standard ERC-4626 flow.
    function _withdraw(address caller, address receiver, address owner, uint256 assets, uint256 shares)
        internal
        override
    {
        totalManagedAssets -= assets;
        super._withdraw(caller, receiver, owner, assets, shares);
    }

    /// @dev Whether the vault is within the active open subscription / redemption window.
    function _isOpenPeriodActive() internal view returns (bool) {
        return _phase == Phase.OPEN && block.timestamp >= openedAt && block.timestamp < openDeadline;
    }

    /// @dev Effective phase used by views and entry-point validation.
    function _currentPhase() internal view returns (Phase) {
        if (_phase == Phase.OPEN && block.timestamp >= openDeadline) {
            return Phase.LOCKED;
        }
        return _phase;
    }

    /// @dev Effective lock start used by views before the lazy state write happens.
    function _currentLockedAt() internal view returns (uint256) {
        if (_phase == Phase.OPEN && block.timestamp >= openDeadline) {
            return openDeadline;
        }
        return _lockedAt;
    }

    /// @dev Persist OPEN -> LOCKED on the first successful transition after openDeadline.
    function _syncOpenPeriodIfNeeded() internal {
        if (_phase == Phase.OPEN && block.timestamp >= openDeadline) {
            _phase = Phase.LOCKED;
            _lockedAt = openDeadline;
            emit RoundLocked(roundIndex, _lockedAt, _lockedAt + lockDuration);
        }
    }

    /// @dev Validate and apply round parameters, then enter (or schedule) the open period.
    function _openRound(RoundParams calldata params) internal {
        if (
            params.openWindow < MIN_OPEN_WINDOW || params.openWindow > MAX_OPEN_WINDOW
                || params.lockDuration > MAX_LOCK_DURATION || params.settleTimelockWindow > MAX_SETTLE_TIMELOCK
                || params.lockDuration < MIN_LOCK_DURATION || params.settleTimelockWindow < MIN_SETTLE_TIMELOCK
                || params.roundCap == 0 || params.minSubscription == 0 || params.performanceFeeBps > MAX_PERFORMANCE_FEE_BPS
                || params.maxLossBps > MAX_LOSS_BPS
        ) {
            revert InvalidRoundParams();
        }
        uint256 approvedFee = IYieldVaultFactory(factory).approvedPerformanceFeeBps();
        if (params.performanceFeeBps != approvedFee) {
            revert UnapprovedPerformanceFee(params.performanceFeeBps, approvedFee);
        }
        // New cap must accommodate assets already under management (rolled principal + yield).
        if (params.roundCap < totalManagedAssets) {
            revert InvalidRoundParams();
        }

        roundIndex += 1;

        openWindow = params.openWindow;
        lockDuration = params.lockDuration;
        settleTimelockWindow = params.settleTimelockWindow;
        roundCap = params.roundCap;
        perAddressCap = params.perAddressCap;
        minSubscription = params.minSubscription;
        performanceFeeBps = params.performanceFeeBps;
        maxLossBps = params.maxLossBps;

        if (params.openedAt == 0) {
            openedAt = block.timestamp;
        } else {
            if (params.openedAt < block.timestamp || params.openedAt - block.timestamp > MAX_OPEN_DELAY) {
                revert InvalidRoundParams();
            }
            openedAt = params.openedAt;
        }
        openDeadline = openedAt + params.openWindow;
        _lockedAt = 0;
        settleProposedAt = 0;
        settledAt = 0;

        settleMode = SettleMode.PROFIT;
        settleAmount = 0;
        profitFunded = false;
        profitFunder = address(0);

        _phase = Phase.OPEN;
        emit RoundOpened(roundIndex, openedAt, openDeadline, roundCap);
    }

    /// @dev Refund a funded profit proposal if any, then return to LOCKED.
    function _cancelProposedSettlement(bool emitEvent) internal {
        if (settleMode == SettleMode.PROFIT && profitFunded && settleAmount > 0 && profitFunder != address(0)) {
            IERC20(address(asset())).safeTransfer(profitFunder, settleAmount);
            emit ProfitRefunded(profitFunder, settleAmount);
        }

        settleAmount = 0;
        settleProposedAt = 0;
        profitFunded = false;
        profitFunder = address(0);

        _phase = Phase.LOCKED;

        if (emitEvent) {
            emit SettlementCancelled(roundIndex, block.timestamp);
        }
    }
}
