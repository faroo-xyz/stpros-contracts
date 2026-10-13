// SPDX-License-Identifier: GPL-3.0
pragma solidity ^0.8.28;

import {Vm} from "forge-std/Vm.sol";
import {Test} from "forge-std/Test.sol";
import {stdStorage, StdStorage} from "forge-std/StdStorage.sol";
import {SyntheticVaultMigration} from "./synthetic/SyntheticVaultMigration.sol";
import {LegacyYieldVaultV1Types} from "../helpers/LegacyInterfaces.sol";
import {SyntheticV1Migration} from "./synthetic/SyntheticV1Migration.sol";
import {CampaignLedger, EvidenceClass, Verdict} from "./harness/CampaignLedger.sol";
import {VaultModel, VaultTerms} from "./oracles/VaultModel.sol";
import {HolderLedger} from "./oracles/HolderLedger.sol";
import {AuthorityMatrix} from "./oracles/AuthorityMatrix.sol";
import {YieldVault} from "../../contracts/YieldVault.sol";

/// @title Vault, holder and authority oracle validation (SYNTHETIC, class C)
/// @notice Holder shares are rebuilt from `Transfer` logs, roles from `RoleGranted`/`RoleRevoked`,
///         owners from `OwnershipTransferred`. Vault expectations come from the independent model
///         seeded with pre-migration observations; post-migration and post-retirement values are
///         predicted, then compared with the contracts under test.
contract VaultOracleTest is SyntheticVaultMigration {
    Vm.Log[] internal recordedLogs;

    function _seedModel(VaultModel.Rule rule, HolderLedger holders) internal returns (VaultModel m) {
        m = new VaultModel(rule);
        m.seedVault(
            vault.totalManagedAssets(),
            holders.totalSupply(),
            core.stPROS.balanceOf(address(vault)),
            VaultModel.Phase(LegacyYieldVaultV1Types.Phase.unwrap(vault.phase())),
            vault.performanceFeeBps()
        );
        m.seedProposal(
            LegacyYieldVaultV1Types.SettleMode.unwrap(vault.settleMode()) == 0,
            vault.settleAmount(),
            vault.profitFunded(),
            vault.profitFunder()
        );
        address[3] memory hs = _holders();
        for (uint256 i; i < hs.length; ++i) {
            m.seedHolder(hs[i], holders.balanceOf(hs[i]));
        }
    }

    function _holderLedger(HolderLedger.Rule rule) internal returns (HolderLedger holders) {
        Vm.Log[] memory recorded = vm.getRecordedLogs();
        for (uint256 i; i < recorded.length; ++i) {
            recordedLogs.push(recorded[i]);
        }
        holders = new HolderLedger(address(vault), rule);
        holders.replay(recordedLogs);
    }

    function _checkHolders(CampaignLedger l, string memory id, HolderLedger holders) internal {
        address[] memory accounts = holders.accounts();
        for (uint256 i; i < accounts.length; ++i) {
            l.eq(string.concat(id, ".shares.", vm.toString(accounts[i])), holders.balanceOf(accounts[i]), vault.balanceOf(accounts[i]));
        }
        l.eq(string.concat(id, ".sumShares==supply"), holders.totalSupply(), vault.totalSupply());
    }

    /// @notice Migration preserves every round term and accounting word; retirement refunds the
    ///         funded profit exactly; every holder redeems exactly the modelled amount.
    function _runRetirement(VaultModel.Rule rule, CampaignLedger l) internal {
        _buildVaultState(true);
        HolderLedger holders = _holderLedger(HolderLedger.Rule.CORRECT);
        _checkHolders(l, "pre", holders);
        assertEq(holders.holderCount(), 3);
        VaultModel m = _seedModel(rule, holders);
        l.eq("pre.custody==TMA+fundedProfit", vault.totalManagedAssets() + 3 ether + 1, core.stPROS.balanceOf(address(vault)));
        uint256[16] memory before = VaultTerms.capture(address(vault));

        _upgradeVaultSide();
        uint256[16] memory afterTerms = VaultTerms.capture(address(vault));
        string[16] memory names = VaultTerms.names();
        for (uint256 i; i < VaultTerms.FIELDS; ++i) {
            l.eq(string.concat("migration.preserved.", names[i]), before[i], afterTerms[i]);
        }
        _checkHolders(l, "migration", holders);

        uint256 funderBefore = core.stPROS.balanceOf(funder);
        vm.prank(address(tl));
        factory.retireVault(address(vault));
        uint256 refund = m.retire();
        YieldVault v = YieldVault(address(vault));
        l.eq("retire.refundToFunder", refund, core.stPROS.balanceOf(funder) - funderBefore);
        l.eq("retire.TMA", m.managed(), v.totalManagedAssets());
        l.eq("retire.supply", m.supply(), v.totalSupply());
        l.eq("retire.custody", m.custody(), core.stPROS.balanceOf(address(v)));
        l.eq("retire.phaseSETTLED", 3, uint256(v.phase()));
        l.truth("retire.retired", v.retired(), "vault not retired");

        address[3] memory hs = _holders();
        for (uint256 i; i < hs.length; ++i) {
            uint256 shares = v.balanceOf(hs[i]);
            uint256 predicted = m.redeem(hs[i], shares);
            uint256 balanceBefore = core.stPROS.balanceOf(hs[i]);
            vm.prank(hs[i]);
            uint256 paid = v.redeem(shares, hs[i], hs[i]);
            l.eq(string.concat("redeem.", vm.toString(hs[i]), ".paid"), predicted, paid);
            l.eq(string.concat("redeem.", vm.toString(hs[i]), ".received"), predicted, core.stPROS.balanceOf(hs[i]) - balanceBefore);
        }
        l.eq("final.TMA", m.managed(), v.totalManagedAssets());
        l.eq("final.custody", m.custody(), core.stPROS.balanceOf(address(v)));
        l.truth("final.Y-1.custody>=TMA", core.stPROS.balanceOf(address(v)) >= v.totalManagedAssets(), "Y-1");
        holders.replay(vm.getRecordedLogs());
        _checkHolders(l, "final", holders);
    }

    function test_vault_migrationRetirementAndRedeem_exact() public {
        CampaignLedger l = new CampaignLedger("FS-7.vault.retirement.synthetic", EvidenceClass.SYNTHETIC);
        _runRetirement(VaultModel.Rule.CORRECT, l);
        assertEq(uint256(l.aggregate()), uint256(Verdict.PASS));
        assertGe(l.count(), 40);
        l.write("vault-retirement.synthetic.json");
    }

    function test_vault_wrongRetirementRuleRejected() public {
        CampaignLedger l = new CampaignLedger("control.retireKeepsProfit", EvidenceClass.SYNTHETIC);
        _runRetirement(VaultModel.Rule.WRONG_RETIRE_KEEPS_PROFIT, l);
        assertEq(uint256(l.verdictOf("retire.TMA")), uint256(Verdict.FAIL));
        assertEq(uint256(l.verdictOf("retire.refundToFunder")), uint256(Verdict.FAIL));
    }

    function test_vault_wrongShareMathRejected() public {
        CampaignLedger l = new CampaignLedger("control.zeroDecimalsOffset", EvidenceClass.SYNTHETIC);
        _runRetirement(VaultModel.Rule.WRONG_ZERO_DECIMALS_OFFSET, l);
        l.write("control-zero-decimals-offset.json");
        assertEq(uint256(l.aggregate()), uint256(Verdict.FAIL));
        // The 10^0 offset differs by one wei on holder 2's odd balance; holder 1's round
        // balance coincides, which is why exact (0-wei) equality and odd balances are required.
        assertEq(uint256(l.verdictOf(string.concat("redeem.", vm.toString(h2), ".paid"))), uint256(Verdict.FAIL));
        assertEq(uint256(l.verdictOf(string.concat("redeem.", vm.toString(h1), ".paid"))), uint256(Verdict.PASS));
    }

    function test_holderLedger_ignoringBurnsRejected() public {
        _buildVaultState(false);
        HolderLedger wrong = _holderLedger(HolderLedger.Rule.WRONG_IGNORE_BURNS);
        _upgradeVaultSide();
        vm.prank(address(tl));
        factory.retireVault(address(vault));
        uint256 shares = vault.balanceOf(h1);
        vm.prank(h1);
        vault.redeem(shares, h1, h1);
        wrong.replay(vm.getRecordedLogs());
        CampaignLedger l = new CampaignLedger("control.ignoreBurns", EvidenceClass.SYNTHETIC);
        _checkHolders(l, "final", wrong);
        assertEq(uint256(l.verdictOf("final.sumShares==supply")), uint256(Verdict.FAIL));
    }

    function _newR4Vault(string memory name) internal returns (YieldVault v) {
        vm.startPrank(address(tl));
        factory.setApprovedPerformanceFeeBps(1000);
        factory.addCounterpartyToWhitelist(counterparty);
        YieldVault.RoundParams memory round =
            YieldVault.RoundParams(2 days, 7 days, 600, 10_000 ether, 0, 1 ether, 1000, 5000, 0);
        v = YieldVault(
            factory.createYieldVault(
                YieldVault.InitParams(
                    address(core.stPROS), address(factory), address(tl), counterparty, counterparty, name, name, round, ops
                )
            )
        );
        vm.stopPrank();
    }

    /// @notice Model-driven deposits (share math) by the three holders, then lock to maturity.
    function _depositAndLock(CampaignLedger l, YieldVault v, VaultModel m) internal {
        address[3] memory hs = _holders();
        uint256[3] memory amounts = [uint256(70 ether), 13_333_333_333_333_333_333, 2 ether + 3];
        for (uint256 i; i < 3; ++i) {
            vm.startPrank(hs[i]);
            core.stPROS.approve(address(v), amounts[i]);
            uint256 shares = v.deposit(amounts[i], hs[i]);
            vm.stopPrank();
            l.eq(string.concat("deposit.", vm.toString(i), ".shares"), m.deposit(hs[i], amounts[i]), shares);
        }
        vm.warp(v.openDeadline() + v.lockDuration());
        m.lock();
    }

    function _redeemThirds(CampaignLedger l, string memory id, YieldVault v, VaultModel m) internal {
        address[3] memory hs = _holders();
        for (uint256 i; i < 3; ++i) {
            uint256 sh = v.balanceOf(hs[i]) / 3;
            uint256 predicted = m.redeem(hs[i], sh);
            vm.prank(hs[i]);
            l.eq(string.concat(id, ".redeem.", vm.toString(i)), predicted, v.redeem(sh, hs[i], hs[i]));
        }
        l.eq(string.concat(id, ".final.TMA"), m.managed(), v.totalManagedAssets());
        l.eq(string.concat(id, ".final.supply"), m.supply(), v.totalSupply());
        l.eq(string.concat(id, ".final.custody"), m.custody(), core.stPROS.balanceOf(address(v)));
    }

    /// @notice R4 continuation on a fresh R4 vault: a PROFIT round funded at proposal with a 10 %
    ///         fee to the recipient, then redemptions.
    function test_r4Vault_profitRound_exact() public {
        _buildVaultState(false);
        _upgradeVaultSide();
        YieldVault v = _newR4Vault("R4P");
        CampaignLedger l = new CampaignLedger("FS-1.POST-5.vaultProfit.synthetic", EvidenceClass.SYNTHETIC);
        VaultModel m = new VaultModel(VaultModel.Rule.CORRECT);
        m.seedVault(0, 0, 0, VaultModel.Phase.OPEN, 1000);
        _depositAndLock(l, v, m);

        uint256 profit = 5 ether + 1;
        _v1DepositWithPROS(core, ops, profit);
        vm.startPrank(ops);
        core.stPROS.approve(address(v), profit);
        v.proposeSettlement(YieldVault.SettleMode.PROFIT, profit, ops);
        vm.stopPrank();
        m.propose(true, profit, ops);
        vm.warp(block.timestamp + v.settleTimelockWindow());
        uint256 recipientBefore = core.stPROS.balanceOf(counterparty);
        v.finalize();
        l.eq("profit.fee", m.finalize(), core.stPROS.balanceOf(counterparty) - recipientBefore);
        l.eq("profit.TMA", m.managed(), v.totalManagedAssets());
        _redeemThirds(l, "profit", v, m);
        assertEq(uint256(l.aggregate()), uint256(Verdict.PASS));
        l.write("vault-r4-profit.synthetic.json");
    }

    /// @notice R4 LOSS round proposed by the Timelock (impersonated; the scheduled-operation path
    ///         is FS-3.8 work): TMA falls by the loss, the counterparty receives it exactly.
    function test_r4Vault_lossRound_exact() public {
        _buildVaultState(false);
        _upgradeVaultSide();
        YieldVault v = _newR4Vault("R4L");
        CampaignLedger l = new CampaignLedger("FS-3.8.vaultLoss.synthetic", EvidenceClass.SYNTHETIC);
        VaultModel m = new VaultModel(VaultModel.Rule.CORRECT);
        m.seedVault(0, 0, 0, VaultModel.Phase.OPEN, 1000);
        _depositAndLock(l, v, m);

        uint256 loss = 7 ether + 11;
        vm.prank(address(tl));
        v.proposeSettlement(YieldVault.SettleMode.LOSS, loss, address(0));
        m.propose(false, loss, address(0));
        vm.warp(block.timestamp + v.settleTimelockWindow());
        uint256 counterpartyBefore = core.stPROS.balanceOf(counterparty);
        v.finalize();
        l.eq("loss.toCounterparty", m.finalize(), core.stPROS.balanceOf(counterparty) - counterpartyBefore);
        l.eq("loss.TMA", m.managed(), v.totalManagedAssets());
        _redeemThirds(l, "loss", v, m);
        assertEq(uint256(l.aggregate()), uint256(Verdict.PASS));
        l.write("vault-r4-loss.synthetic.json");
    }
}

/// @notice Authority reconstruction and the approved GOVERNED matrix on the migrated vault.
contract AuthorityOracleTest is SyntheticVaultMigration {
    Vm.Log[] internal recordedLogs;

    function _replay(AuthorityMatrix.Rule rule) internal returns (AuthorityMatrix a) {
        Vm.Log[] memory recorded = vm.getRecordedLogs();
        for (uint256 i; i < recorded.length; ++i) {
            recordedLogs.push(recorded[i]);
        }
        a = new AuthorityMatrix(rule);
        a.replay(recordedLogs);
    }

    function _handoff() internal {
        _buildVaultState(false);
        _upgradeVaultSide();
        vm.prank(address(tl));
        vault.grantRole(keccak256("OPERATOR_ROLE"), ops);
        vm.prank(gov);
        vault.renounceRole(bytes32(0), gov);
    }

    function test_authority_replayEqualsChainAndGovernedMatrix() public {
        _handoff();
        AuthorityMatrix a = _replay(AuthorityMatrix.Rule.CORRECT);
        a.watchEverywhere(counterparty);
        a.watchEverywhere(h1);
        CampaignLedger l = new CampaignLedger("FS-1.POST-2.authority.synthetic", EvidenceClass.SYNTHETIC);
        a.checkChain(l, "replay");
        address[] memory admin = new address[](1);
        admin[0] = address(tl);
        address[] memory operator = new address[](1);
        operator[0] = ops;
        a.checkExact(l, "GOVERNED.admin", address(vault), bytes32(0), admin);
        a.checkExact(l, "GOVERNED.operator", address(vault), keccak256("OPERATOR_ROLE"), operator);
        a.checkOwner(l, "factory", address(factory), address(tl));
        assertEq(uint256(l.aggregate()), uint256(Verdict.PASS));
        // Non-vacuous: the stale operator, the old admin and both new holders are all checked.
        assertGe(a.accountsOf(address(vault)).length, 6);
        l.write("authority-governed.synthetic.json");
    }

    function test_authority_ignoringRevocationsRejected() public {
        _handoff();
        AuthorityMatrix a = _replay(AuthorityMatrix.Rule.WRONG_IGNORE_REVOKES);
        CampaignLedger l = new CampaignLedger("control.ignoreRevokes", EvidenceClass.SYNTHETIC);
        a.checkChain(l, "replay");
        assertEq(uint256(l.aggregate()), uint256(Verdict.FAIL), "stale operator and renounced admin detected");
    }

    /// @notice An unauthorized extra privilege (FS-1a ix shape) is caught by the approved matrix.
    function test_authority_extraPrivilegeRejected() public {
        _handoff();
        vm.prank(address(tl));
        vault.grantRole(keccak256("OPERATOR_ROLE"), h1);
        AuthorityMatrix a = _replay(AuthorityMatrix.Rule.CORRECT);
        CampaignLedger l = new CampaignLedger("control.extraOperator", EvidenceClass.SYNTHETIC);
        address[] memory operator = new address[](1);
        operator[0] = ops;
        a.checkExact(l, "GOVERNED.operator", address(vault), keccak256("OPERATOR_ROLE"), operator);
        assertEq(uint256(l.aggregate()), uint256(Verdict.FAIL));
    }
}

/// @notice StPROS holder ledger across the real V1 → R4 migration (escrow burn included) and the
///         outstanding-liability relations.
contract StPROSHolderOracleTest is SyntheticV1Migration {
    function setUp() public {
        vm.recordLogs();
        _buildV1State();
    }

    function _check(HolderLedger.Rule rule) internal returns (CampaignLedger l) {
        _migrate();
        HolderLedger holders = new HolderLedger(address(st), rule);
        holders.replay(vm.getRecordedLogs());
        l = new CampaignLedger("FS-5.holders.synthetic", EvidenceClass.SYNTHETIC);
        address[] memory accounts = holders.accounts();
        for (uint256 i; i < accounts.length; ++i) {
            l.eq(string.concat("balance.", vm.toString(accounts[i])), holders.balanceOf(accounts[i]), st.balanceOf(accounts[i]));
        }
        l.eq("S'==sumHolders", holders.totalSupply(), st.totalSupply());
        (uint256 t, uint256 v) = oracle.poolInfo(legacy.wpros);
        l.eq("I-1.V==S'", holders.totalSupply(), v);
        l.eq("pool.T==S'", holders.totalSupply(), t);
        l.eq("liabilities.Q-C==ghostPending", ghost.totalPending(), st.queuedWithdrawal() - st.completedWithdrawal());
    }

    function test_holders_replayAcrossMigration_exact() public {
        CampaignLedger l = _check(HolderLedger.Rule.CORRECT);
        assertEq(uint256(l.aggregate()), uint256(Verdict.PASS));
        assertEq(st.balanceOf(address(st)), 0, "escrow burned");
    }

    function test_holders_ignoringEscrowBurnRejected() public {
        CampaignLedger l = _check(HolderLedger.Rule.WRONG_IGNORE_BURNS);
        assertEq(uint256(l.verdictOf("S'==sumHolders")), uint256(Verdict.FAIL));
    }
}

/// @notice Review finding IV-02: the vault model's share conversions and fee are exact over the
///         full uint256 domain, like the production full-precision `mulDiv`. Three independent
///         sources are compared: (1) the mathematical value (closed form, or exact big-integer
///         literals computed outside Solidity), (2) the production `YieldVault` conversion
///         (real implementation bytecode, TMA and supply written into its storage), and (3) the
///         model. A fourth, algorithmically different reference (double-and-add over quotient and
///         remainder, never forming a 512-bit product) checks the model off the literal vectors.
contract VaultModelPrecisionTest is Test {
    using stdStorage for StdStorage;

    uint256 internal constant OFFSET = 1e6;

    /// @dev Exact ⌊a · b / d⌋ by double-and-add over the bits of `b`, keeping the pair
    ///      (quotient, remainder < d) exact at every step; checked arithmetic reverts when the
    ///      quotient does not fit. Independent of `FullMulDiv` and of the production `Math.mulDiv`.
    function referenceMulDiv(uint256 a, uint256 b, uint256 d) public pure returns (uint256 q) {
        uint256 qa = a / d;
        uint256 ra = a % d;
        uint256 r;
        for (uint256 i = 256; i > 0; --i) {
            q = q * 2;
            // r + r without overflow: r + r >= d  <=>  r >= d - r
            if (r >= d - r) {
                r = r - (d - r);
                q += 1;
            } else {
                r = r + r;
            }
            if ((b >> (i - 1)) & 1 == 1) {
                q += qa;
                if (r >= d - ra) {
                    r = r - (d - ra);
                    q += 1;
                } else {
                    r = r + ra;
                }
            }
        }
    }

    function _production(uint256 managed, uint256 supply) internal returns (YieldVault v) {
        v = new YieldVault();
        stdstore.target(address(v)).sig("totalManagedAssets()").checked_write(managed);
        stdstore.target(address(v)).sig("totalSupply()").checked_write(supply);
        assertEq(v.totalAssets(), managed);
        assertEq(v.totalSupply(), supply);
    }

    function _model(uint256 managed, uint256 supply) internal returns (VaultModel m) {
        m = new VaultModel(VaultModel.Rule.CORRECT);
        m.seedVault(managed, supply, 0, VaultModel.Phase.OPEN, 0);
    }

    /// @dev Compares mathematical value, production and model (and the double-and-add reference).
    function _three(uint256 managed, uint256 supply, uint256 x, uint256 depositShares, uint256 redeemAssets) internal {
        YieldVault v = _production(managed, supply);
        VaultModel m = _model(managed, supply);
        assertEq(v.previewDeposit(x), depositShares, "production deposit");
        assertEq(m.previewDeposit(x), depositShares, "model deposit");
        assertEq(referenceMulDiv(x, supply + OFFSET, managed + 1), depositShares, "reference deposit");
        assertEq(v.previewRedeem(x), redeemAssets, "production redeem");
        assertEq(m.previewRedeem(x), redeemAssets, "model redeem");
        assertEq(referenceMulDiv(x, managed + 1, supply + OFFSET), redeemAssets, "reference redeem");
    }

    /// @notice Agent C's recorded case: managed = supply = 2^200, input 2^100. Closed form:
    ///         deposit = ⌊2^100 · (2^200 + 10^6) / (2^200 + 1)⌋ = 2^100 + ⌊2^100 · 999999 / (2^200 + 1)⌋ = 2^100;
    ///         redeem = 2^100 − ⌈2^100 · 999999 / (2^200 + 10^6)⌉ = 2^100 − 1 (the subtracted term lies in (0, 1)).
    function test_precision_recordedCase_managedSupply2pow200_input2pow100() public {
        uint256 x = uint256(1) << 100;
        _three(uint256(1) << 200, uint256(1) << 200, x, x, x - 1);
    }

    /// @notice Exact vectors (values from Python arbitrary-precision integers): zero and empty-vault
    ///         edges, small odd values, exact divisions and their one-wei neighbours with 354-bit
    ///         intermediate products, the full-width identity with 512-bit products, and a mixed case.
    function test_precision_exactVectors() public {
        // Empty vault: one virtual asset and 10^6 virtual shares.
        _three(0, 0, 0, 0, 0);
        _three(0, 0, 1, 1_000_000, 0);
        _three(0, 0, 999_999, 999_999_000_000, 0);
        _three(0, 0, 1_000_000, 1_000_000_000_000, 1);
        // Small odd values: ⌊7 · 1000003 / 101⌋ = 69307 (rounded down from 69307.13…).
        _three(100, 3, 7, 69_307, 0);
        // TMA + 1 = 2^200, supply + 10^6 = 3 · 2^200: deposit is exactly 3x, redeem ⌊x / 3⌋.
        uint256 tma = (uint256(1) << 200) - 1;
        uint256 sup = 3 * (uint256(1) << 200) - OFFSET;
        uint256 x = 3 * (uint256(1) << 150);
        _three(tma, sup, x, 9 * (uint256(1) << 150), uint256(1) << 150);
        _three(tma, sup, x - 1, 9 * (uint256(1) << 150) - 3, (uint256(1) << 150) - 1);
        _three(tma, sup, x + 1, 9 * (uint256(1) << 150) + 3, uint256(1) << 150);
        // Full width: TMA + 1 = supply + 10^6 = 2^256 − 1; both conversions are the identity.
        uint256 max = type(uint256).max;
        _three(max - 1, max - OFFSET, max, max, max);
        _three(max - 1, max - OFFSET, max - 1, max - 1, max - 1);
        // Mixed magnitudes.
        _three(
            1_000_000_000_000_000_000_000_000_000_007,
            1_000_000_000_000_000_000_000_000_000_000_000_013,
            (uint256(1) << 180) + 1,
            1_532_495_540_865_888_858_358_347_027_139_581_734_755_119_931_431_648_906_468_545,
            1_532_495_540_865_888_858_358_347_027_161_036_632_482_358_312_935
        );
    }

    /// @notice Where the exact result exceeds uint256 every source refuses; the model never
    ///         returns a wrapped or truncated value.
    function test_precision_resultOverflowRefusedEverywhere() public {
        uint256 managed = uint256(1) << 255;
        YieldVault v = _production(managed, 1);
        VaultModel m = _model(managed, 1);
        uint256 x = uint256(1) << 255;
        // Deposit still fits: ⌊2^255 · 1000001 / (2^255 + 1)⌋ = 1000000.
        assertEq(v.previewDeposit(x), 1_000_000);
        assertEq(m.previewDeposit(x), 1_000_000);
        vm.expectRevert();
        v.previewRedeem(x);
        vm.expectRevert();
        m.previewRedeem(x);
        vm.expectRevert();
        this.referenceMulDiv(x, managed + 1, 1 + OFFSET);
    }

    /// @notice PROFIT fee at full precision: ⌊2^250 · 1000 / 10^4⌋, as the production `mulDiv`.
    function test_precision_profitFeeLargeAmount() public {
        VaultModel m = new VaultModel(VaultModel.Rule.CORRECT);
        m.seedVault(1, 1, 1, VaultModel.Phase.LOCKED, 1000);
        uint256 amount = uint256(1) << 250;
        m.propose(true, amount, address(this));
        uint256 fee = m.finalize();
        assertEq(fee, 180925139433306555349329664076074856020734351040063381311652475012364265062);
        assertEq(m.managed(), 1 + amount - fee);
        assertEq(fee, referenceMulDiv(amount, 1000, 10_000));
    }

    /// @notice Production and model agree with the reference on arbitrary uint256 operands, and all
    ///         three revert together exactly where the result does not fit.
    function testFuzz_precision_modelEqualsProductionAndReference(uint256 managed, uint256 supply, uint256 x) public {
        managed = bound(managed, 0, type(uint256).max - 1);
        supply = bound(supply, 0, type(uint256).max - OFFSET);
        YieldVault v = _production(managed, supply);
        VaultModel m = _model(managed, supply);
        _agree(v, m, x, supply + OFFSET, managed + 1, true);
        _agree(v, m, x, managed + 1, supply + OFFSET, false);
    }

    function _agree(YieldVault v, VaultModel m, uint256 x, uint256 num, uint256 den, bool isDeposit) internal view {
        (bool okRef, uint256 ref) = _try(abi.encodeCall(this.referenceMulDiv, (x, num, den)), address(this));
        bytes memory call = isDeposit ? abi.encodeWithSignature("previewDeposit(uint256)", x)
            : abi.encodeWithSignature("previewRedeem(uint256)", x);
        (bool okProd, uint256 prod) = _try(call, address(v));
        (bool okModel, uint256 model) = _try(call, address(m));
        assertEq(okProd, okRef, "production refusal domain");
        assertEq(okModel, okRef, "model refusal domain");
        if (okRef) {
            assertEq(prod, ref, "production value");
            assertEq(model, ref, "model value");
        }
    }

    function _try(bytes memory data, address target) internal view returns (bool ok, uint256 value) {
        bytes memory ret;
        (ok, ret) = target.staticcall(data);
        if (ok) value = abi.decode(ret, (uint256));
    }

    /// @notice The share-offset negative control still differs from the correct rule.
    function test_precision_wrongOffsetStillRejected() public {
        VaultModel wrong = new VaultModel(VaultModel.Rule.WRONG_ZERO_DECIMALS_OFFSET);
        wrong.seedVault(uint256(1) << 200, uint256(1) << 200, 0, VaultModel.Phase.OPEN, 0);
        YieldVault v = _production(uint256(1) << 200, uint256(1) << 200);
        uint256 x = uint256(1) << 100;
        assertTrue(wrong.previewRedeem(x) != v.previewRedeem(x));
    }
}

/// @dev `hasRole` answers only what the test sets; everything else is false.
contract RoleTable {
    mapping(bytes32 => mapping(address => bool)) public has;

    function set(bytes32 role, address account, bool value) external {
        has[role][account] = value;
    }

    function hasRole(bytes32 role, address account) external view returns (bool) {
        return has[role][account];
    }
}

/// @notice Review finding IV-03: every authority check id names the target, the complete 32-byte
///         role and the account, so distinct tuples never share an id and a failing comparison is
///         attributable to exactly one tuple. Permission calculation is unchanged.
contract AuthorityEvidenceTest is Test {
    address internal constant ACCOUNT = address(0xA11CE);
    // Same first four bytes, different role.
    bytes32 internal constant ROLE_A = 0x01020304000000000000000000000000000000000000000000000000000000aa;
    bytes32 internal constant ROLE_B = 0x01020304000000000000000000000000000000000000000000000000000000bb;

    function _id(string memory prefix, address target, bytes32 role, address account) internal pure returns (string memory) {
        return string.concat(
            prefix, ".", vm.toLowercase(vm.toString(target)), ".", vm.toString(role), ".", vm.toLowercase(vm.toString(account))
        );
    }

    function test_authorityIds_sharedRolePrefixAreDistinct() public {
        RoleTable t = new RoleTable();
        AuthorityMatrix a = new AuthorityMatrix(AuthorityMatrix.Rule.CORRECT);
        a.watch(address(t), ROLE_A, ACCOUNT);
        a.watch(address(t), ROLE_B, ACCOUNT);
        CampaignLedger l = new CampaignLedger("iv03.prefix", EvidenceClass.SYNTHETIC);
        a.checkChain(l, "roles");
        assertEq(l.count(), 2);
        assertEq(l.checkAt(0).id, _id("roles", address(t), ROLE_A, ACCOUNT));
        assertEq(l.checkAt(1).id, _id("roles", address(t), ROLE_B, ACCOUNT));
        assertTrue(keccak256(bytes(l.checkAt(0).id)) != keccak256(bytes(l.checkAt(1).id)));

        address[] memory none = new address[](0);
        a.checkExact(l, "exact", address(t), ROLE_A, none);
        a.checkExact(l, "exact", address(t), ROLE_B, none);
        assertEq(l.checkAt(2).id, _id("exact", address(t), ROLE_A, ACCOUNT));
        assertEq(l.checkAt(3).id, _id("exact", address(t), ROLE_B, ACCOUNT));
        assertEq(uint256(l.aggregate()), uint256(Verdict.PASS));
    }

    function test_authorityIds_sameRoleOnDifferentTargetsAreDistinct() public {
        RoleTable t1 = new RoleTable();
        RoleTable t2 = new RoleTable();
        AuthorityMatrix a = new AuthorityMatrix(AuthorityMatrix.Rule.CORRECT);
        a.watch(address(t1), ROLE_A, ACCOUNT);
        a.watch(address(t2), ROLE_A, ACCOUNT);
        CampaignLedger l = new CampaignLedger("iv03.targets", EvidenceClass.SYNTHETIC);
        address[] memory none = new address[](0);
        a.checkExact(l, "exact", address(t1), ROLE_A, none);
        a.checkExact(l, "exact", address(t2), ROLE_A, none);
        assertEq(l.count(), 2);
        assertEq(l.checkAt(0).id, _id("exact", address(t1), ROLE_A, ACCOUNT));
        assertEq(l.checkAt(1).id, _id("exact", address(t2), ROLE_A, ACCOUNT));
        assertTrue(keccak256(bytes(l.checkAt(0).id)) != keccak256(bytes(l.checkAt(1).id)));
        a.checkChain(l, "roles");
        assertTrue(keccak256(bytes(l.checkAt(2).id)) != keccak256(bytes(l.checkAt(3).id)));
    }

    /// @notice An unexpected privilege on one of two prefix-sharing roles fails at that role's own
    ///         id, the other stays PASS, and the aggregate is FAIL.
    function test_authorityIds_incorrectPermissionStaysVisible() public {
        RoleTable t = new RoleTable();
        t.set(ROLE_B, ACCOUNT, true);
        AuthorityMatrix a = new AuthorityMatrix(AuthorityMatrix.Rule.CORRECT);
        a.watch(address(t), ROLE_A, ACCOUNT);
        a.watch(address(t), ROLE_B, ACCOUNT);
        CampaignLedger l = new CampaignLedger("iv03.visible", EvidenceClass.SYNTHETIC);
        a.checkChain(l, "roles");
        address[] memory none = new address[](0);
        a.checkExact(l, "exact", address(t), ROLE_B, none);
        assertEq(uint256(l.verdictOf(_id("roles", address(t), ROLE_A, ACCOUNT))), uint256(Verdict.PASS));
        assertEq(uint256(l.verdictOf(_id("roles", address(t), ROLE_B, ACCOUNT))), uint256(Verdict.FAIL));
        assertEq(uint256(l.verdictOf(_id("exact", address(t), ROLE_B, ACCOUNT))), uint256(Verdict.FAIL));
        assertEq(uint256(l.aggregate()), uint256(Verdict.FAIL));
        assertEq(uint256(l.scenarioVerdict()), uint256(Verdict.FAIL));
    }
}
