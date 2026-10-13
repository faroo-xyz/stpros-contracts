// SPDX-License-Identifier: GPL-3.0
pragma solidity ^0.8.28;

/// @title VaultModel
/// @notice Independent accounting ghost of one R4 YieldVault: managed assets (TMA), share supply,
///         per-holder shares, the vault's asset custody, settlement, retirement refunds and
///         per-round terms. Written from the approved rules:
///
///         - Shares use ERC-4626 virtual accounting with a 10^6 share offset and one virtual asset:
///           deposit shares = ⌊assets · (supply + 10^6) / (TMA + 1)⌋,
///           redeem assets = ⌊shares · (TMA + 1) / (supply + 10^6)⌋.
///         - PROFIT finalize adds `amount − fee` to TMA, with fee = ⌊amount · feeBps / 10^4⌋ paid
///           to the fee recipient out of the funded amount.
///         - Every ⌊a · b / d⌋ is exact over the whole uint256 domain of its operands (`FullMulDiv`,
///           512-bit product): the model refuses only where the result itself exceeds uint256,
///           the same domain as the production full-precision `mulDiv`.
///         - LOSS finalize removes `amount` from TMA and pays it to the counterparty.
///         - Retirement settles at par: TMA and supply unchanged, and a funded PROFIT proposal is
///           refunded exactly to its funder (an unfunded or LOSS proposal moves nothing).
///         - Migration (beacon upgrade) changes no accounting word and no round term.
///
/// @dev Wrong rules are negative controls only:
///      WRONG_ZERO_DECIMALS_OFFSET uses the ERC-4626 default share offset 10^0 instead of 10^6;
///      WRONG_RETIRE_KEEPS_PROFIT books a funded profit into TMA on retirement instead of refunding.
contract VaultModel {
    enum Rule {
        CORRECT,
        WRONG_ZERO_DECIMALS_OFFSET,
        WRONG_RETIRE_KEEPS_PROFIT
    }

    enum Phase {
        OPEN,
        LOCKED,
        SETTLE_PROPOSED,
        SETTLED
    }

    uint256 internal constant SHARE_OFFSET = 1e6;
    uint256 internal constant BPS = 10_000;

    Rule public immutable rule;
    uint256 public managed;
    uint256 public supply;
    /// @notice Asset tokens the vault itself holds (TMA plus any funded, unsettled profit).
    uint256 public custody;
    Phase public phase;
    bool public retired;
    uint256 public feeBps;

    bool public proposalIsProfit;
    uint256 public proposalAmount;
    bool public proposalFunded;
    address public proposalFunder;

    mapping(address => uint256) public sharesOf;
    mapping(address => uint256) public paidTo;

    constructor(Rule rule_) {
        rule = rule_;
    }

    // ------------------------------------------------------------------ seed (inventory / pre-state)

    function seedVault(uint256 managed_, uint256 supply_, uint256 custody_, Phase phase_, uint256 feeBps_) external {
        managed = managed_;
        supply = supply_;
        custody = custody_;
        phase = phase_;
        feeBps = feeBps_;
    }

    function seedHolder(address holder, uint256 shares) external {
        sharesOf[holder] += shares;
    }

    function seedProposal(bool isProfit, uint256 amount, bool funded, address funder) external {
        proposalIsProfit = isProfit;
        proposalAmount = amount;
        proposalFunded = funded;
        proposalFunder = funder;
    }

    // ------------------------------------------------------------------ share math

    function _offset() private view returns (uint256) {
        return rule == Rule.WRONG_ZERO_DECIMALS_OFFSET ? 1 : SHARE_OFFSET;
    }

    function previewDeposit(uint256 assets) public view returns (uint256) {
        return FullMulDiv.floor(assets, supply + _offset(), managed + 1);
    }

    function previewRedeem(uint256 shares) public view returns (uint256) {
        return FullMulDiv.floor(shares, managed + 1, supply + _offset());
    }

    // ------------------------------------------------------------------ transitions

    function deposit(address holder, uint256 assets) external returns (uint256 shares) {
        shares = previewDeposit(assets);
        sharesOf[holder] += shares;
        supply += shares;
        managed += assets;
        custody += assets;
    }

    function redeem(address holder, uint256 shares) external returns (uint256 assets) {
        assets = previewRedeem(shares);
        sharesOf[holder] -= shares;
        supply -= shares;
        managed -= assets;
        custody -= assets;
        paidTo[holder] += assets;
    }

    function lock() external {
        phase = Phase.LOCKED;
    }

    function propose(bool isProfit, uint256 amount, address funder) external {
        // Replacement first refunds an earlier funded profit, exactly as a cancellation.
        if (phase == Phase.SETTLE_PROPOSED) _refundProposal();
        proposalIsProfit = isProfit;
        proposalAmount = amount;
        proposalFunded = !isProfit || amount == 0;
        proposalFunder = address(0);
        if (isProfit && funder != address(0) && amount != 0) _fund(funder);
        phase = Phase.SETTLE_PROPOSED;
    }

    function fund(address funder) external {
        require(phase == Phase.SETTLE_PROPOSED && proposalIsProfit && !proposalFunded, "model: not fundable");
        _fund(funder);
    }

    /// @return toRecipient fee (PROFIT) or loss paid to the counterparty (LOSS)
    function finalize() external returns (uint256 toRecipient) {
        require(phase == Phase.SETTLE_PROPOSED && proposalFunded, "model: not finalizable");
        if (proposalIsProfit) {
            toRecipient = FullMulDiv.floor(proposalAmount, feeBps, BPS);
            managed += proposalAmount - toRecipient;
        } else {
            toRecipient = proposalAmount;
            managed -= proposalAmount;
        }
        custody -= toRecipient;
        _clearProposal();
        phase = Phase.SETTLED;
    }

    /// @return refund paid back to the funder of a funded PROFIT proposal
    function retire() external returns (uint256 refund) {
        require(!retired, "model: retired");
        if (phase == Phase.SETTLE_PROPOSED && proposalIsProfit && proposalFunded && proposalAmount != 0) {
            if (rule == Rule.WRONG_RETIRE_KEEPS_PROFIT) {
                managed += proposalAmount;
            } else if (proposalFunder != address(0)) {
                refund = proposalAmount;
                custody -= refund;
                paidTo[proposalFunder] += refund;
            }
        }
        _clearProposal();
        phase = Phase.SETTLED;
        retired = true;
    }

    function _fund(address funder) private {
        proposalFunded = true;
        proposalFunder = funder;
        custody += proposalAmount;
    }

    function _refundProposal() private {
        if (proposalIsProfit && proposalFunded && proposalAmount != 0 && proposalFunder != address(0)) {
            custody -= proposalAmount;
            paidTo[proposalFunder] += proposalAmount;
        }
        _clearProposal();
    }

    function _clearProposal() private {
        proposalIsProfit = true;
        proposalAmount = 0;
        proposalFunded = false;
        proposalFunder = address(0);
    }
}

/// @title FullMulDiv
/// @notice ⌊a · b / d⌋ without intermediate overflow, written independently of the production
///         `Math.mulDiv`: the 512-bit product (hi · 2^256 + lo) is formed from the Chinese-remainder
///         identity (lo = a·b mod 2^256, hi from a·b mod (2^256 − 1)), then divided by `d` with plain
///         restoring binary long division, one quotient bit per step. Reverts `ResultOverflow` when
///         the quotient does not fit in uint256 (hi ≥ d) and `DivisionByZero` for d = 0.
library FullMulDiv {
    error DivisionByZero();
    error ResultOverflow();

    function floor(uint256 a, uint256 b, uint256 d) internal pure returns (uint256 q) {
        if (d == 0) revert DivisionByZero();
        uint256 lo;
        uint256 hi;
        unchecked {
            lo = a * b;
            uint256 mm = mulmod(a, b, type(uint256).max);
            hi = mm - lo - (mm < lo ? 1 : 0);
        }
        if (hi == 0) return lo / d;
        if (hi >= d) revert ResultOverflow();
        // Remainder r < d throughout; it starts as the high word and absorbs one bit of `lo` per step.
        uint256 r = hi;
        unchecked {
            for (uint256 i = 256; i > 0; --i) {
                uint256 carry = r >> 255;
                r = (r << 1) | ((lo >> (i - 1)) & 1);
                // 2r + bit < 2d; if it reached 2^256 (carry) or d, subtract d once (wrapping is exact).
                if (carry == 1 || r >= d) {
                    r -= d;
                    q |= uint256(1) << (i - 1);
                }
            }
        }
    }
}

/// @notice Field-by-field round snapshot read through individual getters. It is independent of
///         `ReleaseAssertions.roundTermsHash`, which is production code under test.
library VaultTerms {
    uint256 internal constant FIELDS = 16;

    function names() internal pure returns (string[FIELDS] memory n) {
        n = [
            "roundIndex()",
            "openWindow()",
            "lockDuration()",
            "settleTimelockWindow()",
            "roundCap()",
            "perAddressCap()",
            "minSubscription()",
            "performanceFeeBps()",
            "maxLossBps()",
            "openedAt()",
            "openDeadline()",
            "counterparty()",
            "feeRecipient()",
            "totalManagedAssets()",
            "totalSupply()",
            "asset()"
        ];
    }

    function capture(address vault) internal view returns (uint256[FIELDS] memory w) {
        string[FIELDS] memory n = names();
        for (uint256 i; i < FIELDS; ++i) {
            (bool ok, bytes memory data) = vault.staticcall(abi.encodeWithSignature(n[i]));
            require(ok && data.length == 32, string.concat("VaultTerms: unreadable ", n[i]));
            w[i] = abi.decode(data, (uint256));
        }
    }
}
