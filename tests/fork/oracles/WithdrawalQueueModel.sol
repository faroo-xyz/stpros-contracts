// SPDX-License-Identifier: GPL-3.0
pragma solidity ^0.8.28;

/// @title WithdrawalQueueModel
/// @notice Independent ghost of the StPROS FIFO withdrawal queue (I-4, FS-5, F-07 semantics).
///         Written from the specified behaviour, not from the production loop:
///
///         - A request appends a record whose baseline is the global queued total Q at request
///           time; Q grows by the request.
///         - A claim takes one liquidity snapshot `cum = available + C` at its start. `available`
///           is the BridgeVault custody after R4, or the V1 reserve before R4.
///         - Records are visited in FIFO order, at most `maxRecords` of them (0 = no limit). A
///           record is payable once matured and `cum > baseline`; the payout is
///           `min(cum − baseline, pending)`.
///         - A partial payout lowers pending, **advances the record baseline by the payout**
///           (F-07) and ends the claim. A full payout removes the record.
///         - C grows by every payout; Q never changes on a claim. Hence Q − C == Σ pending.
///         - A record bound to a receiver pays that receiver; an unbound record pays the
///           claim's receiver.
///
/// @dev `Rule.WRONG_NO_PARTIAL_BUMP` reproduces the pre-F-07 replay error (the baseline is not
///      advanced on a partial payout). It exists only for negative controls.
contract WithdrawalQueueModel {
    enum Rule {
        CORRECT,
        WRONG_NO_PARTIAL_BUMP
    }

    struct Record {
        uint256 baseline;
        uint256 pending;
        uint256 createdAt;
        uint256 unbonding;
        address boundReceiver;
    }

    struct Payout {
        address receiver;
        uint256 amount;
        bool partialRecord;
    }

    Rule public immutable rule;
    uint256 public queued;
    uint256 public completed;
    bool public sealedSeed;
    bool public lastClaimReverts;

    address[] internal ownerList;
    mapping(address => bool) internal knownOwner;
    mapping(address => Record[]) internal queue;
    mapping(address => uint256) internal frontier;
    Payout[] internal lastPayouts;

    constructor(Rule rule_) {
        rule = rule_;
    }

    // ------------------------------------------------------------------ seeding (inventory)

    /// @notice Seed global totals from an authenticated inventory (before any replayed action).
    function seedTotals(uint256 queued_, uint256 completed_) external {
        require(!sealedSeed, "model: seed sealed");
        require(queued_ >= completed_, "model: Q < C");
        queued = queued_;
        completed = completed_;
    }

    /// @notice Seed one outstanding record, in the owner's queue order.
    function seedRecord(address owner, Record calldata r) external {
        require(!sealedSeed, "model: seed sealed");
        require(r.pending != 0, "model: empty record");
        _remember(owner);
        queue[owner].push(r);
    }

    /// @notice Close seeding; the seed must already satisfy Q − C == Σ pending.
    function sealSeed() external {
        require(queued - completed == totalPending(), "model: seed violates Q - C == sum pending");
        sealedSeed = true;
    }

    // ------------------------------------------------------------------ transitions

    function request(address owner, uint256 assets, address boundReceiver, uint256 timestamp, uint256 unbonding)
        external
    {
        sealedSeed = true;
        require(assets != 0, "model: zero request");
        _remember(owner);
        queue[owner].push(Record(queued, assets, timestamp, unbonding, boundReceiver));
        queued += assets;
    }

    /// @return total paid by this claim; per-record payouts are in `payouts()`. When the claim is
    ///         predicted to revert, `lastClaimReverts()` is true, `total` is 0 and no state changes.
    function claim(address owner, address claimReceiver, uint256 maxRecords, uint256 timestamp, uint256 available)
        external
        returns (uint256 total)
    {
        sealedSeed = true;
        delete lastPayouts;
        // Plan first, without writing, so that a reverting claim leaves the ghost untouched.
        (uint256 visited, bool reverts) = _plan(owner, maxRecords, timestamp, available);
        lastClaimReverts = reverts;
        if (reverts) return 0;
        uint256 cum = available + completed;
        for (uint256 n; n < visited; ++n) {
            (uint256 pay, bool partialRecord) = _payFront(owner, claimReceiver, cum);
            total += pay;
            if (partialRecord) break;
        }
    }

    /// @dev Pays the owner's front record against the claim's liquidity snapshot `cum`.
    function _payFront(address owner, address claimReceiver, uint256 cum)
        internal
        returns (uint256 pay, bool partialRecord)
    {
        Record storage r = queue[owner][frontier[owner]];
        pay = cum - r.baseline < r.pending ? cum - r.baseline : r.pending;
        address to = r.boundReceiver == address(0) ? claimReceiver : r.boundReceiver;
        completed += pay;
        partialRecord = pay < r.pending;
        lastPayouts.push(Payout(to, pay, partialRecord));
        if (partialRecord) {
            r.pending -= pay;
            if (rule == Rule.CORRECT) r.baseline += pay;
        } else {
            delete queue[owner][frontier[owner]];
            ++frontier[owner];
        }
    }

    function _plan(address owner, uint256 maxRecords, uint256 timestamp, uint256 available)
        internal
        view
        returns (uint256 visited, bool reverts)
    {
        uint256 cum = available + completed;
        uint256 remaining = available;
        Record[] storage q = queue[owner];
        for (uint256 i = frontier[owner]; i < q.length && (maxRecords == 0 || visited < maxRecords); ++i) {
            Record storage r = q[i];
            if (timestamp < r.createdAt + r.unbonding || cum <= r.baseline) break;
            ++visited;
            uint256 pay = cum - r.baseline < r.pending ? cum - r.baseline : r.pending;
            if (pay > remaining) return (visited, true);
            remaining -= pay;
            if (pay < r.pending) break;
        }
    }

    // ------------------------------------------------------------------ views

    /// @notice Predicted claimable amount without state change (dApp preview semantics).
    function preview(address owner, uint256 maxRecords, uint256 timestamp, uint256 available)
        external
        view
        returns (uint256 total, uint256 fullRecords, uint256 partialAmount)
    {
        uint256 cum = available + completed;
        Record[] storage q = queue[owner];
        for (uint256 i = frontier[owner]; i < q.length && (maxRecords == 0 || i - frontier[owner] < maxRecords); ++i) {
            Record storage r = q[i];
            if (timestamp < r.createdAt + r.unbonding || cum <= r.baseline) break;
            if (cum - r.baseline < r.pending) {
                partialAmount = cum - r.baseline;
                total += partialAmount;
                break;
            }
            total += r.pending;
            ++fullRecords;
        }
    }

    function payouts() external view returns (Payout[] memory) {
        return lastPayouts;
    }

    function owners() external view returns (address[] memory) {
        return ownerList;
    }

    function outstanding(address owner) public view returns (Record[] memory out) {
        Record[] storage q = queue[owner];
        out = new Record[](q.length - frontier[owner]);
        for (uint256 i; i < out.length; ++i) {
            out[i] = q[frontier[owner] + i];
        }
    }

    function pendingOf(address owner) public view returns (uint256 sum) {
        Record[] storage q = queue[owner];
        for (uint256 i = frontier[owner]; i < q.length; ++i) {
            sum += q[i].pending;
        }
    }

    function totalPending() public view returns (uint256 sum) {
        for (uint256 i; i < ownerList.length; ++i) {
            sum += pendingOf(ownerList[i]);
        }
    }

    function outstandingOwners() external view returns (uint256 n) {
        for (uint256 i; i < ownerList.length; ++i) {
            if (frontier[ownerList[i]] < queue[ownerList[i]].length) ++n;
        }
    }

    function _remember(address owner) private {
        if (!knownOwner[owner]) {
            knownOwner[owner] = true;
            ownerList.push(owner);
        }
    }
}
