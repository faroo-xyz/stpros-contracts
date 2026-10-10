// SPDX-License-Identifier: GPL-3.0
pragma solidity ^0.8.28;

import {InvariantFixture, StdInvariant, Test, Oracle, StPROS} from "./InvariantFixture.sol";

contract CoreAccountingHandler is InvariantFixture {
    uint256 public t;
    uint256 public v;
    uint256 public issued;
    uint256 public paid;
    uint256 public funded;
    uint256 public recovered;
    uint256 public deposited;
    uint256 public lastReport;
    uint256 public partialClaims;
    uint256 public fullClaims;
    mapping(address => uint256) public shares;

    struct Ticket {
        uint256 start;
        uint256 amount;
        uint256 claimed;
        uint256 mature;
        address receiver;
    }

    mapping(address => Ticket[]) internal tickets;

    constructor() {
        _core();
        lastReport = block.timestamp;
    }

    function deposit(uint256 seed, uint256 raw, bool mintShares) external {
        attempts[0]++;
        address who = _actor(seed);
        uint256 n = bound(raw, 1e9, 10 ether);
        uint256 s = mintShares ? n : (v == 0 ? n : n * v / t);
        uint256 a = mintShares ? (v == 0 ? n : (n * t + v - 1) / v) : n;
        vm.deal(who, a);
        if (mintShares) {
            vm.startPrank(who);
            wrapped.deposit{value: a}();
            wrapped.approve(address(st), a);
            assertEq(st.mint(s, who), a);
            vm.stopPrank();
        } else {
            vm.prank(who);
            assertEq(st.depositWithPROS{value: a}(), s);
        }
        if (v != 0) {
            if (mintShares) {
                assertGe(a * v, s * t);
                assertLt((a - 1) * v, s * t);
            } else {
                assertLe(s * t, a * v);
                assertGt((s + 1) * t, a * v);
            }
        }
        if (v == 0) lastReport = block.timestamp;
        t += a;
        v += s;
        shares[who] += s;
        deposited += a;
        successes[0]++;
    }

    function request(uint256 seed, uint256 raw, bool fixedAssets, bool boundReceiver) external {
        attempts[1]++;
        address who = _actor(seed);
        if (shares[who] == 0 || tickets[who].length >= 64) {
            rejections[1]++;
            return;
        }
        uint256 pendingTickets;
        for (uint256 i; i < tickets[who].length; i++) {
            if (tickets[who][i].claimed < tickets[who][i].amount) pendingTickets++;
        }
        if (pendingTickets >= 5) {
            vm.prank(who);
            vm.expectRevert();
            st.redeem(shares[who], who, who);
            rejections[1]++;
            return;
        }
        uint256 s = bound(raw, 1, shares[who]);
        uint256 a = s * t / v;
        if (a == 0) {
            rejections[1]++;
            return;
        }
        if (fixedAssets) s = (a * v + t - 1) / t;
        address receiver = boundReceiver ? RECEIVER : who;
        vm.prank(who);
        if (fixedAssets) assertEq(st.withdraw(a, receiver, who), s);
        else assertEq(st.redeem(s, receiver, who), a);
        tickets[who].push(Ticket(issued, a, 0, block.timestamp + 1 days, receiver));
        issued += a;
        shares[who] -= s;
        v -= s;
        t -= a;
        if (v == 0) t = 0;
        successes[1]++;
    }

    function fund(uint256 raw) external {
        attempts[2]++;
        uint256 a = bound(raw, 1, 10 ether);
        vm.deal(address(this), a);
        (bool ok,) = address(bridge).call{value: a}("");
        assertTrue(ok);
        funded += a;
        successes[2]++;
    }

    function recover(uint256 raw) external {
        attempts[3]++;
        uint256 reserve = funded - recovered - paid;
        if (reserve == 0) {
            rejections[3]++;
            return;
        }
        uint256 a = bound(raw, 1, reserve);
        vm.prank(address(tl));
        bridge.emergencyWithdraw(address(0), RECEIVER, a);
        recovered += a;
        successes[3]++;
    }

    function advance(uint256 raw) external {
        attempts[4]++;
        vm.warp(block.timestamp + bound(raw, 1, 10 days));
        successes[4]++;
    }

    function report(uint256 raw, bool invalid) external {
        attempts[5]++;
        uint256 elapsed = block.timestamp - lastReport;
        uint256 cap = t * 500_000 * (elapsed > 7 days ? 7 days : elapsed) / (365 days * 1_000_000);
        if (cap > 100 ether) cap = 100 ether;
        uint256 a = cap == 0 ? 1 : bound(raw, 1, cap);
        if (invalid) a = cap + 1;
        bool allowed = v != 0 && elapsed >= 1 hours && a <= cap && a <= 100 ether;
        vm.prank(REPORTER);
        (bool ok,) = address(oracle).call(abi.encodeCall(Oracle.update, (address(wrapped), a)));
        assertEq(ok, allowed);
        _record(5, ok);
        if (ok) {
            uint256 fee = a / 10;
            uint256 c = fee * v / (t + a - fee);
            assertLe(c * (t + a - fee), fee * v);
            assertGt((c + 1) * (t + a - fee), fee * v);
            t += a;
            v += c;
            shares[TREASURY] += c;
            lastReport = block.timestamp;
        }
    }

    function claim(uint256 seed, bool asWrapped) external {
        attempts[6]++;
        address who = _actor(seed);
        uint256 frontier = funded - recovered;
        uint256 expected;
        uint256 userPay;
        uint256 otherPay;
        Ticket[] storage list = tickets[who];
        for (uint256 i; i < list.length; i++) {
            Ticket storage q = list[i];
            if (q.claimed == q.amount) continue;
            if (block.timestamp < q.mature || frontier <= q.start + q.claimed) break;
            uint256 x = frontier - q.start - q.claimed;
            if (x > q.amount - q.claimed) x = q.amount - q.claimed;
            q.claimed += x;
            expected += x;
            if (q.receiver == who) userPay += x;
            else otherPay += x;
            if (q.claimed == q.amount) {
                fullClaims++;
            } else {
                partialClaims++;
                break;
            }
        }
        uint256 beforeUser = asWrapped ? wrapped.balanceOf(who) : who.balance;
        uint256 beforeOther = asWrapped ? wrapped.balanceOf(RECEIVER) : RECEIVER.balance;
        vm.prank(who);
        uint256 actual = asWrapped ? st.withdrawComplete(who, 0) : st.withdrawCompleteToPROS(0);
        assertEq(actual, expected);
        assertEq((asWrapped ? wrapped.balanceOf(who) : who.balance) - beforeUser, userPay);
        assertEq((asWrapped ? wrapped.balanceOf(RECEIVER) : RECEIVER.balance) - beforeOther, otherPay);
        paid += expected;
        _record(6, expected != 0);
    }

    function unauthorized() external {
        attempts[7]++;
        vm.prank(ALICE);
        (bool ok,) = address(oracle).call(abi.encodeCall(Oracle.setTokenAmount, (address(wrapped), t, t + 1)));
        assertFalse(ok);
        vm.prank(ALICE);
        (ok,) = address(bridge).call(
            abi.encodeWithSignature("withdrawToken(address,address,uint256)", address(wrapped), ALICE, 1)
        );
        assertFalse(ok);
        rejections[7]++;
    }

    function check() external view {
        (uint256 actualT, uint256 actualV) = oracle.poolInfo(address(wrapped));
        assertEq(actualT, t);
        assertEq(actualV, v);
        assertEq(st.totalSupply(), v);
        assertEq(st.balanceOf(ALICE), shares[ALICE]);
        assertEq(st.balanceOf(BOB), shares[BOB]);
        assertEq(st.balanceOf(TREASURY), shares[TREASURY]);
        assertEq(st.queuedWithdrawal(), issued);
        assertEq(st.completedWithdrawal(), paid);
        assertEq(address(bridge).balance, funded - recovered - paid);
        assertEq(address(sink).balance, deposited);
        uint256 outstanding;
        for (uint256 a; a < 2; a++) {
            Ticket[] storage list = tickets[_actor(a)];
            for (uint256 i; i < list.length; i++) {
                outstanding += list[i].amount - list[i].claimed;
            }
        }
        assertEq(outstanding, issued - paid);
    }

    function wrongNoCommissionDilution() external view {
        assertEq(st.totalSupply(), shares[ALICE] + shares[BOB]);
    }
}

contract CoreAccountingInvariant is StdInvariant, Test {
    CoreAccountingHandler internal h;

    function setUp() public {
        vm.createDir("cache_forge/invariant-coverage", true);
        vm.writeFile("cache_forge/invariant-coverage/INV-A.hex", "");
        h = new CoreAccountingHandler();
        bytes4[] memory s = new bytes4[](8);
        s[0] = h.deposit.selector;
        s[1] = h.request.selector;
        s[2] = h.fund.selector;
        s[3] = h.recover.selector;
        s[4] = h.advance.selector;
        s[5] = h.report.selector;
        s[6] = h.claim.selector;
        s[7] = h.unauthorized.selector;
        targetSelector(FuzzSelector(address(h), s));
        targetContract(address(h));
    }

    function afterInvariant() public {
        (uint256[16] memory a, uint256[16] memory s, uint256[16] memory r) = h.coverage();
        emit log_named_bytes("coverage-at-sequence-end", abi.encode(a, s, r));
        // Files live only in the approved isolated export, never in B0 snapshots.
        vm.writeLine("cache_forge/invariant-coverage/INV-A.hex", vm.toString(abi.encode(a, s, r)));
    }

    function invariant_accountingAndFifo() public view {
        h.check();
    }

    function test_nonVacuityAndWrongRule() public {
        h.deposit(0, 10 ether, false);
        h.deposit(1, 10 ether, true);
        h.advance(2 days);
        h.report(0.01 ether, false);
        vm.expectRevert();
        h.wrongNoCommissionDilution();
        h.request(0, 5 ether, false, true);
        h.request(1, 2 ether, true, false);
        h.advance(1 days);
        h.fund(1 ether);
        h.claim(0, false);
        h.fund(10 ether);
        h.claim(0, true);
        h.claim(1, false);
        h.recover(1);
        h.unauthorized();
        h.check();
        assertGt(h.partialClaims(), 0);
        assertGt(h.fullClaims(), 0);
        for (uint256 i; i < 7; i++) {
            assertGt(h.successes(i), 0);
        }
        assertGt(h.rejections(7), 0);
        afterInvariant();
    }

    function testFuzz_roundTrip(uint96 amount, bool minted) public {
        h.deposit(0, amount, minted);
        h.request(0, type(uint256).max, false, true);
        h.advance(1 days);
        h.fund(10 ether);
        h.claim(0, minted);
        h.check();
        assertEq(h.issued(), h.paid());
        assertEq(h.v(), 0);
    }

    function test_zeroSupplyRestart() public {
        h.deposit(0, 1 ether, false);
        h.request(0, type(uint256).max, false, false);
        h.check();
        assertEq(h.t(), 0);
        assertEq(h.v(), 0);
        h.deposit(1, 1234567890, false);
        h.check();
    }
}
