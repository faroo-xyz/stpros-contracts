// SPDX-License-Identifier: GPL-3.0
pragma solidity ^0.8.28;
import {LabFixture, LabForceNative} from "./LabFixture.sol";
import {Oracle} from "../../contracts/Oracle.sol";
import {SlpDistributor} from "../../contracts/SlpDistributor.sol";

contract DispatchDestination {
    bool public reject;
    uint256 public received;
    constructor(bool reject_) {reject=reject_;}
    receive() external payable {require(!reject,"destination unavailable");received+=msg.value;}
}
contract EconomicInteractionsTest is LabFixture {
    function setUp() public {_core();_canonicalRoles();}
    function test_labJitNominalGainBoundAndSevenDayWait() public {
        vm.prank(address(tl));st.setUnbondingPeriod(7 days);
        _deposit(ALICE,1000 ether);vm.warp(block.timestamp+7 days);_deposit(BOB,1000 ether);
        uint256 principal=1000 ether;uint256 delta=10 ether;uint256 fee=1 ether;
        uint256 commission=fee*2000 ether/(2010 ether-fee);
        vm.prank(REPORTER);oracle.update(address(wrapped),delta);
        assertEq(st.balanceOf(TREASURY),commission);
        uint256 entitlement=principal*2010 ether/(2000 ether+commission);
        assertLe(entitlement-principal,delta);assertGt(entitlement,principal);
        vm.prank(BOB);assertEq(st.redeem(principal,BOB,BOB),entitlement);
        _fundBridge(entitlement);
        vm.prank(BOB);assertEq(st.withdrawCompleteToPROS(),0);
        vm.warp(block.timestamp+7 days);uint256 before=BOB.balance;
        vm.prank(BOB);assertEq(st.withdrawCompleteToPROS(),entitlement);
        assertEq(BOB.balance-before,entitlement);
        emit log_named_uint("nominal profit before financing/gas",entitlement-principal);
        // Negative control: waiting is not a proof of nonpositive nominal profit.
        assertFalse(entitlement<=principal);
    }
    function test_labRepeatedReportsUseCompoundingAndDoNotReuseTime() public {
        _deposit(ALICE,1000 ether);uint256 expectedT=1000 ether;uint256 expectedV=1000 ether;uint256 fees;
        for(uint256 i;i<24;i++){
            vm.warp(block.timestamp+1 hours);
            uint256 cap=expectedT*500_000*1 hours/(365 days*1_000_000);
            vm.prank(REPORTER);vm.expectRevert();oracle.update(address(wrapped),cap+1);
            uint256 c=(cap/10)*expectedV/(expectedT+cap-cap/10);
            vm.prank(REPORTER);oracle.update(address(wrapped),cap);
            expectedT+=cap;expectedV+=c;fees+=c;
            vm.prank(REPORTER);vm.expectRevert();oracle.update(address(wrapped),1);
        }
        (uint256 t,uint256 v)=oracle.poolInfo(address(wrapped));assertEq(t,expectedT);assertEq(v,expectedV);
        assertEq(st.totalSupply(),v);assertEq(st.balanceOf(TREASURY),fees);
        uint256 simple=uint256(1000 ether)*500_000*1 days/(365 days*1_000_000);
        assertGt(t-1000 ether,simple);emit log_named_uint("24 hourly gross yield",t-1000 ether);
        vm.warp(block.timestamp+30 days);
        uint256 catchup=t*500_000*7 days/(365 days*1_000_000);
        vm.prank(REPORTER);vm.expectRevert();oracle.update(address(wrapped),catchup+1);
        vm.prank(REPORTER);oracle.update(address(wrapped),catchup);
    }
    function test_labDownwardCorrectionPreservesOldTicketsAndChangesFutureExits() public {
        _deposit(ALICE,100 ether);_deposit(BOB,100 ether);
        vm.prank(ALICE);assertEq(st.redeem(50 ether,ALICE,ALICE),50 ether);
        vm.prank(BOB);vm.expectRevert();oracle.setTokenAmount(address(wrapped),150 ether,75 ether);
        vm.prank(address(tl));oracle.setTokenAmount(address(wrapped),150 ether,75 ether);
        vm.prank(BOB);assertEq(st.redeem(100 ether,BOB,BOB),50 ether);
        assertEq(st.getWithdrawals(ALICE)[0].pending,50 ether);
        assertEq(st.queuedWithdrawal(),100 ether);assertEq(st.totalSupply(),50 ether);
        (uint256 t,uint256 v)=oracle.poolInfo(address(wrapped));assertEq(t,25 ether);assertEq(v,50 ether);
        // Correcting before Alice's request would have fixed only 25 assets, not 50.
        assertEq(uint256(50 ether)*100 ether/200 ether,25 ether);
        vm.warp(block.timestamp+1 days);_fundBridge(100 ether);
        vm.prank(BOB);assertEq(st.withdrawCompleteToPROS(),50 ether);
        vm.prank(ALICE);assertEq(st.withdrawCompleteToPROS(),50 ether);
        assertEq(st.completedWithdrawal(),100 ether);assertEq(address(bridge).balance,0);
    }
    function test_labStaleCasAndPausedCorrection() public {
        _deposit(ALICE,100 ether);_deposit(BOB,1 ether);
        vm.prank(address(tl));vm.expectRevert(Oracle.StaleTokenAmount.selector);oracle.setTokenAmount(address(wrapped),100 ether,50 ether);
        vm.prank(OPS);oracle.pause();vm.prank(OPS);st.pause();
        vm.deal(BOB,1 ether);vm.prank(BOB);vm.expectRevert();st.depositWithPROS{value:1 ether}();
        vm.prank(address(tl));oracle.setTokenAmount(address(wrapped),101 ether,50 ether);
        (uint256 t,uint256 v)=oracle.poolInfo(address(wrapped));assertEq(t,50 ether);assertEq(v,101 ether);
        assertEq(st.totalSupply(),101 ether);assertEq(st.previewRedeem(101 ether),50 ether);
    }
    function test_labRoundingCyclesAndEmptyPoolCannotReport() public {
        vm.warp(block.timestamp+7 days);vm.prank(REPORTER);vm.expectRevert();oracle.update(address(wrapped),1);
        _deposit(ALICE,100 ether);vm.prank(address(tl));oracle.setTokenAmount(address(wrapped),100 ether,150 ether);
        uint256 t=150 ether;uint256 v=100 ether;uint256 loss;
        for(uint256 i;i<32;i++){
            uint256 a=1e9+i;uint256 s=a*v/t;uint256 afterAssets=t+a;uint256 afterShares=v+s;
            uint256 returned=s*afterAssets/afterShares;
            assertLe(returned,a);assertLe(s*t,a*v);assertGt((s+1)*t,a*v);
            vm.deal(BOB,a);vm.prank(BOB);assertEq(st.depositWithPROS{value:a}(),s);
            vm.prank(BOB);assertEq(st.redeem(s,BOB,BOB),returned);
            _fundBridge(returned);vm.warp(block.timestamp+1 days);
            vm.prank(BOB);assertEq(st.withdrawCompleteToPROS(),returned);
            t=afterAssets-returned;loss+=a-returned;
        }
        (uint256 actualT,uint256 actualV)=oracle.poolInfo(address(wrapped));assertEq(actualT,150 ether+loss);assertEq(actualV,v);
        vm.prank(ALICE);st.redeem(v,ALICE,ALICE);
        (actualT,actualV)=oracle.poolInfo(address(wrapped));assertEq(actualT,0);assertEq(actualV,0);
        _deposit(BOB,1 ether);assertEq(st.balanceOf(BOB),1 ether);
        vm.prank(REPORTER);vm.expectRevert();oracle.update(address(wrapped),1);
    }
    function test_labDispatchRollbackDuplicateDestinationAndExternalLiquidityGap() public {
        _deposit(ALICE,30 ether);DispatchDestination good=new DispatchDestination(false);DispatchDestination bad=new DispatchDestination(true);
        address[] memory list=new address[](2);list[0]=address(good);list[1]=address(bad);
        vm.prank(address(tl));sink.setSlps(list,true);
        SlpDistributor.Distribution[] memory ds=new SlpDistributor.Distribution[](2);
        ds[0]=SlpDistributor.Distribution(address(good),10 ether);ds[1]=SlpDistributor.Distribution(address(bad),10 ether);
        vm.prank(REPORTER);vm.expectRevert();sink.distribute(ds);
        assertEq(good.received(),0);assertEq(address(sink).balance,30 ether);
        ds[1]=SlpDistributor.Distribution(address(good),10 ether);
        vm.prank(ALICE);vm.expectRevert();sink.distribute(ds);
        vm.prank(REPORTER);sink.distribute(ds);assertEq(good.received(),20 ether);assertEq(address(sink).balance,10 ether);
        vm.prank(ALICE);st.redeem(10 ether,ALICE,ALICE);vm.warp(block.timestamp+1 days);
        vm.prank(ALICE);assertEq(st.withdrawCompleteToPROS(),0);
        _fundBridge(10 ether);vm.prank(ALICE);assertEq(st.withdrawCompleteToPROS(),10 ether);
        // Local destination balance is not evidence of recoverable external SLP assets.
        assertEq(good.received(),20 ether);
    }
    function test_labUnsolicitedBalancesDoNotCreditNav() public {
        _deposit(ALICE,100 ether);vm.deal(address(this),4 ether);
        new LabForceNative{value:1 ether}(payable(address(st)));new LabForceNative{value:1 ether}(payable(address(oracle)));
        wrapped.deposit{value:2 ether}();wrapped.transfer(address(st),1 ether);wrapped.transfer(address(oracle),1 ether);
        (uint256 t,uint256 v)=oracle.poolInfo(address(wrapped));assertEq(t,100 ether);assertEq(v,100 ether);
        assertEq(st.totalSupply(),100 ether);assertEq(st.previewRedeem(100 ether),100 ether);
        assertEq(address(st).balance,1 ether);assertEq(wrapped.balanceOf(address(st)),1 ether);
    }
    /// AL-01: a compromised reporter gets one 7-day catch-up step, then hourly steps.
    function test_labCompromisedReporterInflationIsCappedPerReportAndPerWeek() public {
        _deposit(ALICE,1000 ether);_deposit(BOB,1000 ether);vm.warp(block.timestamp+30 days);
        uint256 t=2000 ether;uint256 v=2000 ether;
        uint256 cap=t*500_000*7 days/(365 days*1_000_000);
        vm.prank(REPORTER);vm.expectRevert();oracle.update(address(wrapped),cap+1);
        vm.prank(REPORTER);oracle.update(address(wrapped),cap);
        uint256 c=(cap/10)*v/(t+cap-cap/10);t+=cap;v+=c;
        uint256 expected=uint256(1000 ether)*t/v;uint256 gain=expected-1000 ether;
        assertLe(gain,cap/2);assertGe(gain,cap*9/20-1);                     // half the pool, net of 10% commission
        vm.prank(BOB);assertEq(st.redeem(1000 ether,BOB,BOB),expected);
        t-=expected;v-=1000 ether;uint256 t0=t;
        for(uint256 i;i<168;i++){
            vm.warp(block.timestamp+1 hours);uint256 step=t*500_000*1 hours/(365 days*1_000_000);
            vm.prank(REPORTER);vm.expectRevert();oracle.update(address(wrapped),step+1);
            vm.prank(REPORTER);oracle.update(address(wrapped),step);
            c=(step/10)*v/(t+step-step/10);t+=step;v+=c;
        }
        (uint256 actualT,uint256 actualV)=oracle.poolInfo(address(wrapped));assertEq(actualT,t);assertEq(actualV,v);
        uint256 growthPpb=(t-t0)*1e9/t0;uint256 simplePpb=uint256(500_000)*7 days*1e3/365 days;
        assertGe(growthPpb,simplePpb);assertLe(growthPpb,simplePpb+simplePpb*simplePpb/1e9);
        emit log_named_uint("first 7-day catch-up gross",cap);emit log_named_uint("attacker nominal gain",gain);
        emit log_named_uint("worst 168 hourly reports growth ppb",growthPpb);
    }
    /// IS-20: OZ preview directions on the WPROS entrypoints with the real WPROS9 unwrap path.
    function test_labWprosMintWithdrawCyclesAreProtocolFavourable() public {
        _deposit(ALICE,100 ether);vm.prank(address(tl));oracle.setTokenAmount(address(wrapped),100 ether,137 ether+3);
        uint256 t=137 ether+3;uint256 v=100 ether;uint256 paid;uint256 queued;
        vm.deal(BOB,10 ether);vm.startPrank(BOB);wrapped.deposit{value:10 ether}();wrapped.approve(address(st),type(uint256).max);vm.stopPrank();
        for(uint256 i;i<16;i++){
            uint256 s=1e9+i*7919;uint256 a=(s*t+v-1)/v;                            // mint costs ceil
            vm.prank(BOB);assertEq(st.mint(s,BOB),a);t+=a;v+=s;paid+=a;
            uint256 w=s*t/v;uint256 burned=(w*v+t-1)/t;assertLe(burned,s);assertLe(w,a);
            vm.prank(BOB);assertEq(st.withdraw(w,BOB,BOB),burned);t-=w;v-=burned;queued+=w;
            uint256 d=3e9+i;uint256 minted=d*v/t;vm.prank(BOB);assertEq(st.deposit(d,BOB),minted);t+=d;v+=minted;paid+=d;
            uint256 back=minted*t/v;vm.prank(BOB);assertEq(st.redeem(minted,BOB,BOB),back);assertLe(back,d);t-=back;v-=minted;queued+=back;
            _fundBridge(w+back);vm.warp(block.timestamp+1 days);vm.prank(BOB);assertEq(st.withdrawComplete(),w+back);
        }
        (uint256 actualT,uint256 actualV)=oracle.poolInfo(address(wrapped));assertEq(actualT,t);assertEq(actualV,v);
        assertEq(st.totalSupply(),v);uint256 dust=st.balanceOf(BOB);
        assertGe(paid,queued+st.previewRedeem(dust));assertEq(wrapped.balanceOf(BOB),10 ether-paid+queued);
        assertEq(address(st).balance,0);assertEq(wrapped.balanceOf(address(st)),0);assertEq(address(sink).balance,100 ether+paid);
        emit log_named_uint("protocol-favourable rounding retained (wei)",paid-queued-st.previewRedeem(dust));
    }
    /// IS-25: CAS is an unbounded governance NAV authority; old tickets stay fixed.
    function test_labUpwardCasIsGovernanceTrustNotRateBounded() public {
        _deposit(ALICE,100 ether);_deposit(BOB,100 ether);vm.prank(ALICE);assertEq(st.redeem(50 ether,ALICE,ALICE),50 ether);
        vm.prank(address(tl));oracle.setTokenAmount(address(wrapped),150 ether,300 ether);      // 2x in one call
        vm.prank(BOB);assertEq(st.redeem(100 ether,BOB,BOB),200 ether);
        assertEq(st.getWithdrawals(ALICE)[0].pending,50 ether);assertEq(st.queuedWithdrawal(),250 ether);
        assertEq(address(sink).balance,200 ether);                                                 // liabilities exceed principal by 50
        vm.warp(block.timestamp+1 hours);(uint256 t,)=oracle.poolInfo(address(wrapped));
        uint256 step=t*500_000*1 hours/(365 days*1_000_000);
        vm.prank(REPORTER);vm.expectRevert();oracle.update(address(wrapped),step+1);
        vm.prank(REPORTER);oracle.update(address(wrapped),step);
    }
}
