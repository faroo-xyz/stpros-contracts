// SPDX-License-Identifier: GPL-3.0
pragma solidity ^0.8.28;

import {InvariantFixture, StdInvariant, Test, YieldVault} from "./InvariantFixture.sol";

contract VaultLifecycleHandler is InvariantFixture {
    uint256 public assets;
    uint256 public supply;
    mapping(address => uint256) public balances;
    uint256 public phaseModel;
    uint256 public end;
    uint256 public deadline;
    uint256 public proposedAt;
    uint256 public proposal;
    bool public loss;
    bool public binding;
    bool public terminal;
    bool public stopped;
    uint256 public fee;
    uint256 public approvedFee;
    uint256 public feePaid;
    uint256 public lossPaid;
    uint256 public round = 1;
    uint256 public refunds;

    constructor() {
        _vault();
        end = block.timestamp + 1 days;
        deadline = end + 31 days;
    }

    function effective() public view returns (uint256) {
        return phaseModel == 0 && block.timestamp >= end ? 1 : phaseModel;
    }

    function deposit(uint256 seed, uint256 raw) external {
        attempts[0]++;
        address who = _actor(seed);
        uint256 a = bound(raw, 1, 10 ether);
        bool allowed = effective() == 0 && !stopped && !terminal;
        uint256 s = a * (supply + 1e6) / (assets + 1);
        vm.prank(who);
        (bool ok, bytes memory result) = address(vault).call(abi.encodeCall(YieldVault.deposit, (a, who)));
        assertEq(ok, allowed);
        _record(0, ok);
        if (ok) {
            assertEq(abi.decode(result, (uint256)), s);
            assertLe(s * (assets + 1), a * (supply + 1e6));
            assertGt((s + 1) * (assets + 1), a * (supply + 1e6));
            assets += a;
            supply += s;
            balances[who] += s;
        }
    }

    function redeem(uint256 seed, uint256 raw) external {
        attempts[1]++;
        address who = _actor(seed);
        if (balances[who] == 0) {
            rejections[1]++;
            return;
        }
        uint256 s = bound(raw, 1, balances[who]);
        uint256 a = s * (assets + 1) / (supply + 1e6);
        bool allowed = (effective() == 0 || effective() == 3);
        vm.prank(who);
        (bool ok, bytes memory result) = address(vault).call(abi.encodeCall(YieldVault.redeem, (s, who, who)));
        assertEq(ok, allowed);
        _record(1, ok);
        if (ok) {
            assertEq(abi.decode(result, (uint256)), a);
            assets -= a;
            supply -= s;
            balances[who] -= s;
        }
    }

    function advance(uint256 raw) external {
        attempts[2]++;
        vm.warp(block.timestamp + bound(raw, 1, 10 days));
        successes[2]++;
    }

    function propose(uint256 raw, bool isLoss, bool fundNow) external {
        attempts[3]++;
        uint256 a = isLoss ? bound(raw, 0, assets / 2 + 1) : bound(raw, 0, 1 ether);
        bool allowed = !stopped && (effective() == 1 || effective() == 2)
            && (effective() == 2 || block.timestamp >= end + 1 days) && block.timestamp + 1 hours <= deadline
            && (!isLoss || a <= assets / 2);
        address sender = isLoss || (effective() == 2 && loss) ? address(tl) : OPS;
        // Timelock cannot fund from OPS. This model funds its own PROFIT via separate fund().
        address from = fundNow && sender == OPS && !isLoss ? OPS : address(0);
        vm.prank(sender);
        (bool ok,) = address(vault).call(
            abi.encodeCall(
                YieldVault.proposeSettlement,
                (isLoss ? YieldVault.SettleMode.LOSS : YieldVault.SettleMode.PROFIT, a, from)
            )
        );
        assertEq(ok, allowed);
        _record(3, ok);
        if (ok) {
            if (effective() == 2 && !loss && binding) refunds += proposal;
            phaseModel = 2;
            loss = isLoss;
            proposal = a;
            binding = isLoss || a == 0 || from != address(0);
            proposedAt = block.timestamp;
        }
    }

    function fund() external {
        attempts[4]++;
        bool allowed = effective() == 2 && !loss && !binding && !stopped;
        vm.prank(OPS);
        (bool ok,) = address(vault).call(abi.encodeCall(YieldVault.fundProfit, ()));
        assertEq(ok, allowed);
        _record(4, ok);
        if (ok) binding = true;
    }

    function cancel() external {
        attempts[5]++;
        bool allowed = effective() == 2;
        uint256 before = st.balanceOf(OPS);
        vm.prank(loss ? address(tl) : OPS);
        (bool ok,) = address(vault).call(abi.encodeCall(YieldVault.cancelProposedSettlement, ()));
        assertEq(ok, allowed);
        _record(5, ok);
        if (ok) {
            uint256 refund = !loss && binding ? proposal : 0;
            assertEq(st.balanceOf(OPS) - before, refund);
            refunds += refund;
            phaseModel = 1;
            binding = false;
            proposal = 0;
        }
    }

    function settle(bool force) external {
        attempts[6]++;
        bool allowed = force
            ? block.timestamp >= deadline && (effective() == 1 || (effective() == 2 && !binding))
            : effective() == 2 && binding && block.timestamp >= proposedAt + 1 hours && !stopped;
        (bool ok,) = address(vault).call(
            force ? abi.encodeCall(YieldVault.forceParSettlement, ()) : abi.encodeCall(YieldVault.finalize, ())
        );
        assertEq(ok, allowed);
        _record(6, ok);
        if (ok) {
            if (!force) {
                if (loss) {
                    assets -= proposal;
                    lossPaid += proposal;
                } else {
                    uint256 f = proposal * fee / 10000;
                    feePaid += f;
                    assets += proposal - f;
                }
            }
            phaseModel = 3;
            binding = false;
        }
    }

    function reopen() external {
        attempts[7]++;
        bool allowed = effective() == 3 && !terminal;
        vm.prank(OPS);
        (bool ok,) = address(vault).call(abi.encodeCall(YieldVault.openNextRound, (_round(approvedFee))));
        assertEq(ok, allowed);
        _record(7, ok);
        if (ok) {
            phaseModel = 0;
            end = block.timestamp + 1 days;
            deadline = end + 31 days;
            fee = approvedFee;
            round++;
            binding = false;
        }
    }

    function policy(uint256 raw, bool pause_) external {
        attempts[8]++;
        approvedFee = bound(raw, 0, 3000);
        vm.prank(address(tl));
        factory.setApprovedPerformanceFeeBps(uint16(approvedFee));
        if (pause_ != stopped) {
            vm.prank(pause_ ? OPS : address(tl));
            if (pause_) factory.pause();
            else factory.unpause();
            stopped = pause_;
        }
        successes[8]++;
    }

    function retire(uint256 sample) external {
        attempts[9]++;
        // Terminal actions otherwise dominate short sequences and hide settlement.
        // All phases remain eligible; deterministic witnesses use sample zero.
        if (sample % 32 != 0) { rejections[9]++; return; }
        if (terminal) {
            vm.prank(OPS);
            vm.expectRevert();
            vault.openNextRound(_round(approvedFee));
            rejections[9]++;
            return;
        }
        uint256 before = st.balanceOf(OPS);
        vm.prank(address(tl));
        factory.retireVault(address(vault));
        uint256 refund = effective() == 2 && !loss && binding ? proposal : 0;
        assertEq(st.balanceOf(OPS) - before, refund);
        refunds += refund;
        terminal = true;
        phaseModel = 3;
        binding = false;
        successes[9]++;
    }

    function unauthorizedLoss() external {
        attempts[10]++;
        vm.prank(OPS);
        (bool ok,) = address(vault).call(
            abi.encodeCall(YieldVault.proposeSettlement, (YieldVault.SettleMode.LOSS, 0, address(0)))
        );
        assertFalse(ok);
        rejections[10]++;
    }

    function check() external view {
        assertEq(vault.totalManagedAssets(), assets);
        assertEq(vault.totalSupply(), supply);
        assertEq(vault.balanceOf(ALICE), balances[ALICE]);
        assertEq(vault.balanceOf(BOB), balances[BOB]);
        uint256 escrow = effective() == 2 && !loss && binding ? proposal : 0;
        assertEq(st.balanceOf(address(vault)), assets + escrow);
        assertEq(st.balanceOf(TREASURY), feePaid);
        assertEq(st.balanceOf(COUNTERPARTY), lossPaid);
        assertEq(uint256(vault.phase()), effective());
        assertEq(vault.settlementDeadline(), deadline);
        assertEq(vault.performanceFeeBps(), fee);
        assertEq(vault.roundIndex(), round);
        assertEq(vault.retired(), terminal);
    }

    function wrongFeeChangesCurrentRound() external view {
        assertEq(vault.performanceFeeBps(), approvedFee);
    }
}

contract VaultLifecycleInvariant is StdInvariant, Test {
    VaultLifecycleHandler internal h;

    function setUp() public {
        vm.createDir("cache_forge/invariant-coverage", true);
        vm.writeFile("cache_forge/invariant-coverage/INV-B.hex", "");
        h = new VaultLifecycleHandler();
        bytes4[] memory s = new bytes4[](11);
        s[0] = h.deposit.selector;
        s[1] = h.redeem.selector;
        s[2] = h.advance.selector;
        s[3] = h.propose.selector;
        s[4] = h.fund.selector;
        s[5] = h.cancel.selector;
        s[6] = h.settle.selector;
        s[7] = h.reopen.selector;
        s[8] = h.policy.selector;
        s[9] = h.retire.selector;
        s[10] = h.unauthorizedLoss.selector;
        targetSelector(FuzzSelector(address(h), s));
        targetContract(address(h));
    }

    function afterInvariant() public {
        (uint256[16] memory a, uint256[16] memory s, uint256[16] memory r) = h.coverage();
        emit log_named_bytes("coverage-at-sequence-end", abi.encode(a, s, r));
        // Files live only in the approved isolated export, never in B0 snapshots.
        vm.writeLine("cache_forge/invariant-coverage/INV-B.hex", vm.toString(abi.encode(a, s, r)));
    }

    function invariant_lifecycleAndBacking() public view {
        h.check();
    }

    function testFuzz_settlement(uint64 amount, bool isLoss) public {
        h.deposit(0, 10 ether);
        h.advance(2 days);
        h.propose(amount, isLoss, true);
        h.advance(1 hours);
        h.settle(false);
        h.check();
    }

    function test_pausedRoundOpeningPreservesApprovedPolicy() public {
        h.advance(2 days);
        h.propose(0, false, true);
        h.advance(1 hours);
        h.settle(false);
        h.policy(1000, true);
        h.reopen();
        h.deposit(0, 1 ether);
        h.check();
        assertEq(h.round(), 2);
        assertEq(h.rejections(0), 1);
    }

    function test_retirementRefundAndPausedExit() public {
        h.deposit(0, 10 ether);
        h.advance(2 days);
        h.propose(1 ether, false, true);
        h.policy(1000, true);
        h.retire(0);
        h.redeem(0, type(uint256).max);
        h.reopen();
        h.check();
        assertEq(h.refunds(), 1 ether);
        assertGt(h.successes(1), 0);
        assertGt(h.rejections(7), 0);
    }

    function test_nonVacuityAndWrongRule() public {
        h.deposit(0, 10 ether);
        h.deposit(1, 2 ether);
        h.redeem(1, 1e6);
        h.policy(1000, false);
        vm.expectRevert();
        h.wrongFeeChangesCurrentRound();
        h.advance(2 days);
        h.propose(1 ether, false, false);
        h.fund();
        h.cancel();
        h.propose(1 ether, false, true);
        h.advance(1 hours);
        h.settle(false);
        h.reopen();
        h.advance(2 days);
        h.propose(1 ether, false, true);
        h.propose(0.5 ether, false, true);
        h.advance(1 hours);
        h.settle(false);
        h.reopen();
        h.advance(2 days);
        h.propose(1 ether, true, false);
        h.advance(1 hours);
        h.settle(false);
        h.reopen();
        h.advance(10 days);
        h.advance(10 days);
        h.advance(10 days);
        h.advance(2 days);
        h.settle(true);
        h.unauthorizedLoss();
        h.retire(0);
        h.reopen();
        h.check();
        for (uint256 i; i < 10; i++) {
            assertGt(h.successes(i), 0);
        }
        assertGt(h.rejections(10), 0);
        afterInvariant();
    }
}
