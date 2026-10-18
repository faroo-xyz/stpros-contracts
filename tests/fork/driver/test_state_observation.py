"""Offline tests of the version-aware state observation, the campaign-input blocker and the integrated pipeline.

Run inside an approved export:
  tools/verify/run.sh "$dest" python3 -m unittest discover -s tests/fork/driver -p 'test_state_observation.py' -v

The protocol below is a FAKE chain (SIMULATION ONLY) on registry addresses. Implementation, proxy, ProxyAdmin and
WPROS code is the recorded legacy runtime from ``tests/fixtures/legacy/``, so profile selection runs against the
reviewed identities; beacon, vault-proxy and Safe code is synthetic and the reviewed identities for those are
replaced by the synthetic ones in an explicit test bundle. The loopback rehearsal runs the recorded YieldVault_V1,
YieldVault_V3 and Oracle_V3 runtimes on a local Anvil node with zero accounts; without ``anvil`` it is NOT RUN.
The Safe module pages follow a reference model of the published 1.3.0 and 1.4.x ``getModulesPaginated`` (SIMULATION
ONLY; no singleton version is inferred or approved).
"""
import asyncio
import copy
import hashlib
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
from unittest import mock

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
sys.path[:0] = [str(HERE), str(ROOT)]
import campaign_input as ci  # noqa: E402
import collection_plan as cp  # noqa: E402
import collector as col  # noqa: E402
import history as h  # noqa: E402
import history_inventory as hi  # noqa: E402
import observe  # noqa: E402
import state_observation as so  # noqa: E402
import test_collector as tc  # noqa: E402
import transport as tp  # noqa: E402
from history import HistoryRefused  # noqa: E402
from tools.lib import abi  # noqa: E402
from tools.lib.keccak import keccak256  # noqa: E402

T = h.logscan.T
E = so.E
word, amount = tc.word, tc.amount
ALICE, BOB, E2 = tc.ALICE, tc.BOB, tc.E2
E1 = '0x4597c97a43dfbb4a398e2b16aa9ce61f90d801dd'
SAFE = tc.SAFE
ZERO20 = tc.ZERO20
ADMIN = hi.DEFAULT_ADMIN_ROLE
FIXTURES = ROOT / 'tests/fixtures/legacy'
KEEPER = '0x' + 'ee' * 20
SINGLETON = '0x' + '5a' * 20
TS0 = 1_700_000_000


def ts(block):
    return TS0 + block


def runtime(chain, name):
    return json.loads((FIXTURES / chain / f'{name}.json').read_text())['runtimeCode']


def enc(types, values):
    return abi.encode_parameters([{'type': t} for t in types], values)


def data(*values):
    return '0x' + ''.join(format(int(v), '064x') for v in values)


def module(i):
    return '0x' + format(0x5f00 + i, '040x')


def safe_page(modules, start, size, form):
    """``getModulesPaginated(start, size)`` of a Safe whose module list is ``modules`` (SIMULATION ONLY), after the
    published ModuleManager: 1.3.0 returns the next unread module as ``next``, 1.4.x the last returned module while
    more remain; both return the sentinel at the end."""
    linked = [so.SENTINEL, *modules, so.SENTINEL]
    after = {linked[i]: linked[i + 1] for i in range(len(linked) - 1)}
    out, current = [], after[start]
    while current != so.SENTINEL and len(out) < size:
        out.append(current)
        current = after[current]
    if form == '1.3.0' or current == so.SENTINEL:
        return out, current
    return out, out[-1]


def safe_pages(modules, form):
    """The pages the reviewed reader records for ``modules`` (each page starts at the last module of the one before)."""
    pages, start = [], so.SENTINEL
    while True:
        out, nxt = safe_page(modules, start, so.MODULE_PAGE, form)
        pages.append({'start': start, 'modules': out, 'next': nxt})
        if nxt == so.SENTINEL:
            return pages
        start = out[-1]


def install_modules(fake, modules, form):
    for start in (so.SENTINEL, *modules):
        fake.calls[(SAFE, abi.calldata(observe.MODULES_SIGNATURE, start, so.MODULE_PAGE))] = enc(
            ['address[]', 'address'], list(safe_page(modules, start, so.MODULE_PAGE, form)))


class Protocol:
    """Builds the synthetic protocol history and registers every getter the observation reads."""

    def __init__(self, chain, *, safe_modules=(), safe_form='1.3.0', oracle_vtokens=None):
        self.chain = chain
        self.reg = tc.REG[chain]
        self.recorded = json.loads((ROOT / 'snapshots/deployed' / chain / 'state.json').read_text())
        self.safe_modules, self.safe_form = list(safe_modules), safe_form
        # extra Oracle vTokens: address -> {'flag': current registration, 'asset': asset() result or None (no getter)};
        # history registers each one and, when the flag is false, unregisters it again
        self.oracle_vtokens = dict(oracle_vtokens or {})

    def __call__(self, world, fake):
        self.world, self.fake = world, fake
        reg, chain = self.reg, self.chain
        rows, labels, post = [], {}, []
        pa = reg['proxy_admin']
        oracle = self.oracle = next(a for a, x in reg['expected_proxies'].items() if x['name'] == 'Oracle')
        self.bridge = next((a for a, x in reg['expected_proxies'].items() if x['name'] == 'BridgeVault'), None)
        self.slp = next((a for a, x in reg['expected_proxies'].items() if x['name'] == 'SlpDistributor'), None)
        self.reporter = next((a for a, label in reg['accounts'].items() if label == 'SLP contract'), None)
        owner = SAFE if chain == 'mainnet' else E1
        rows.append(fake.row(pa, [T['OwnershipTransferred'], word(ZERO20), word(owner)], 101, 0))
        labels[pa] = ['proxy_admin']
        for k, (proxy, x) in enumerate(sorted(reg['expected_proxies'].items())):
            block = 102 + k
            rows += [fake.row(proxy, [T['Upgraded'], word(x['implementation'])], block, 0),
                     fake.row(proxy, [T['AdminChanged']], block, 1, '0x' + '00' * 32 + word(pa)[2:])]
            if proxy not in reg['factories']:
                rows.append(fake.row(proxy, [T['OwnershipTransferred'], word(ZERO20), word(owner)], block, 2))
            labels.setdefault(proxy, [f'record:{x["name"]}'])
        for i, factory in enumerate(world.factories):
            rows.append(fake.row(world.beacons[factory], [T['OwnershipTransferred'], word(ZERO20), word(factory)], 122, i))
            cpty = self.counterparty(world.vaults[factory])
            rows.append(fake.row(factory, [E['CounterpartyWhitelistUpdated'], word(cpty)], 125, i, data(1)))
        stpros = self.stpros = reg['stpros']
        rows += [fake.row(stpros, [T['Transfer'], word(ZERO20), word(ALICE)], 130, 0, amount(10 ** 6)),
                 fake.row(stpros, [T['Transfer'], word(ZERO20), word(E2)], 130, 1, amount(10 ** 4)),
                 fake.row(stpros, [E['OracleChanged'], word(ZERO20), word(oracle)], 131, 0),
                 fake.row(stpros, [E['UnbondingPeriodChanged']], 131, 1, data(604800, 604800)),
                 fake.row(stpros, [E['MaxWithdrawCountChanged']], 131, 2, data(5))]
        self.supply = 10 ** 6 + 10 ** 4
        if chain == 'testnet':
            rows += [fake.row(stpros, [E['SlpChanged'], word(ZERO20), word(self.slp)], 132, 0),
                     fake.row(stpros, [E['BridgeVaultChanged'], word(ZERO20), word(self.bridge)], 132, 1),
                     fake.row(oracle, [E['SlpChanged'], word(ZERO20), word(self.reporter)], 133, 0),
                     fake.row(oracle, [E['OracleVTokenSet'], word(stpros)], 133, 1, data(1)),
                     fake.row(oracle, [E['UpdateLimitsChanged']], 133, 2, data(10 ** 21, 3600)),
                     fake.row(oracle, [E['SetTokenAmount'], word(reg['wpros'])], 133, 3, data(2 * self.supply, self.supply)),
                     fake.row(self.bridge, [E['BridgeVTokenSet'], word(stpros)], 134, 0, data(1, 1)),
                     fake.row(self.bridge, [E['WethChanged'], word(ZERO20), word(reg['wpros'])], 134, 1),
                     fake.row(self.slp, [E['SlpWhitelistUpdated'], word(self.reporter)], 135, 0, data(1)),
                     fake.row(self.slp, [E['KeeperChanged'], word(ZERO20), word(KEEPER)], 135, 1)]
            post.append(fake.row(oracle, [E['TokenAmountUpdated'], word(reg['wpros'])], 3150, 0, data(100, 2 * self.supply + 100)))
            for j, (vtoken, x) in enumerate(sorted(self.oracle_vtokens.items())):
                rows.append(fake.row(oracle, [E['OracleVTokenSet'], word(vtoken)], 136 + j, 0, data(1)))
                if not x['flag']:
                    rows.append(fake.row(oracle, [E['OracleVTokenSet'], word(vtoken)], 136 + j, 1, data(0)))
        self.vault_rows(world, fake, rows)
        for address in (oracle, self.bridge, self.slp):
            if address:
                labels.setdefault(address, ['record:proxy'])
        return {'rows': rows, 'labels': labels, 'post': post}

    def counterparty(self, vault):
        for f in self.recorded['factories'].values():
            for v in f['vaults']:
                if v['vault'] == vault:
                    return v['counterparty']
        raise KeyError(vault)

    def fee_recipient(self, vault):
        for f in self.recorded['factories'].values():
            for v in f['vaults']:
                if v['vault'] == vault:
                    return v['feeRecipient']
        raise KeyError(vault)

    def vault_rows(self, world, fake, rows):
        stpros = self.stpros
        self.vault_state = {}
        for i, factory in enumerate(world.factories):
            vault = world.vaults[factory]
            base = 160 + 400 * i
            opened, deadline = ts(base), ts(base) + 100
            settled = self.chain == 'mainnet' and i == 0
            rows += [fake.row(vault, [E['RoundOpened'], word('0x' + '0' * 39 + '1')], base, 0, data(opened, deadline, 10 ** 24)),
                     fake.row(vault, [T['Transfer'], word(ZERO20), word(ALICE)], base + 1, 0, amount(100 * 10 ** 6)),
                     fake.row(vault, [E['Deposit'], word(ALICE), word(ALICE)], base + 1, 1, data(100, 100 * 10 ** 6)),
                     fake.row(stpros, [T['Transfer'], word(ALICE), word(vault)], base + 1, 2, amount(100))]
            state = {'phase': 0, 'slot15': 0, 'settleProposedAt': 0, 'settleMode': 0, 'settleAmount': 0,
                     'profitFunded': False, 'profitFunder': ZERO20, 'settledAt': 0, 'tma': 100,
                     'opened': opened, 'deadline': deadline}
            if settled:
                fee_to = self.fee_recipient(vault)
                rows += [fake.row(vault, [E['RoundLocked'], word('0x' + '0' * 39 + '1')], base + 200, 0, data(deadline, deadline + 50)),
                         fake.row(stpros, [T['Transfer'], word(E2), word(vault)], base + 200, 1, amount(10)),
                         fake.row(vault, [E['SettlementProposed'], word('0x' + '0' * 39 + '1'), word('0x' + '0' * 40), word(E2)],
                                  base + 200, 2, data(10, ts(base + 200), 1)),
                         fake.row(stpros, [T['Transfer'], word(vault), word(fee_to)], base + 300, 0, amount(1)),
                         fake.row(vault, [E['SettlementFinalized'], word('0x' + '0' * 39 + '1'), word('0x' + '0' * 40)],
                                  base + 300, 1, data(10, 9, 1, ts(base + 300)))]
                state.update(phase=3, slot15=deadline, settleProposedAt=ts(base + 200), settleAmount=10, profitFunded=True,
                             profitFunder=E2, settledAt=ts(base + 300), tma=109)
            self.vault_state[vault] = state

    # -- getters --------------------------------------------------------------------------------------------------
    def install(self, world):
        """Registers code, storage and getters consistent with the fake history at the anchor."""
        f, reg, chain = world.fake, self.reg, self.chain
        name = {'mainnet': {'Oracle': 'Oracle_V1', 'StPROS': 'StPROS_V1', 'YieldVaultFactory': 'YieldVaultFactory_V1',
                            'YieldVaultFactory_test': 'YieldVaultFactory_V1'},
                'testnet': {'Oracle': 'Oracle_V3', 'StPROS': 'StPROS_V3', 'YieldVaultFactory': 'YieldVaultFactory_V3',
                            'BridgeVault': 'BridgeVault_V3', 'SlpDistributor': 'SlpDistributor_V3'}}[chain]
        vault_profile = 'YieldVault_V1' if chain == 'mainnet' else 'YieldVault_V3'
        tup = runtime(chain, 'TransparentUpgradeableProxy_V4')
        rows = f.rows
        balances = {t: h.logscan.replay_balances([r for r in rows if int(r['blockNumber'], 16) <= tc.ANCHOR], t)
                    for t in [self.stpros, *world.vaults.values()]}

        def call(to, sig, *args, ret):
            f.calls[(to, abi.calldata(sig, *args))] = ret
        for proxy, x in reg['expected_proxies'].items():
            f.code[proxy] = (101, tup)
            f.code[x['implementation']] = (101, runtime(chain, name[x['name']]))
            f.storage[(proxy, int(h.logscan.EIP1967_IMPL, 16))] = word(x['implementation'])
            f.storage[(proxy, int(h.logscan.EIP1967_ADMIN, 16))] = word(reg['proxy_admin'])
            f.storage[(proxy, so.OZ_INITIALIZABLE)] = amount(x['version'])
        owners = {r['address']: abi.topic_addr(r['topics'][2]) for r in rows if r['topics'][0] == T['OwnershipTransferred']}
        for target, owner in owners.items():
            call(target, 'owner()', ret=word(owner))
        f.code[reg['proxy_admin']] = (101, runtime(chain, 'ProxyAdmin_V4'))
        f.code[reg['wpros']] = (101, runtime(chain, 'WPROS'))
        vault_impl = '0x' + '1f' * 20
        f.code[vault_impl] = (101, runtime(chain, vault_profile))
        for factory in world.factories:
            beacon, vault = world.beacons[factory], world.vaults[factory]
            f.code[beacon] = (101, '0x60be' + beacon[2:])
            f.code[vault] = (101, '0x60fa' + vault[2:])
            call(beacon, 'implementation()', ret=word(vault_impl))
            call(factory, 'beacon()', ret=word(beacon))
            call(factory, 'paused()', ret=amount(0))
            call(factory, 'currentImplementation()', ret=word(vault_impl))
            call(factory, 'getAllProxies()', ret=enc(['address[]'], [[vault]]))
            call(factory, 'isFactoryVault(address)', vault, ret=amount(1))
            call(factory, 'counterpartyWhitelist(address)', self.counterparty(vault), ret=amount(1))
            s = self.vault_state[vault]
            terms = {'openWindow': 100, 'lockDuration': 50, 'settleTimelockWindow': 30, 'roundCap': 10 ** 24,
                     'perAddressCap': 0, 'minSubscription': 1, 'performanceFeeBps': 1000, 'maxLossBps': 500,
                     'openedAt': s['opened'], 'openDeadline': s['deadline']}
            for k, v in terms.items():
                call(vault, f'{k}()', ret=amount(v))
            call(vault, 'counterparty()', ret=word(self.counterparty(vault)))
            call(vault, 'feeRecipient()', ret=word(self.fee_recipient(vault)))
            effective = 1 if s['phase'] == 0 and ts(tc.ANCHOR) >= s['deadline'] else s['phase']
            call(vault, 'phase()', ret=amount(effective))
            call(vault, 'lockedAt()', ret=amount(s['deadline'] if s['phase'] == 0 and ts(tc.ANCHOR) >= s['deadline'] else s['slot15']))
            call(vault, 'roundIndex()', ret=amount(1))
            for k in ('settleProposedAt', 'settleMode', 'settleAmount', 'settledAt'):
                call(vault, f'{k}()', ret=amount(s[k]))
            call(vault, 'profitFunded()', ret=amount(int(s['profitFunded'])))
            call(vault, 'profitFunder()', ret=word(s['profitFunder']))
            call(vault, 'totalManagedAssets()', ret=amount(s['tma']))
            call(vault, 'totalAssets()', ret=amount(s['tma']))
            call(vault, 'totalSupply()', ret=amount(sum(balances[vault].values())))
            call(vault, 'factory()', ret=word(factory))
            call(vault, 'asset()', ret=word(self.stpros))
            for role in (ADMIN, hi.OPERATOR_ROLE):
                call(vault, 'getRoleAdmin(bytes32)', role, ret=ADMIN)
            f.storage[(vault, so.SLOT_PHASE)] = amount(s['phase'])
            f.storage[(vault, so.SLOT_LOCKED_AT)] = amount(s['slot15'])
            f.storage[(vault, so.OZ_INITIALIZABLE)] = amount(1)
            candidates = set(reg['accounts']) | {reg['proxy_admin']} | set(reg['factories'])
            for account in candidates | {E2}:
                for role in (ADMIN, hi.OPERATOR_ROLE):
                    call(vault, 'hasRole(bytes32,address)', role, account, ret=amount(int(role == ADMIN and account == E2)))
        for token, accounts in balances.items():
            call(token, 'totalSupply()', ret=amount(sum(accounts.values())))
            for account, value in accounts.items():
                call(token, 'balanceOf(address)', account, ret=amount(value))
        st = self.stpros
        call(st, 'paused()', ret=amount(0))
        call(st, 'oracle()', ret=word(self.oracle))
        call(st, 'unbondingPeriod()', ret=amount(604800))
        call(st, 'maxWithdrawCount()', ret=amount(5))
        for k in ('queuedWithdrawal', 'completedWithdrawal', 'totalCanWithdrawAmount'):
            call(st, f'{k}()', ret=amount(0))
        call(st, 'balanceOf(address)', st, ret=amount(0))
        call(reg['wpros'], 'balanceOf(address)', st, ret=amount(self.supply))
        f.storage[(st, 9)] = amount(self.supply)
        f.balances[st] = 0
        o = self.oracle
        call(o, 'paused()', ret=amount(0))
        if chain == 'mainnet':
            call(o, 'poolInfo(address)', reg['wpros'], ret=data(0, 0))
            safe_state = self.recorded['safe']
            f.storage[(SAFE, 0)] = word(SINGLETON)
            f.code[SINGLETON] = (101, '0x60aa')
            call(SAFE, 'getOwners()', ret=enc(['address[]'], [safe_state['owners']]))
            call(SAFE, 'getThreshold()', ret=amount(3))
            call(SAFE, 'nonce()', ret=amount(18))
            call(SAFE, 'VERSION()', ret=enc(['string'], ['1.3.0']))
            install_modules(f, self.safe_modules, self.safe_form)
        else:
            wpros = reg['wpros']
            call(st, 'slp()', ret=word(self.slp))
            call(st, 'bridgeVault()', ret=word(self.bridge))
            call(st, 'getTotalBalance()', ret=amount(777))
            call(o, 'slp()', ret=word(self.reporter))
            call(o, 'maxUpdateAmount()', ret=amount(10 ** 21))
            call(o, 'updateInterval()', ret=amount(3600))
            call(o, 'commissionAccount()', ret=word(ZERO20))
            call(o, 'commissionRatePpm()', ret=amount(0))
            call(o, 'poolInfo(address)', wpros, ret=data(2 * self.supply + 100, self.supply))
            call(o, 'lastUpdateAt(address)', wpros, ret=amount(ts(3150)))
            call(o, 'tokenToVToken(address)', wpros, ret=word(ZERO20))
            call(o, 'vTokenAddresses(address)', st, ret=amount(1))
            call(st, 'asset()', ret=word(wpros))
            for vtoken, x in self.oracle_vtokens.items():
                call(o, 'vTokenAddresses(address)', vtoken, ret=amount(int(x['flag'])))
                if x['asset'] is not None:
                    call(vtoken, 'asset()', ret=word(x['asset']))
            b = self.bridge
            call(b, 'paused()', ret=amount(0))
            call(b, 'weth()', ret=word(wpros))
            call(b, 'vTokenAddressCount()', ret=amount(1))
            call(b, 'vTokenAddresses(address)', st, ret=amount(1))
            call(b, 'vTokenByAsset(address)', wpros, ret=word(st))
            call(b, 'getBalance(address)', ZERO20, ret=amount(5))
            call(b, 'getBalance(address)', wpros, ret=amount(6))
            call(wpros, 'balanceOf(address)', b, ret=amount(6))
            s = self.slp
            call(s, 'keeper()', ret=word(KEEPER))
            call(s, 'getSlps()', ret=enc(['address[]'], [[self.reporter]]))
            call(s, 'slpCount()', ret=amount(1))

    def reviewed(self, world):
        """The reviewed bundle with the synthetic beacon, vault-proxy and Safe code identities (SIMULATION ONLY)."""
        r = copy.deepcopy(so.reviewed_sources(self.chain))
        for factory in world.factories:
            beacon, vault = world.beacons[factory], world.vaults[factory]
            r['state']['beacons'][beacon]['beacon_code']['keccak256'] = keccak256(bytes.fromhex(('60be' + beacon[2:])))
            r['vaultRuntimes'][vault]['runtimeKeccak'] = keccak256(bytes.fromhex('60fa' + vault[2:]))
        if self.chain == 'mainnet':
            r['state']['safe']['code']['keccak256'] = keccak256(bytes.fromhex('6080'))
        return r


def build(root, chain, model=None, **kw):
    """Collects a full synthetic run (history, headers, both observations) and verifies it offline."""
    model = model or Protocol(chain)
    world = tc.World(root, chain, protocol=model, **kw)
    model.install(world)
    reviewed = model.reviewed(world)
    runtimes = reviewed['vaultRuntimes']
    with world.patch():
        summary, gate, spec = tc.collect(world, observe=observe.read, reviewed_state=reviewed, reviewed_runtimes=runtimes)
        result = observe.verify_run(world.root, 'run-b', spec, reviewed=reviewed, reviewed_runtimes=runtimes)
    return {'world': world, 'model': model, 'reviewed': reviewed, 'summary': summary, 'gate': gate, 'spec': spec,
            'result': result}


class Case(unittest.TestCase):
    """One collected synthetic run per chain, shared by the negative controls (each mutates a deep copy)."""

    built = {}

    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        root = Path(cls._tmp.name).resolve()
        cls.built = {chain: build(root / chain, chain) for chain in ('testnet', 'mainnet')}
        cls.inputs = {}
        for chain, b in cls.built.items():
            w = b['world']
            with w.patch():
                content, verified, headers = col.content_verify(w.root, 'run-b', b['spec'])
            raw = (w.root / 'run-b/observation.json').read_bytes()
            cls.inputs[chain] = {'verified': verified, 'derived': hi.derive(verified, chain), 'headers': headers,
                                 'v1': json.loads(raw), 'v1sha': hashlib.sha256(raw).hexdigest(),
                                 'doc': json.loads((w.root / 'run-b/state-observation.json').read_bytes())}

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def check(self, chain, mutate=None, rows=None, headers=None):
        i = self.inputs[chain]
        doc = copy.deepcopy(i['doc'])
        if mutate:
            mutate(doc)
        verified = i['verified'] if rows is None else {**i['verified'], 'rows': sorted(
            [*i['verified']['rows'], *rows], key=lambda r: (int(r['blockNumber'], 16), int(r['logIndex'], 16)))}
        derived = i['derived'] if rows is None else hi.derive(verified, chain)
        report, _ = so.check(verified, derived, doc, chain, observation_v1=i['v1'], observation_v1_sha256=i['v1sha'],
                             headers=headers or i['headers'], reviewed=self.built[chain]['reviewed'])
        return report

    def section(self, report, name):
        return report['sections'][name]

    def rules(self, report, name):
        return {(x['severity'], x['rule']) for x in report['sections'][name]['issues']}

    def refused(self, chain, mutate, code, status='REFUSED'):
        try:
            self.check(chain, mutate)
            error = None
        except Exception as caught:      # noqa: BLE001 - the class of the refusal is part of the assertion
            error = caught
        self.assertIsInstance(error, HistoryRefused, repr(error))
        self.assertEqual((error.code, error.status), (code, status), str(error))

    def vault(self, chain, settled=True):
        w = self.built[chain]['world']
        return w.vaults[w.factories[0 if settled else -1]]


# ------------------------------------------------------------------------------------------------ D. integration
class IntegrationTest(Case):
    def test_pipeline_both_chains(self):
        for chain, b in self.built.items():
            report = b['result']['state']
            self.assertEqual(b['result']['content']['status'], 'CONTENT_VERIFIED')
            self.assertFalse(b['result']['content']['provenance']['authenticated'])
            self.assertEqual(report['overall'], 'INCOMPLETE', json.dumps(report, indent=1)[:3000])
            self.assertTrue(so.local_exit(report))
            statuses = {k: v['status'] for k, v in report['sections'].items()}
            self.assertNotIn('FAIL', statuses.values(), statuses)
            self.assertNotIn('DRIFT_EVIDENCE', statuses.values(), statuses)
            text = json.dumps(report)
            for forbidden in so.FORBIDDEN_STATUSES:
                self.assertNotIn(forbidden, text)
            self.assertEqual(b['gate'].ledger.used, b['world'].fake.posts)
            vault = statuses[f'vaults.{self.vault(chain)}']
            if chain == 'testnet':
                self.assertEqual(vault, 'INCOMPLETE')
                self.assertIn(('INCOMPLETE', 'EVENTLESS_SETTER_EXPOSURE'), self.rules(report, f'vaults.{self.vault(chain)}'))
                self.assertEqual(statuses['bridgeVault'], 'PENDING_INDEPENDENT_REREAD')
                self.assertEqual(b['summary']['inventory'], 'BOUND')
            else:
                self.assertEqual(vault, 'PENDING_INDEPENDENT_REREAD')
                self.assertEqual(statuses['safe'], 'INCOMPLETE')
                self.assertIn(('INCOMPLETE', 'FR-X8'), self.rules(report, 'safe'))
            self.assertEqual(statuses['oracle'], 'INCOMPLETE' if chain == 'testnet' else 'PROVISIONAL_RECONCILED')
            self.assertEqual(statuses['stpros'], 'PENDING_INDEPENDENT_REREAD')
            for name, status in statuses.items():
                if name.startswith('factories.'):
                    self.assertEqual(status, 'INCOMPLETE')
                    self.assertIn(('INCOMPLETE', 'CR-D7'), self.rules(report, name))
                if name.startswith(('proxies.', 'beacons.')) or name == 'governance':
                    self.assertEqual(status, 'PROVISIONAL_RECONCILED', name)
            # the derived Vault values are offline computations, never observation inputs
            settled = report['sections'][f'vaults.{self.vault(chain)}']
            self.assertIn('termsHash', settled)
            self.assertEqual(settled['debugSetterExposure'], chain == 'testnet')

    def test_campaign_input_keeps_e_fresh_incomplete(self):
        b = self.built['testnet']
        w = b['world']
        export = Path(self._tmp.name) / 'export'
        shutil.rmtree(export, ignore_errors=True)
        (export / 'tools/ci').mkdir(parents=True)
        (export / 'tools/ci/r4-reference.json').write_text('{}\n')
        ref = tc.hashlib.sha256((export / 'tools/ci/r4-reference.json').read_bytes()).hexdigest()
        (export / 'cache/native-r4').mkdir(parents=True)
        (export / 'cache/native-r4/manifest.json').write_text(json.dumps({'referenceSha256': ref}))
        (export / 'snapshots/deployed').mkdir(parents=True)
        (export / 'snapshots/deployed/pins.json').write_text(json.dumps({'mainnet': 1, 'testnet': tc.PIN}))
        (export / 'EXPORT-MANIFEST.txt').write_text(json.dumps({'sourceHead': 'a' * 40}))
        cache = export / ci.INPUT_ROOT
        shutil.copytree(w.root, cache / 'history')
        (cache / 'inputs').mkdir(parents=True)
        shutil.copy(cache / 'history/run-b/inventory.json', cache / 'inputs/inventory.json')
        manifest = cache / 'history/run-b/manifest.json'
        digest = '0x' + hashlib.sha256(manifest.read_bytes()).hexdigest()
        shutil.copy(manifest, cache / 'history' / f'{digest}.json')
        inventory_sha = '0x' + hashlib.sha256((cache / 'inputs/inventory.json').read_bytes()).hexdigest()
        self.assertEqual(json.loads(manifest.read_bytes())['inventorySha256'], inventory_sha)
        actors = {}
        for i, role in enumerate(ci.REQUIRED_ACTORS['testnet']):
            real = ci.REAL_IDENTITIES['testnet'].get(role)
            actors[role] = {'address': real or '0x' + format(0xc0 + i, '040x'),
                            'kind': 'REAL' if real else 'LABELLED_SYNTHETIC', 'label': role}
        doc = {'schema': ci.SCHEMA, 'campaign': 'T', 'chain': 'testnet', 'chainId': 688689, 'evidenceClass': 'E_FRESH',
               'fork': {'block': tc.ANCHOR, 'blockHash': w.fake.hash(tc.ANCHOR)},
               'source': {'productionHead': 'a' * 40,
                          'exportManifestSha256': ci.sha256_file(export / 'EXPORT-MANIFEST.txt'),
                          'r4ReferenceSha256': ci.sha256_file(export / 'tools/ci/r4-reference.json'),
                          'nativeR4ManifestSha256': ci.sha256_file(export / 'cache/native-r4/manifest.json')},
               'inventory': {'path': 'inputs/inventory.json', 'sha256': inventory_sha, 'anchorBlock': tc.ANCHOR,
                             'complete': True, 'historyManifestSha256': digest},
               'actors': actors, 'safe': {'mode': 'ABSENT', 'identityManifestSha256': ci.ZERO_HASH},
               'decisions': {k: 'UNDECIDED' for k in ci.DECISIONS}}
        path = Path(self._tmp.name) / 'input.json'
        path.write_text(json.dumps(doc))
        with w.patch():
            _, _, blockers = ci.load(path, export)
        self.assertIn(('INCOMPLETE', ci.STATE_OBSERVATION_BLOCKER), blockers)
        self.assertIn(('INCOMPLETE', ci.HISTORY_CONTENT_ONLY), blockers)
        self.assertIn(('INCOMPLETE', 'final FS acceptance gate not implemented (fail-closed)'), blockers)
        self.assertEqual(ci.APPROVED_SAFE_IDENTITY_MANIFESTS, frozenset())
        # E_HIST and SYNTHETIC inputs do not get the E_FRESH state blocker (their own blockers are unchanged)
        hist = copy.deepcopy(doc)
        hist['evidenceClass'] = 'E_HIST'
        self.assertNotIn(('INCOMPLETE', ci.STATE_OBSERVATION_BLOCKER), [tuple(x) for x in _validate_only(hist)])


def _validate_only(doc):
    try:
        return ci.validate(doc, {'mainnet': 1, 'testnet': tc.PIN})
    except ci.InputRefused:
        return []


# ------------------------------------------------------------------------------------------------ F. profiles
class ProfileTest(Case):
    def test_profiles_come_from_recorded_runtimes(self):
        for chain, expected in (('mainnet', {'Oracle_V1', 'StPROS_V1', 'YieldVaultFactory_V1', 'YieldVault_V1'}),
                                ('testnet', {'Oracle_V3', 'StPROS_V3', 'YieldVaultFactory_V3', 'YieldVault_V3',
                                             'BridgeVault_V3', 'SlpDistributor_V3'})):
            self.assertEqual(set(self.inputs[chain]['doc']['profiles'].values()), expected)
        reviewed = so.reviewed_sources('testnet')
        manifest = json.loads((FIXTURES / 'manifest.json').read_text())['fixtures']
        self.assertEqual(so.profile_of(reviewed, manifest['testnet/YieldVault_V3']['runtimeKeccak'], 'YieldVault'),
                         'YieldVault_V3')
        self.assertEqual(so.profile_of(reviewed, manifest['testnet/Oracle_V3']['runtimeKeccak'], 'YieldVault'),
                         so.UNKNOWN)
        self.assertEqual(so.profile_of(reviewed, manifest['mainnet/YieldVault_V1']['runtimeKeccak'], 'YieldVault'),
                         so.UNKNOWN)          # the mainnet runtime is not a testnet profile

    def test_recorded_storage_layout_matches_the_slots_used(self):
        for chain in ('mainnet', 'testnet'):
            layout = json.loads((ROOT / 'deployments' / chain / 'YieldVault.json').read_text())['storageLayout']['storage']
            slots = {s['label']: (int(s['slot']), s['offset']) for s in layout}
            self.assertEqual(slots['_phase'], (so.SLOT_PHASE, 0))
            self.assertEqual(slots['_lockedAt'], (so.SLOT_LOCKED_AT, 0))
            self.assertEqual(max(int(s['slot']) for s in layout), 21)
            stpros = json.loads((ROOT / 'deployments' / chain / 'StPROS_Implementation.json').read_text())
            self.assertEqual({s['label']: int(s['slot']) for s in stpros['storageLayout']['storage']}['_tracked'], 9)
            oracle = json.loads((ROOT / 'deployments' / chain / 'Oracle_Implementation.json').read_text())
            labels = [s['label'] for s in oracle['storageLayout']['storage']]
            self.assertEqual(labels[0], 'poolInfo')
            self.assertEqual(len(labels), 1 if chain == 'mainnet' else 9)
        abi_v1 = {a.get('name') for a in json.loads((ROOT / 'deployments/mainnet/YieldVault.json').read_text())['abi']}
        abi_v3 = {a.get('name') for a in json.loads((ROOT / 'deployments/testnet/YieldVault.json').read_text())['abi']}
        self.assertEqual(abi_v3 - abi_v1, {'setLockDuration', 'setMaxLossBps', 'setOpenPeriodRemaining', 'setPhase',
                                           'setRoundCap', 'PhaseChanged'})
        for chain in ('mainnet', 'testnet'):
            for name in ('Oracle_Implementation', 'StPROS_Implementation', 'YieldVaultFactory_Implementation'):
                items = json.loads((ROOT / 'deployments' / chain / f'{name}.json').read_text())['abi']
                self.assertNotIn('pendingOwner', {a.get('name') for a in items})

    def test_no_pending_owner_call_on_a_legacy_profile(self):
        for chain in ('mainnet', 'testnet'):
            selector = abi.calldata('pendingOwner()')
            bodies = self.built[chain]['world'].fake.bodies
            items = [i for b in bodies for i in (b if isinstance(b, list) else [b])]
            self.assertFalse(any(i['method'] == 'eth_call' and i['params'][0]['data'] == selector for i in items))
        self.refused('testnet', lambda d: d['stpros'].update(pendingOwner=ZERO20), 'STATE_KEYS')


def plan_inputs(chain, vtokens=()):
    reg = tc.REG[chain]
    owners = {a: E1 for a in [*reg['expected_proxies'], reg['proxy_admin']]}
    derived = {'holders': {}, 'factories': {f: [] for f in reg['factories']}, 'factoryBeacons': {}, 'owners': owners,
               'implementations': {}, 'admins': {}, 'withdrawOwners': [], 'pendingOwners': {SAFE: ZERO20},
               'roles': {}, 'vaultCreation': [], '_whitelist': {}, '_bridgeVTokens': [],
               '_oracleTokens': [reg['wpros']], '_oracleVTokens': sorted([reg['stpros'], *vtokens])}
    version = 'V1' if chain == 'mainnet' else 'V3'
    prof = {a: f'{x["name"].replace("_test", "")}_{version}' for a, x in reg['expected_proxies'].items()}
    return derived, prof


class ReadPlanTest(unittest.TestCase):
    """The read plans never contain pendingOwner(): it is absent from every recorded legacy ABI."""

    def test_no_pending_owner_selector_in_any_plan(self):
        selector = abi.calldata('pendingOwner()')
        for chain in ('mainnet', 'testnet'):
            derived, prof = plan_inputs(chain)
            plans = [observe.plan_v1(derived, chain, 10), observe.plan_state(derived, chain, 10, prof, {})]
            items = [item for plan in plans for item in plan.items]
            self.assertTrue(items)
            self.assertFalse([k for k, m, p in items if m == 'eth_call' and p[0]['data'] == selector])
            self.assertTrue([k for k, m, p in items if m == 'eth_getStorageAt' and int(p[1], 16) == so.OWNABLE2STEP])

    def test_asset_reads_only_for_registered_vtokens(self):
        reg = tc.REG['testnet']
        disabled = '0x' + '7d' * 20
        derived, prof = plan_inputs('testnet', [disabled])

        def reads(plan, signature):
            return sorted(p[0]['to'] for _, m, p in plan.items if m == 'eth_call' and p[0]['data'][:10] ==
                          abi.calldata(signature, *([ZERO20] if '(address)' in signature else []))[:10])
        first = observe.plan_state(derived, 'testnet', 10, prof, {})
        self.assertEqual(reads(first, 'asset()'), [])                    # no asset() before the flags are read
        flags = [p[0]['data'] for _, m, p in first.items if m == 'eth_call' and p[0]['to'] == next(
            a for a, x in reg['expected_proxies'].items() if x['name'] == 'Oracle')]
        for vtoken in derived['_oracleVTokens']:                         # every history-derived vToken's flag
            self.assertIn(abi.calldata('vTokenAddresses(address)', vtoken), flags)
        second = observe.plan_state(derived, 'testnet', 10, prof, {}, registered={reg['stpros']})
        self.assertEqual(reads(second, 'asset()'), [reg['stpros']])
        with self.assertRaises(tp.CollectionStop) as ctx:
            observe.plan_state(derived, 'testnet', 10, prof, {}, registered={'0x' + '99' * 20})
        self.assertEqual((ctx.exception.code, ctx.exception.status), ('OBSERVATION_PLAN', 'REFUSED'))


# ------------------------------------------------------------------------------------------------ G. invalid states
class VaultControlTest(Case):
    def mutate_vault(self, chain, fn, settled=True):
        v = self.vault(chain, settled)
        return lambda d: fn(d['vaults'][v]), f'vaults.{v}'

    def test_v1_round_cap_mismatch_fails_and_v3_eventless_changes_are_drift(self):
        fn, name = self.mutate_vault('mainnet', lambda v: v['terms'].update(roundCap=str(10 ** 23)))
        self.assertIn(('FAIL', 'T1_ROUND_CAP'), self.rules(self.check('mainnet', fn), name))
        for field, value, rule in (('roundCap', str(10 ** 23), 'T1_ROUND_CAP'), ('lockDuration', '0', 'MIN_LOCK_DURATION'),
                                   ('maxLossBps', '20000', 'MAX_LOSS_BPS')):
            fn, name = self.mutate_vault('testnet', lambda v, f=field, x=value: v['terms'].update({f: x}))
            report = self.check('testnet', fn)
            self.assertEqual(self.section(report, name)['status'], 'DRIFT_EVIDENCE', field)
            self.assertIn(('DRIFT', rule), self.rules(report, name))
        # an in-bounds eventless change is not detectable: STATE-ONLY plus the exposure keep it from any PASS
        fn, name = self.mutate_vault('testnet', lambda v: v['terms'].update(lockDuration='51'))
        report = self.check('testnet', fn)
        self.assertEqual(self.section(report, name)['status'], 'INCOMPLETE')
        self.assertIn('terms.lockDuration', self.section(report, name)['stateOnlyFields'])

    def test_phase_rules(self):
        fn, name = self.mutate_vault('testnet', lambda v: v.update(phase=0))     # stored OPEN past the deadline
        self.assertIn(('FAIL', 'P2_EFFECTIVE_PHASE'), self.rules(self.check('testnet', fn), name))
        fn, name = self.mutate_vault('mainnet', lambda v: v['storage'].update(phase=amount(7)))
        self.assertIn(('FAIL', 'STORED_PHASE_RANGE'), self.rules(self.check('mainnet', fn), name))
        fn, name = self.mutate_vault('mainnet', lambda v: v['storage'].update(phase=amount(1)))
        self.assertIn(('FAIL', 'P1_STORED_PHASE'), self.rules(self.check('mainnet', fn), name))
        fn, name = self.mutate_vault('testnet', lambda v: v.update(lockedAt='1'))
        self.assertIn(('FAIL', 'EFFECTIVE_LOCKED_AT'), self.rules(self.check('testnet', fn), name))

    def test_settlement_and_managed_assets(self):
        fn, name = self.mutate_vault('mainnet', lambda v: v['settlement'].update(settleAmount='11'))
        self.assertIn(('FAIL', 'S_settleAmount'), self.rules(self.check('mainnet', fn), name))
        # V1 stored OPEN/LOCKED with a proposal timestamp: FAIL; SETTLED after finalize keeps it (no FAIL)
        fn, name = self.mutate_vault('mainnet', lambda v: v['settlement'].update(settleProposedAt='5'), settled=False)
        rules = self.rules(self.check('mainnet', fn), name)
        self.assertIn(('FAIL', 'S2_CLEARED_PROPOSAL'), rules)
        settled = self.inputs['mainnet']['doc']['vaults'][self.vault('mainnet')]['settlement']['settleProposedAt']
        self.assertNotEqual(settled, '0')
        self.assertNotIn('FAIL', {x['severity'] for x in self.check('mainnet')['sections'][f'vaults.{self.vault("mainnet")}']['issues']})
        fn, name = self.mutate_vault('mainnet', lambda v: v.update(totalManagedAssets='110', totalAssets='110'))
        self.assertIn(('FAIL', 'A1_MANAGED_ASSETS'), self.rules(self.check('mainnet', fn), name))
        fn, name = self.mutate_vault('mainnet', lambda v: v.update(assetBalance='100'))
        rules = self.rules(self.check('mainnet', fn), name)
        self.assertIn(('FAIL', 'B1_BACKING'), rules)
        self.assertIn(('FAIL', 'ASSET_BALANCE_REPLAY'), rules)
        # funded profit in escrow: the backing bound is managed assets plus the escrowed proposal
        proposed = lambda bal: lambda v: (v['storage'].update(phase=amount(2)), v.update(assetBalance=str(bal)))  # noqa: E731
        fn, name = self.mutate_vault('mainnet', proposed(118))
        self.assertIn(('FAIL', 'B1_BACKING'), self.rules(self.check('mainnet', fn), name))
        fn, name = self.mutate_vault('mainnet', proposed(119))
        self.assertNotIn(('FAIL', 'B1_BACKING'), self.rules(self.check('mainnet', fn), name))
        # LOSS finalization lowers managed assets by the amount (no LOSS round exists in the synthetic history)
        vault = self.vault('mainnet')
        loss = tc.FakeChain('mainnet').row(vault, [E['SettlementFinalized'], word('0x' + '0' * 39 + '1'),
                                                   word('0x' + '0' * 39 + '1')], 2900, 0, data(5, 0, 0, ts(2900)))
        managed = lambda n: lambda d: d['vaults'][vault].update(totalManagedAssets=str(n), totalAssets=str(n))  # noqa: E731
        rules = self.rules(self.check('mainnet', managed(104), rows=[loss]), f'vaults.{vault}')
        self.assertNotIn(('FAIL', 'A1_MANAGED_ASSETS'), rules)
        rules = self.rules(self.check('mainnet', managed(114), rows=[loss]), f'vaults.{vault}')
        self.assertIn(('FAIL', 'A1_MANAGED_ASSETS'), rules)
        fn, name = self.mutate_vault('mainnet', lambda v: v['storage'].update(retired=amount(1)))
        self.assertIn(('FAIL', 'R4_RETIRED_SLOT'), self.rules(self.check('mainnet', fn), name))
        fn, name = self.mutate_vault('mainnet', lambda v: v['roleAdmins'].update({ADMIN: '0x' + '11' * 32}))
        self.assertTrue(any(r[0] == 'FAIL' and r[1].startswith('ROLE_ADMIN') for r in self.rules(self.check('mainnet', fn), name)))

    def test_unknown_runtime_is_never_interpreted(self):
        chain = 'testnet'
        v = self.vault(chain)
        b = self.built[chain]['world'].beacons[self.built[chain]['world'].factories[0]]

        def unknown(d):
            d['beacons'][b]['implementationRuntimeKeccak'] = '0x' + '99' * 32
            d['vaults'][v] = {'profile': so.UNKNOWN, 'runtimeKeccak': d['vaults'][v]['runtimeKeccak']}
            d['profiles'][v] = so.UNKNOWN
        report = self.check(chain, unknown)
        self.assertEqual(self.section(report, f'vaults.{v}')['status'], 'INCOMPLETE')
        self.assertIn(('INCOMPLETE', so.UNKNOWN), self.rules(report, f'vaults.{v}'))

        def guessed(d):
            unknown(d)
            d['vaults'][v]['terms'] = {}
        self.refused(chain, guessed, 'STATE_KEYS')

        def mismatched(d):
            d['beacons'][b]['implementationRuntimeKeccak'] = '0x' + '99' * 32
        self.refused(chain, mismatched, 'PROFILE_MISMATCH')


class OracleControlTest(Case):
    def test_oracle_v1_rules(self):
        reg = tc.REG['mainnet']
        oracle = next(a for a, x in reg['expected_proxies'].items() if x['name'] == 'Oracle')
        fake = tc.FakeChain('mainnet')
        row = fake.row(oracle, [E['SetTokenAmount']], 3400, 0, '0x' + '00' * 12 + reg['wpros'][2:] + data(1, 1)[2:])
        self.assertIn(('FAIL', 'V1_SET_TOKEN_AMOUNT'), self.rules(self.check('mainnet', rows=[row]), 'oracle'))
        report = self.check('mainnet', lambda d: d['oracle']['pools'].update({reg['wpros']: ['1', '0']}))
        self.assertIn(('FAIL', 'V1_POOL_ZERO'), self.rules(report, 'oracle'))
        report = self.check('mainnet', lambda d: d['oracle']['storage'].update(slot1=amount(5)))
        self.assertIn(('FAIL', 'R4_SLOT slot1'), self.rules(report, 'oracle'))
        # the mainnet V1 backing relation uses the observed pool
        report = self.check('mainnet', lambda d: d['oracle']['pools'].update({reg['wpros']: ['1', '0']}))
        self.assertIn(('FAIL', 'V1_LIVE_BACKING'), self.rules(report, 'stpros'))

    def test_oracle_v3_replay_and_policy(self):
        reg = tc.REG['testnet']
        wpros = reg['wpros']
        report = self.check('testnet', lambda d: d['oracle']['pools'].update({wpros: [str(2 * 1010000 + 101), '1010000']}))
        self.assertIn(('FAIL', f'O1_POOL_TOKEN {wpros}'), self.rules(report, 'oracle'))
        oracle = next(a for a, x in reg['expected_proxies'].items() if x['name'] == 'Oracle')
        fake = tc.FakeChain('testnet')
        supply = 1010000
        big = fake.row(oracle, [E['TokenAmountUpdated'], word(wpros)], 3160, 0, data(10 ** 22, 2 * supply + 100 + 10 ** 22))
        headers = {'timestamps': {**self.inputs['testnet']['headers']['timestamps'], 3160: ts(3160)}}
        mutate = lambda d: d['oracle']['pools'].update({wpros: [str(2 * supply + 100 + 10 ** 22), str(supply)]})  # noqa: E731
        report = self.check('testnet', lambda d: (mutate(d), d['oracle']['lastUpdateAt'].update({wpros: str(ts(3160))})),
                            rows=[big], headers=headers)
        rules = self.rules(report, 'oracle')
        self.assertIn(('FAIL', 'MAX_UPDATE_AMOUNT'), rules)
        self.assertIn(('FAIL', 'UPDATE_INTERVAL'), rules)
        chain_break = fake.row(oracle, [E['TokenAmountUpdated'], word(wpros)], 4300, 1, data(5, 7))
        report = self.check('testnet', rows=[chain_break],
                            headers={'timestamps': {**headers['timestamps'], 4300: ts(4300)}})
        self.assertIn(('FAIL', 'TOKEN_AMOUNT_CHAIN'), self.rules(report, 'oracle'))
        # a missing header timestamp is INCOMPLETE, never a pass of the interval rule
        report = self.check('testnet', rows=[big], headers={'timestamps': {}},
                            mutate=lambda d: (mutate(d), d['oracle']['lastUpdateAt'].update({wpros: str(ts(3160))})))
        self.assertIn(('INCOMPLETE', 'UPDATE_TIMESTAMP_UNKNOWN'), self.rules(report, 'oracle'))

    def test_commission_is_attributed_to_its_own_update(self):
        token = '0x' + 'a4' * 20
        # two commissioned updates of one token in one transaction (reachable with updateInterval == 0)
        two = [[('commission', 10, 4), ('update', 100, 1100), ('commission', 5, 1), ('update', 50, 1150)]]
        self.assertFalse(fails(replay(token, *oracle_rows(token, two), (1150, 405))))
        self.assertIn(('FAIL', 'O1_POOL_VTOKEN'), replay(token, *oracle_rows(token, two), (1150, 410)))   # sum added twice
        self.assertIn(('FAIL', 'O1_POOL_VTOKEN'), replay(token, *oracle_rows(token, two), (1150, 404)))
        # a commissioned and a zero-commission update in one transaction
        mixed = [[('commission', 10, 4), ('update', 100, 1100), ('update', 9, 1109)]]
        self.assertFalse(fails(replay(token, *oracle_rows(token, mixed), (1109, 404))))
        self.assertIn(('FAIL', 'O1_POOL_VTOKEN'), replay(token, *oracle_rows(token, mixed), (1109, 408)))
        # one update per transaction, with and without commission
        apart = [[('commission', 10, 4), ('update', 100, 1100)], [('update', 9, 1109)], [('commission', 5, 1), ('update', 50, 1159)]]
        self.assertFalse(fails(replay(token, *oracle_rows(token, apart), (1159, 405))))
        self.assertIn(('FAIL', 'O1_POOL_VTOKEN'), replay(token, *oracle_rows(token, apart), (1159, 406)))
        # a commission event without its own following update is never attributed
        for bad in ([[('commission', 10, 4)]],
                    [[('commission', 10, 4)], [('update', 100, 1100)]],
                    [[('commission', 10, 4), ('commission', 5, 1), ('update', 100, 1100)]]):
            self.assertIn(('FAIL', 'COMMISSION_ATTRIBUTION'), replay(token, *oracle_rows(token, bad), (1100, 400)))
        rows, stamps = oracle_rows(token, [[('commission', 10, 4), ('update', 100, 1100)]])
        rows[-1]['topics'] = [E['TokenAmountUpdated'], word('0x' + 'a5' * 20)]           # another token's update
        self.assertIn(('FAIL', 'COMMISSION_ATTRIBUTION'), replay(token, rows, stamps, (1000, 400)))
        rows, stamps = oracle_rows(token, [[('commission', 10, 4), ('update', 100, 1100)]])
        rows[-2]['topics'] = rows[-2]['topics'][:3]                                       # not the reviewed ABI
        self.assertIn(('FAIL', 'COMMISSION_EVENT_SHAPE'), replay(token, rows, stamps, (1100, 404)))
        # the interval and limit checks still apply to repeated updates
        rows, stamps = oracle_rows(token, two, interval=60)
        self.assertIn(('FAIL', 'UPDATE_INTERVAL'), replay(token, rows, stamps, (1150, 405)))
        rows, stamps = oracle_rows(token, two, limit=60)
        self.assertIn(('FAIL', 'MAX_UPDATE_AMOUNT'), replay(token, rows, stamps, (1150, 405)))

    def test_unexplained_registered_vtoken_and_universe_residual(self):
        reg = tc.REG['testnet']
        oracle = next(a for a, x in reg['expected_proxies'].items() if x['name'] == 'Oracle')
        other = '0x' + '77' * 20
        row = tc.FakeChain('testnet').row(oracle, [E['OracleVTokenSet'], word(other)], 2000, 0, data(1))

        def registered(d):
            d['oracle']['vTokenAddresses'][other] = True
            d['oracle']['vTokenAsset'][other] = reg['wpros']
        report = self.check('testnet', registered, rows=[row])
        self.assertIn(('INCOMPLETE', 'UNEXPLAINED_VTOKEN'), self.rules(report, 'oracle'))
        self.assertIn(('INCOMPLETE', 'CR-D7'), self.rules(self.check('testnet'), 'oracle'))
        # a vToken missing from the observation is INCOMPLETE, not "not registered"
        with self.assertRaises(HistoryRefused) as ctx:
            self.check('testnet', rows=[row])
        self.assertEqual(ctx.exception.status, 'INCOMPLETE')


class CustodyGovernanceControlTest(Case):
    def test_custody_and_bridge(self):
        report = self.check('testnet', lambda d: d['bridgeVault'].update(vTokenAddressCount='2'))
        self.assertIn(('FAIL', 'VTOKEN_COUNT_COMPLETENESS'), self.rules(report, 'bridgeVault'))
        report = self.check('mainnet', lambda d: d['stpros'].update(wrapped='1'))
        self.assertIn(('FAIL', 'WRAPPED_WPROS_BALANCE'), self.rules(report, 'stpros'))
        report = self.check('mainnet', lambda d: d['stpros']['storage'].update(tracked=amount(1)))
        self.assertIn(('FAIL', 'INV1_TRACKED_SUPPLY'), self.rules(report, 'stpros'))
        report = self.check('testnet', lambda d: d['stpros'].update(totalCanWithdrawAmount='1'))
        self.assertIn(('FAIL', 'QUEUE_V1_OBSERVATION'), self.rules(report, 'stpros'))
        report = self.check('mainnet', lambda d: d['stpros'].update(nativeBalance=so.NOT_OBSERVED))
        self.assertEqual(self.section(report, 'stpros')['status'], 'NOT_OBSERVED')
        self.refused('mainnet', lambda d: d['stpros'].update(nativeBalance=None), 'OBSERVATION_VALUE')

    def test_pending_owner_slot_and_governance(self):
        for chain, section, path in (('mainnet', 'stpros', ('stpros', 'storage')), ('testnet', 'bridgeVault', ('bridgeVault', 'storage')),
                                     ('testnet', 'slpDistributor', ('slpDistributor', 'storage'))):
            report = self.check(chain, lambda d, p=path: d[p[0]][p[1]].update(ownable2StepPending=word('0x' + '33' * 20)))
            self.assertIn(('FAIL', 'OWNABLE2STEP_SLOT'), self.rules(report, section))
        factory = tc.REG['mainnet']['factories'][0]
        report = self.check('mainnet', lambda d: d['factories'][factory]['storage'].update(ownable2StepPending=amount(1)))
        self.assertIn(('FAIL', 'OWNABLE2STEP_SLOT'), self.rules(report, f'factories.{factory}'))
        report = self.check('mainnet', lambda d: d['governance']['proxyAdmin'].update(owner=E1))
        self.assertIn(('FAIL', 'PROXY_ADMIN_OWNER_REPLAY'), self.rules(report, 'governance'))
        report = self.check('mainnet', lambda d: d['governance']['accountCodeKeccak'].update({E1: '0x' + '12' * 32}))
        self.assertIn(('INCOMPLETE', 'INCOMPLETE_SAFE_SIGNER_CONTROL'), self.rules(report, 'governance'))
        beacon = self.built['mainnet']['world'].beacons[factory]
        report = self.check('mainnet', lambda d: d['factories'][factory].update(beacon='0x' + '44' * 20))
        self.assertIn(('FAIL', 'BEACON_DRIFT'), self.rules(report, f'factories.{factory}'))
        report = self.check('mainnet', lambda d: d['beacons'][beacon].update(runtimeKeccak='0x' + '45' * 32))
        self.assertIn(('FAIL', 'BEACON_RUNTIME'), self.rules(report, f'beacons.{beacon}'))
        proxy = factory
        report = self.check('mainnet', lambda d: d['proxies'][proxy].update(implementation='0x' + '46' * 20))
        self.assertIn(('FAIL', 'IMPLEMENTATION_REPLAY'), self.rules(report, f'proxies.{proxy}'))

    def test_safe_candidate(self):
        report = self.check('mainnet', lambda d: d['safe'].update(runtimeKeccak='0x' + '47' * 32))
        self.assertIn(('FAIL', 'SAFE_PROXY_RUNTIME'), self.rules(report, 'safe'))
        self.refused('mainnet', lambda d: d['safe'].update(expectedSingleton=SINGLETON), 'SELF_EXPECTATION')
        self.refused('mainnet', lambda d: d['safe']['scanStart'].update(label='creationBlock'), 'STATE_VALUE')

        def nested(d):
            d['governance']['accountCodeKeccak'] = {**d['governance']['accountCodeKeccak']}
            d['safe']['setupEvents']['expectedCount'] = 1
        self.refused('mainnet', nested, 'SELF_EXPECTATION')
        report = self.check('mainnet', lambda d: d['safe']['scanStart'].update(codeBefore='0x60'))
        self.assertIn(('FAIL', 'CODE_BEFORE_FROM_BLOCK'), self.rules(report, 'safe'))
        self.assertEqual(ci.APPROVED_SAFE_IDENTITY_MANIFESTS, frozenset())
        candidate = so.safe_candidate(copy.deepcopy(self.inputs['mainnet']['doc']))
        self.assertEqual(candidate['status'], 'CANDIDATE / UNAPPROVED (FR-X8)')
        self.assertTrue(all(f['label'] == 'OBSERVED' for f in candidate['facts'].values()))
        self.assertNotIn('expected', json.dumps(candidate).lower())
        bad = copy.deepcopy(self.inputs['mainnet']['doc'])
        bad['safe']['modules'] = [{'expectedModule': SINGLETON}]
        with self.assertRaises(HistoryRefused) as ctx:
            so.safe_candidate(bad)
        self.assertEqual(ctx.exception.code, 'SELF_EXPECTATION')


class SafeModuleControlTest(Case):
    """The checker over recorded module pages (mainnet document, SIMULATION ONLY module lists)."""

    def with_pages(self, pages, modules=None):
        def mutate(d):
            d['safe']['modulePagination']['pages'] = copy.deepcopy(pages)
            d['safe']['modules'] = [m for p in pages for m in p['modules']] if modules is None else list(modules)
        return mutate

    def mutated(self, mutate):
        doc = copy.deepcopy(self.inputs['mainnet']['doc'])
        mutate(doc)
        return doc

    def test_complete_enumerations(self):
        for n in (0, 3, 10, 11, 25, 100):
            for form in ('1.3.0', '1.4.x'):
                modules = [module(i) for i in range(n)]
                mutate = self.with_pages(safe_pages(modules, form))
                report = self.check('mainnet', mutate)
                safe = self.section(report, 'safe')
                self.assertEqual((safe['moduleEnumeration'], safe['moduleCount']), ('COMPLETE', n), (n, form))
                self.assertFalse([r for r in self.rules(report, 'safe') if r[1].startswith('SAFE_MODULE')], (n, form))
                self.assertEqual(safe['status'], 'INCOMPLETE')                    # FR-X8 is unchanged
                candidate = so.safe_candidate(self.mutated(mutate))
                self.assertEqual(candidate['facts']['modules']['value'], modules)
                self.assertEqual(candidate['status'], 'CANDIDATE / UNAPPROVED (FR-X8)')

    def test_incomplete_enumerations_are_never_a_complete_candidate(self):
        eleven, many = [module(i) for i in range(11)], [module(i) for i in range(101)]
        full = safe_pages([module(i) for i in range(25)], '1.4.x')
        cycle = [full[0], {'start': full[0]['modules'][-1], 'modules': full[0]['modules'], 'next': full[0]['modules'][-1]}]
        mixed = [full[0], {**full[1], 'next': full[2]['modules'][0]}, full[2]]
        neither = [{**full[0], 'next': module(999)}, *full[1:]]
        cases = {
            'first page only (the former reader)': (safe_pages(eleven, '1.3.0')[:1], 'SAFE_MODULES_UNTERMINATED'),
            'page bound': (safe_pages(many, '1.4.x')[:so.MODULE_MAX_PAGES], 'SAFE_MODULE_PAGE_BOUND'),
            'cycle': (cycle, 'SAFE_MODULE_CYCLE'),
            'sentinel listed': ([{'start': so.SENTINEL, 'modules': [module(1), so.SENTINEL], 'next': so.SENTINEL}],
                                'SAFE_MODULE_CYCLE'),
            'short non-terminal page': ([{'start': so.SENTINEL, 'modules': eleven[:3], 'next': eleven[2]}],
                                        'SAFE_MODULE_PAGE_MALFORMED'),
            'empty non-terminal page': ([{'start': so.SENTINEL, 'modules': [], 'next': ZERO20}], 'SAFE_MODULE_PAGE_MALFORMED'),
            'mixed cursor forms': (mixed, 'SAFE_MODULE_PAGE_MALFORMED'),
            'cursor in neither form': (neither, 'SAFE_MODULE_PAGE_MALFORMED'),
            'pages after the terminal sentinel': ([{'start': so.SENTINEL, 'modules': eleven[:3], 'next': so.SENTINEL},
                                                   {'start': eleven[2], 'modules': [], 'next': so.SENTINEL}],
                                                  'SAFE_MODULE_PAGE_MALFORMED'),
        }
        for name, (pages, rule) in cases.items():
            mutate = self.with_pages(pages)
            report = self.check('mainnet', mutate)
            self.assertIn(('INCOMPLETE', rule), self.rules(report, 'safe'), name)
            self.assertEqual(self.section(report, 'safe')['moduleEnumeration'], 'INCOMPLETE', name)
            with self.assertRaises(HistoryRefused) as ctx:
                so.safe_candidate(self.mutated(mutate))
            self.assertEqual((ctx.exception.code, ctx.exception.status), ('SAFE_MODULES_INCOMPLETE', 'INCOMPLETE'), name)

    def test_malformed_module_records_are_refused(self):
        pages = safe_pages([module(i) for i in range(25)], '1.3.0')
        concat = [m for p in pages for m in p['modules']]

        def page(i, **kw):
            return self.with_pages([*pages[:i], {**pages[i], **kw}, *pages[i + 1:]])
        for mutate, code, status in (
                (page(0, start=module(1)), 'STATE_VALUE', 'REFUSED'),
                (page(1, start=pages[0]['modules'][0]), 'STATE_VALUE', 'REFUSED'),
                (self.with_pages(pages, concat[:10]), 'STATE_VALUE', 'REFUSED'),         # truncated to the first page
                (self.with_pages(pages, concat + [module(99)]), 'STATE_VALUE', 'REFUSED'),
                (self.with_pages(pages, list(reversed(concat))), 'STATE_VALUE', 'REFUSED'),
                (page(0, modules=[*pages[0]['modules'], module(98)]), 'STATE_VALUE', 'REFUSED'),
                (page(0, next=pages[0]['next'].upper().replace('0X', '0x')), 'STATE_VALUE', 'REFUSED'),
                (self.with_pages([]), 'STATE_VALUE', 'REFUSED'),
                (self.with_pages(safe_pages([module(i) for i in range(101)], '1.4.x')), 'STATE_VALUE', 'REFUSED'),
                (page(0, extra=1), 'STATE_KEYS', 'REFUSED'),
                (lambda d: d['safe']['modulePagination']['pages'][0].pop('next'), 'STATE_KEYS', 'INCOMPLETE'),
                (lambda d: d['safe']['modulePagination'].update(pageSize=11), 'STATE_VALUE', 'REFUSED'),
                (lambda d: d['safe']['modulePagination'].update(pageSize=10.0), 'STATE_VALUE', 'REFUSED'),
                (lambda d: d['safe']['modulePagination'].update(maxPages=11), 'STATE_VALUE', 'REFUSED'),
                (lambda d: d['safe'].update(modulesNext=so.SENTINEL), 'STATE_KEYS', 'REFUSED'),
                (lambda d: d['safe'].pop('modulePagination'), 'STATE_KEYS', 'INCOMPLETE')):
            self.refused('mainnet', mutate, code, status)


class ModulePaginationTest(tc.Tmp):
    """The module reader through the real gate and budget ledger, against the reference Safe model (SIMULATION ONLY)."""
    BLOCK = tc.ANCHOR

    def read(self, calls, *, ceiling=100_000):
        fake = tc.FakeChain('mainnet')
        fake.calls.update(calls)
        root = self.root / f'g{len(list(self.root.iterdir()))}'
        root.mkdir()
        gate = tc.gate_for(root, 'mainnet', fake, tc.Clock(), ceiling=ceiling)
        gate.ledger.open()
        self.addCleanup(gate.ledger.close)
        lane = tp.Lane(tp.release_rpc(gate), gate)

        async def go():
            first = await lane.call('eth_call', [{'to': SAFE, 'data': abi.calldata(
                observe.MODULES_SIGNATURE, so.SENTINEL, so.MODULE_PAGE)}, hex(self.BLOCK)])
            return await observe.module_pages(lane, SAFE, self.BLOCK, first)
        return fake, go

    def model(self, modules, form):
        fake = tc.FakeChain('mainnet')
        install_modules(fake, modules, form)
        return fake.calls

    def page(self, start, modules, nxt):
        return {(SAFE, abi.calldata(observe.MODULES_SIGNATURE, start, so.MODULE_PAGE)): enc(['address[]', 'address'],
                                                                                         [modules, nxt])}

    def gaps(self, pages):
        return {rule for rule, _ in so.module_enumeration(
            {'modules': [m for p in pages for m in p['modules']],
             'modulePagination': {'pageSize': so.MODULE_PAGE, 'maxPages': so.MODULE_MAX_PAGES, 'pages': pages}})[1]}

    def test_zero_fewer_exactly_and_more_than_one_page(self):
        for n in (0, 1, 9, 10, 11, 19, 20, 21, 35, 100):
            for form in ('1.3.0', '1.4.x'):
                modules = [module(i) for i in range(n)]
                fake, go = self.read(self.model(modules, form))
                pages = tc.run(go())
                self.assertEqual(pages, safe_pages(modules, form), (n, form))
                self.assertEqual([m for p in pages for m in p['modules']], modules, (n, form))
                self.assertEqual(len(pages), max(1, -(-n // so.MODULE_PAGE)), (n, form))
                self.assertEqual(fake.posts, len(pages), (n, form))                 # one budgeted POST per page
                self.assertEqual(pages[-1]['next'], so.SENTINEL)
                self.assertEqual(self.gaps(pages), set(), (n, form))

    def test_page_bound_is_incomplete_and_never_truncated(self):
        modules = [module(i) for i in range(101)]
        fake, go = self.read(self.model(modules, '1.4.x'))
        pages = tc.run(go())
        self.assertEqual((len(pages), fake.posts), (so.MODULE_MAX_PAGES, so.MODULE_MAX_PAGES))
        self.assertEqual([m for p in pages for m in p['modules']], modules[:100])
        self.assertIn('SAFE_MODULE_PAGE_BOUND', self.gaps(pages))

    def test_repeated_cursors_cycles_and_malformed_results(self):
        ten = [module(i) for i in range(10)]
        for name, calls, posts, rule in (
                ('cycle', {**self.page(so.SENTINEL, ten, ten[-1]), **self.page(ten[-1], ten, ten[-1])}, 2, 'SAFE_MODULE_CYCLE'),
                ('repeated cursor', {**self.page(so.SENTINEL, ten, ten[-1]),
                                     **self.page(ten[-1], [ten[-1], *[module(20 + i) for i in range(9)]], module(28))},
                 2, 'SAFE_MODULE_CYCLE'),
                ('sentinel listed', self.page(so.SENTINEL, [ten[0], so.SENTINEL], so.SENTINEL), 1, 'SAFE_MODULE_CYCLE'),
                ('short non-terminal', self.page(so.SENTINEL, ten[:3], ten[2]), 1, 'SAFE_MODULE_PAGE_MALFORMED'),
                ('empty non-terminal', self.page(so.SENTINEL, [], ZERO20), 1, 'SAFE_MODULE_PAGE_MALFORMED')):
            fake, go = self.read(calls)
            pages = tc.run(go())
            self.assertEqual(fake.posts, posts, name)
            self.assertIn(rule, self.gaps(pages), name)
        too_many = [module(i) for i in range(11)]
        for name, calls, code in (
                ('more entries than the page size', self.page(so.SENTINEL, too_many, so.SENTINEL), 'OBSERVATION_DECODE'),
                ('not the ABI shape', {(SAFE, abi.calldata(observe.MODULES_SIGNATURE, so.SENTINEL, so.MODULE_PAGE)): '0x1234'},
                 'OBSERVATION_DECODE'),
                ('empty result', {(SAFE, abi.calldata(observe.MODULES_SIGNATURE, so.SENTINEL, so.MODULE_PAGE)): '0x'},
                 'OBSERVATION_DECODE'),
                ('second page reverts', self.page(so.SENTINEL, ten, ten[-1]), 'RPC_ERROR')):
            fake, go = self.read(calls)
            with self.assertRaises(tp.CollectionStop) as ctx:
                tc.run(go())
            self.assertEqual(ctx.exception.code, code, name)

    def test_cumulative_budget_bounds_the_pages(self):
        fake, go = self.read(self.model([module(i) for i in range(35)], '1.4.x'), ceiling=2)
        with self.assertRaises(tp.BudgetExhausted) as ctx:
            tc.run(go())
        self.assertEqual(ctx.exception.code, 'HTTP_BUDGET_EXHAUSTED')
        self.assertEqual(fake.posts, 2)


class RepairIntegrationTest(unittest.TestCase):
    """Full synthetic runs for the module pages and the Oracle registration universe (SIMULATION ONLY)."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name).resolve()

    def inputs(self, b, chain):
        w = b['world']
        with w.patch():
            _, verified, headers = col.content_verify(w.root, 'run-b', b['spec'])
        raw = (w.root / 'run-b/observation.json').read_bytes()
        doc = json.loads((w.root / 'run-b/state-observation.json').read_bytes())

        def check(mutate):
            d = copy.deepcopy(doc)
            mutate(d)
            return so.check(verified, hi.derive(verified, chain), d, chain, observation_v1=json.loads(raw),
                            observation_v1_sha256=hashlib.sha256(raw).hexdigest(), headers=headers, reviewed=b['reviewed'])
        return doc, check

    def calls_to(self, b, signature):
        bodies = b['world'].fake.bodies
        items = [i for body in bodies for i in (body if isinstance(body, list) else [body])]
        return [i['params'][0] for i in items if i['method'] == 'eth_call' and i['params'][0]['data'][:10] ==
                abi.calldata(signature, *([ZERO20] if '(address' in signature else []), *([0] if 'uint256)' in signature else []))[:10]]

    def test_safe_with_more_than_one_page_of_modules(self):
        modules = [module(i) for i in range(11)]
        b = build(self.root / 'mainnet', 'mainnet', Protocol('mainnet', safe_modules=modules, safe_form='1.3.0'))
        doc, _ = self.inputs(b, 'mainnet')
        self.assertEqual(doc['safe']['modules'], modules)
        self.assertEqual(doc['safe']['modulePagination']['pages'], safe_pages(modules, '1.3.0'))
        starts = [abi.decode_parameters([{'type': 'address'}], '0x' + p['data'][10:74])[0].lower()
                  for p in self.calls_to(b, observe.MODULES_SIGNATURE)]
        self.assertEqual(starts, [so.SENTINEL, modules[9]])
        safe = b['result']['state']['sections']['safe']
        self.assertEqual((safe['moduleEnumeration'], safe['moduleCount']), ('COMPLETE', 11))
        self.assertIn(('INCOMPLETE', 'FR-X8'), {(i['severity'], i['rule']) for i in safe['issues']})
        self.assertEqual(so.safe_candidate(doc)['facts']['modules']['value'], modules)
        self.assertEqual(b['gate'].ledger.used, b['world'].fake.posts)
        self.assertEqual(ci.APPROVED_SAFE_IDENTITY_MANIFESTS, frozenset())

    def test_oracle_asset_reads_follow_the_registration_flags(self):
        stpros = tc.REG['testnet']['stpros']
        disabled, registered, asset = '0x' + '7d' * 20, '0x' + '7e' * 20, '0x' + '7f' * 20
        model = Protocol('testnet', oracle_vtokens={disabled: {'flag': False, 'asset': None},
                                                    registered: {'flag': True, 'asset': asset}})
        b = build(self.root / 'testnet', 'testnet', model)
        doc, check = self.inputs(b, 'testnet')
        oracle = doc['oracle']
        self.assertEqual({k: oracle['vTokenAddresses'][k] for k in (disabled, registered, stpros)},
                         {disabled: False, registered: True, stpros: True})
        self.assertEqual(oracle['vTokenAsset'], {registered: asset, stpros: tc.REG['testnet']['wpros']})
        self.assertEqual(sorted(p['to'] for p in self.calls_to(b, 'asset()') if p['to'] in (disabled, registered, stpros)),
                         sorted([registered, stpros]))                       # never asset() on the disabled address
        report = b['result']['state']
        self.assertNotIn('FAIL', {i['severity'] for i in report['sections']['oracle']['issues']})
        self.assertIn(('INCOMPLETE', 'CR-D7'), {(i['severity'], i['rule']) for i in report['sections']['oracle']['issues']})
        for mutate, status in ((lambda d: d['oracle']['vTokenAsset'].update({disabled: asset}), 'REFUSED'),
                               (lambda d: d['oracle']['vTokenAsset'].update({'0x' + '7b' * 20: asset}), 'REFUSED'),
                               (lambda d: d['oracle']['vTokenAsset'].pop(registered), 'INCOMPLETE'),
                               (lambda d: d['oracle']['vTokenAddresses'].update({disabled: True}), 'INCOMPLETE')):
            with self.assertRaises(HistoryRefused) as ctx:
                check(mutate)
            self.assertEqual((ctx.exception.code, ctx.exception.status), ('STATE_UNIVERSE', status))

    def test_registered_vtoken_without_an_asset_getter_stops(self):
        missing = '0x' + '7c' * 20
        model = Protocol('testnet', oracle_vtokens={missing: {'flag': True, 'asset': None}})
        with self.assertRaises(tp.CollectionStop) as ctx:
            build(self.root / 'testnet', 'testnet', model)
        self.assertEqual((ctx.exception.code, ctx.exception.status), ('RPC_ERROR', 'STOP'))


def oracle_rows(token, steps, *, limit=10 ** 6, interval=0, base=(1000, 400), block=3400):
    """Oracle_V3 rows in emission order (reviewed event ABI): UpdateLimitsChanged and SetTokenAmount, then one
    transaction per step, each a list of ('commission', tokenAmount, vTokenAmount) / ('update', added, newTotal)."""
    fake, oracle, vtoken = tc.FakeChain('testnet'), '0x' + 'a0' * 20, '0x' + 'a6' * 20
    rows = [fake.row(oracle, [E['UpdateLimitsChanged']], block, 0, data(limit, interval)),
            fake.row(oracle, [E['SetTokenAmount'], word(token)], block, 1, data(*base))]
    stamps = {}
    for n, events in enumerate(steps):
        b = block + 1 + n
        stamps[b] = ts(b)
        for i, (kind, x, y) in enumerate(events):
            topics = ([E['TokenAmountUpdated'], word(token)] if kind == 'update' else
                      [E['CommissionMinted'], word(token), word(vtoken), word('0x' + 'a7' * 20)])
            rows.append(fake.row(oracle, topics, b, i, data(x, y), tx=f'tx {b}'))
    return rows, stamps


def replay(token, rows, stamps, pool):
    updates = [int(r['blockNumber'], 16) for r in rows if r['topics'][0] == E['TokenAmountUpdated']]
    doc = {'pools': {token: [str(pool[0]), str(pool[1])]},
           'lastUpdateAt': {token: str(stamps[max(updates)] if updates else 0)}, 'tokenToVToken': {token: ZERO20}}
    sec = so.Section('oracle')
    so._pool_replay(sec, token, doc, rows, stamps)
    return {(i['severity'], i['rule'].split(' ')[0]) for i in sec.issues}


def fails(rules):
    return {r for r in rules if r[0] == 'FAIL'}


def stub_vtoken(asset):
    """Runtime: ``asset()`` returns ``asset``; any other call (``mintCommission``) succeeds and emits nothing."""
    selector = abi.calldata('asset()')[2:]
    return '0x6000' + '3560e01c63' + selector + '14601057005b73' + asset[2:] + '600052' + '60206000f3'


def two_calls(target):
    """Runtime: calls ``target`` with calldata[0:68] and then calldata[68:136] in one transaction; reverts if either
    call fails."""
    def call(offset):
        # PUSH1 0 (retSize), PUSH1 0 (retOffset), PUSH1 0x44 (argsSize), PUSH1 offset, PUSH1 0 (value), PUSH20 target,
        # GAS, CALL, ISZERO, PUSH1 0x51, JUMPI
        return '600060006044' + '60' + format(offset, '02x') + '6000' + '73' + target[2:] + '5af1' + '15605157'
    code = '366000600037' + call(0) + call(0x44)
    assert len(code) // 2 == 0x50                 # STOP at 0x50, the revert JUMPDEST at 0x51
    return '0x' + code + '00' + '5b60006000fd'


def oracle_two_update_rehearsal(node):
    """The recorded Oracle_V3 runtime: two updates of one token in one transaction (updateInterval == 0), with
    commission, and a commissioned plus a zero-commission update; then the same pair under updateInterval 60."""
    oracle, multi, account = '0x' + 'b0' * 20, '0x' + 'b8' * 20, '0x' + 'b9' * 20
    tokens, vtokens = ['0x' + 'b4' * 20, '0x' + 'b5' * 20], ['0x' + 'b6' * 20, '0x' + 'b7' * 20]
    node.rpc('anvil_setCode', [oracle, runtime('testnet', 'Oracle_V3')])
    for token, vtoken in zip(tokens, vtokens):
        node.rpc('anvil_setCode', [vtoken, stub_vtoken(token)])
    node.rpc('anvil_setCode', [multi, two_calls(oracle)])
    node.rpc('evm_mine', [])
    oracle_abi = json.loads((ROOT / 'deployments/testnet/Oracle_Implementation.json').read_text())['abi']
    v3 = next(a for a in oracle_abi if a.get('name') == 'initializeV3')

    def send(to, data_hex, sender=E1):
        return node.send_alone(sender, to, data_hex)
    receipts = [send(oracle, abi.calldata('initialize(address)', E1), E2),
                send(oracle, abi.calldata('initializeV2(address,address,uint256,uint256)', multi, vtokens[0], 10 ** 6, 0)),
                send(oracle, abi.encode_function(v3, [account, 100_000, tokens, vtokens]))]
    receipts += [send(oracle, abi.calldata('setPoolInfo(address,uint256,uint256)', t, 1000, 400)) for t in tokens]

    def pair(token, a, b):
        return abi.calldata('update(address,uint256)', token, a) + abi.calldata('update(address,uint256)', token, b)[2:]
    receipts += [send(multi, pair(tokens[0], 100, 50), E2), send(multi, pair(tokens[1], 100, 9), E2)]
    assert all(r['status'] == '0x1' for r in receipts), receipts
    rows = [{k: r[k] for k in tp.ROW_KEYS}
            for r in node.rpc('eth_getLogs', [{'address': [oracle], 'fromBlock': '0x0', 'toBlock': 'latest'}])]
    stamps = {int(r['blockNumber'], 16): int(node.rpc('eth_getBlockByNumber', [r['blockNumber'], False])['timestamp'], 16)
              for r in rows}
    state = {t: {'pool': [str(x) for x in abi.decode_parameters([{'type': 'uint256'}, {'type': 'uint256'}],
                                                                node.call(oracle, abi.calldata('poolInfo(address)', t)))],
                 'lastUpdateAt': str(abi.dec_uint(node.call(oracle, abi.calldata('lastUpdateAt(address)', t)))),
                 'tokenToVToken': abi.dec_addr(node.call(oracle, abi.calldata('tokenToVToken(address)', t))).lower()}
             for t in tokens}
    # updateInterval 60: the second same-token update of one transaction reverts the whole transaction
    send(oracle, abi.calldata('setUpdateLimits(uint256,uint256)', 10 ** 6, 60))
    node.rpc('evm_mine', [int(node.rpc('eth_getBlockByNumber', ['latest', False])['timestamp'], 16) + 120])
    guarded = send(multi, pair(tokens[0], 1, 1), E2)['status']
    across = send(multi, abi.calldata('update(address,uint256)', tokens[0], 1)
                  + abi.calldata('update(address,uint256)', tokens[1], 1)[2:], E2)['status']
    return {'oracle': oracle, 'tokens': tokens, 'vtokens': vtokens, 'rows': rows, 'timestamps': stamps,
            'state': state, 'transactions': [receipts[-2]['transactionHash'], receipts[-1]['transactionHash']],
            'intervalGuard': {'sameTokenTwice': guarded, 'twoTokens': across}}


class SchemaControlTest(Case):
    def test_structure(self):
        self.refused('testnet', lambda d: d.pop('oracle'), 'STATE_KEYS', 'INCOMPLETE')
        self.refused('testnet', lambda d: d.update(extra=1), 'STATE_KEYS')
        self.refused('testnet', lambda d: d.update(block=d['block'] - 1), 'STATE_ANCHOR')
        self.refused('testnet', lambda d: d.update(observationV1Sha256='0' * 64), 'STATE_BINDING')
        v = self.vault('testnet')
        self.refused('testnet', lambda d: d['vaults'][v].update(totalManagedAssets='0100'), 'OBSERVATION_VALUE')
        self.refused('testnet', lambda d: d['vaults'][v]['terms'].pop('roundCap'), 'STATE_KEYS', 'INCOMPLETE')
        self.refused('testnet', lambda d: d['vaults'].pop(v), 'STATE_UNIVERSE', 'INCOMPLETE')
        self.refused('testnet', lambda d: d['vaults'].update({'0x' + '48' * 20: {}}), 'STATE_UNIVERSE')

    def test_consistently_stale_observations_are_refused(self):
        i = self.inputs['testnet']
        v1 = copy.deepcopy(i['v1'])
        v1['block'] -= 1
        doc = copy.deepcopy(i['doc'])
        doc['block'] -= 1
        try:
            so.check(i['verified'], i['derived'], doc, 'testnet', observation_v1=v1, observation_v1_sha256=i['v1sha'],
                     headers=i['headers'], reviewed=self.built['testnet']['reviewed'])
            error = None
        except Exception as caught:      # noqa: BLE001
            error = caught
        self.assertIsInstance(error, HistoryRefused, repr(error))
        self.assertEqual(error.code, 'STATE_ANCHOR')

    def test_status_sequencing(self):
        try:
            report = self.check('testnet')
        except Exception as error:      # noqa: BLE001 - a refusal here means B emitted a non-local status
            self.fail(f'B-local check refused its own consistent document: {error!r}')
        self.assertEqual(report['overall'], 'INCOMPLETE')
        self.assertTrue(so.local_exit(report))
        self.assertIn('PENDING_INDEPENDENT_REREAD', {s['status'] for s in report['sections'].values()})
        text = json.dumps(report)
        for forbidden in so.FORBIDDEN_STATUSES:
            self.assertNotIn(forbidden, text)
        import inspect
        for module in (so, observe, col, tp, cp):
            source = inspect.getsource(module)
            for forbidden in so.FORBIDDEN_STATUSES:
                self.assertNotIn(f"'{forbidden}'", source.replace("FORBIDDEN_STATUSES = ('REREAD_EQUAL', "
                                                                  "'INDEPENDENTLY_REPRODUCED', 'APPROVAL_RECORD_PREPARED', "
                                                                  "'SOURCE_BOUND')", ''), module.__name__)


# ------------------------------------------------------------------------------------------------ H. loopback rehearsal
class LoopbackRehearsalTest(unittest.TestCase):
    """The recorded YieldVault_V3 / YieldVault_V1 / Oracle_V3 runtimes on a loopback Anvil node (zero accounts,
    no fork, no network). Shows where history is silent and the state rules detect the change."""

    @classmethod
    def setUpClass(cls):
        if shutil.which('anvil') is None:
            raise unittest.SkipTest('NOT RUN: anvil is not available in this export environment')
        if not (ROOT / 'EXPORT-MANIFEST.txt').is_file():
            raise unittest.SkipTest('NOT RUN: Foundry binaries run only inside an approved export')
        import anvil
        cls.node = anvil.LoopbackNode(688689, TS0, ROOT)

    @classmethod
    def tearDownClass(cls):
        cls.node.close()

    def deploy(self, address, chain, name):
        self.node.rpc('anvil_setCode', [address, runtime(chain, name)])
        self.node.rpc('evm_mine', [])

    def send(self, to, data_hex, sender=E2):
        receipt = self.node.send_alone(sender, to, data_hex)
        self.assertEqual(receipt['status'], '0x1', receipt)
        return receipt

    def logs(self, address):
        rows = self.node.rpc('eth_getLogs', [{'address': [address], 'fromBlock': '0x0', 'toBlock': 'latest'}])
        return [{k: r[k] for k in tp.ROW_KEYS} for r in rows]

    def call(self, address, sig, *args):
        return self.node.call(address, abi.calldata(sig, *args))

    def initialize_vault(self, address, chain):
        item = next(a for a in json.loads((ROOT / 'deployments' / chain / 'YieldVault.json').read_text())['abi']
                    if a.get('name') == 'initialize')
        # RoundParams: the recorded V1 struct carries a trailing openedAt (0 = open now); V3 has none
        first = [100, 50, 30, 10 ** 24, 0, 1, 1000, 500] + ([0] if chain == 'mainnet' else [])
        params = [['0x' + '5b' * 20, '0x' + '5c' * 20, E2, E1, E1, 'Rehearsal', 'RH', first]]
        self.send(address, abi.encode_function(item, params))

    def vault_view(self, vault):
        s = {k: abi.dec_uint(self.call(vault, f'{k}()')) for k in
             ('roundIndex', 'phase', 'lockedAt', 'openWindow', 'lockDuration', 'settleTimelockWindow', 'roundCap',
              'maxLossBps', 'openedAt', 'openDeadline', 'settleProposedAt', 'settleAmount', 'settledAt')}
        s['slot4'] = int(self.node.rpc('eth_getStorageAt', [vault, hex(so.SLOT_PHASE), 'latest']), 16)
        s['slot15'] = int(self.node.rpc('eth_getStorageAt', [vault, hex(so.SLOT_LOCKED_AT), 'latest']), 16)
        s['slot22'] = int(self.node.rpc('eth_getStorageAt', [vault, hex(so.SLOT_RETIRED), 'latest']), 16)
        s['timestamp'] = int(self.node.rpc('eth_getBlockByNumber', ['latest', False])['timestamp'], 16)
        return s

    def test_v3_eventless_setters_are_silent_in_history_and_detected_in_state(self):
        vault = '0x' + 'a3' * 20
        self.deploy(vault, 'testnet', 'YieldVault_V3')
        self.initialize_vault(vault, 'testnet')
        rows = self.logs(vault)
        self.assertIn(E['RoundOpened'], [r['topics'][0] for r in rows])
        for sig, value in (('setRoundCap(uint256)', 7), ('setLockDuration(uint256)', 0), ('setMaxLossBps(uint256)', 20000)):
            before = len(self.logs(vault))
            self.send(vault, abi.calldata(sig, value), sender='0x' + '9a' * 20)     # any account: unguarded
            self.assertEqual(len(self.logs(vault)), before, sig)                     # eventless
        view = self.vault_view(vault)
        self.assertEqual((view['roundCap'], view['lockDuration'], view['maxLossBps']), (7, 0, 20000))
        replay = so.replay_vault(self.logs(vault), {})
        self.assertEqual(replay['state']['roundCap'], 10 ** 24)          # history still says the opened cap
        self.assertEqual(replay['state']['phase'], view['slot4'])
        # setOpenPeriodRemaining emits the same event as the privileged extension, forces OPEN, keeps T2
        self.send(vault, abi.calldata('setOpenPeriodRemaining(uint256)', 500), sender='0x' + '9a' * 20)
        view = self.vault_view(vault)
        replay = so.replay_vault(self.logs(vault), {})
        self.assertEqual(view['openDeadline'], replay['state']['openDeadline'])
        self.assertEqual(view['openWindow'], view['openDeadline'] - view['openedAt'])
        self.assertEqual((view['slot4'], view['slot15']), (replay['state']['phase'], replay['state']['lockedAt']))
        # setPhase(LOCKED) emits RoundLocked + PhaseChanged; the replay follows the stored phase and lockedAt
        self.send(vault, abi.calldata('setPhase(uint8)', 1), sender='0x' + '9a' * 20)
        view = self.vault_view(vault)
        replay = so.replay_vault(self.logs(vault), {})
        self.assertTrue(replay['phaseChanged'])
        self.assertEqual((view['slot4'], view['slot15'], view['phase']), (1, replay['state']['lockedAt'], 1))
        self.assertEqual(view['slot22'], 0)
        self.assertEqual(replay['state']['phase'], view['slot4'])

    def test_v1_lazy_lock_effective_phase_and_absent_setters(self):
        vault = '0x' + 'a1' * 20
        self.deploy(vault, 'mainnet', 'YieldVault_V1')
        self.initialize_vault(vault, 'mainnet')
        view = self.vault_view(vault)
        self.assertEqual((view['phase'], view['slot4']), (0, 0))
        self.node.rpc('evm_mine', [view['openDeadline'] + 1])
        view = self.vault_view(vault)
        self.assertGreaterEqual(view['timestamp'], view['openDeadline'])
        # stored OPEN, effective LOCKED; stored lockedAt 0, effective lockedAt = openDeadline (P2 rule)
        self.assertEqual((view['slot4'], view['phase'], view['slot15'], view['lockedAt']), (0, 1, 0, view['openDeadline']))
        effective = 1 if view['slot4'] == 0 and view['timestamp'] >= view['openDeadline'] else view['slot4']
        self.assertEqual(view['phase'], effective)
        replay = so.replay_vault(self.logs(vault), {})
        self.assertEqual((replay['state']['phase'], replay['state']['lockedAt']), (0, 0))
        receipt = self.node.send_alone('0x' + '9a' * 20, vault, abi.calldata('setRoundCap(uint256)', 7))
        self.assertEqual(receipt['status'], '0x0')                      # not in the recorded V1 runtime
        self.assertEqual(abi.dec_uint(self.call(vault, 'roundCap()')), 10 ** 24)

    def test_oracle_v3_pool_replay_matches_the_recorded_runtime(self):
        oracle = '0x' + 'a0' * 20
        token, slp = '0x' + 'a4' * 20, '0x' + 'a5' * 20
        self.deploy(oracle, 'testnet', 'Oracle_V3')
        self.send(oracle, abi.calldata('initialize(address)', E1))
        self.send(oracle, abi.calldata('initializeV2(address,address,uint256,uint256)', slp, '0x' + 'a6' * 20, 10 ** 6, 60),
                  sender=E1)
        self.send(oracle, abi.calldata('setPoolInfo(address,uint256,uint256)', token, 1000, 400), sender=E1)
        self.send(oracle, abi.calldata('update(address,uint256)', token, 25), sender=slp)
        self.node.rpc('evm_mine', [int(self.node.rpc('eth_getBlockByNumber', ['latest', False])['timestamp'], 16) + 120])
        last = self.send(oracle, abi.calldata('update(address,uint256)', token, 5), sender=slp)
        too_big = self.node.send_alone(slp, oracle, abi.calldata('update(address,uint256)', token, 10 ** 7))
        self.assertEqual(too_big['status'], '0x0')
        rows = self.logs(oracle)
        timestamps = {int(r['blockNumber'], 16): int(self.node.rpc('eth_getBlockByNumber', [r['blockNumber'], False])['timestamp'], 16)
                      for r in rows}
        pool = abi.decode_parameters([{'type': 'uint256'}, {'type': 'uint256'}], self.call(oracle, 'poolInfo(address)', token))
        last_at = abi.dec_uint(self.call(oracle, 'lastUpdateAt(address)', token))
        doc = {'pools': {token: [str(pool[0]), str(pool[1])]}, 'lastUpdateAt': {token: str(last_at)},
               'tokenToVToken': {token: ZERO20}}
        sec = so.Section('oracle')
        so._pool_replay(sec, token, doc, rows, timestamps)
        self.assertEqual([int(x) for x in doc['pools'][token]], [1030, 400])
        self.assertEqual(last_at, timestamps[int(last['blockNumber'], 16)])
        self.assertFalse([i for i in sec.issues if i['severity'] == 'FAIL'], sec.issues)
        doc['pools'][token] = ['1031', '400']
        sec = so.Section('oracle')
        so._pool_replay(sec, token, doc, rows, timestamps)
        self.assertIn('FAIL', {i['severity'] for i in sec.issues})

    def test_oracle_v3_two_updates_in_one_transaction_attribute_commission_per_update(self):
        r = oracle_two_update_rehearsal(self.node)
        (a, b), rows = r['tokens'], r['rows']
        # the recorded runtime's order inside one transaction: CommissionMinted, then its own TokenAmountUpdated
        for tx, kinds in zip(r['transactions'], (['CommissionMinted', 'TokenAmountUpdated'] * 2,
                                                 ['CommissionMinted', 'TokenAmountUpdated', 'TokenAmountUpdated'])):
            own = [x for x in rows if x['transactionHash'] == tx]
            self.assertEqual([next(k for k, v in E.items() if v == x['topics'][0]) for x in own], kinds)
            indices = [int(x['logIndex'], 16) for x in own]
            self.assertEqual(indices, list(range(indices[0], indices[0] + len(own))))
        self.assertEqual(r['state'][a]['pool'], ['1150', '405'])     # 400 + 4 (10 * 400 / 1000) + 1 (5 * 404 / 1100)
        self.assertEqual(r['state'][b]['pool'], ['1109', '404'])     # the 9-token update has no commission
        for token, wrong in ((a, '410'), (b, '408')):                 # the per-transaction sum added to every update
            doc = {'pools': {token: r['state'][token]['pool']}, 'lastUpdateAt': {token: r['state'][token]['lastUpdateAt']},
                   'tokenToVToken': {token: r['state'][token]['tokenToVToken']}}
            sec = so.Section('oracle')
            so._pool_replay(sec, token, doc, rows, r['timestamps'])
            self.assertFalse([i for i in sec.issues if i['severity'] == 'FAIL'], sec.issues)
            doc['pools'][token] = [doc['pools'][token][0], wrong]
            sec = so.Section('oracle')
            so._pool_replay(sec, token, doc, rows, r['timestamps'])
            self.assertIn(('FAIL', f'O1_POOL_VTOKEN {token}'), {(i['severity'], i['rule']) for i in sec.issues})
        self.assertEqual([r['state'][t]['tokenToVToken'] for t in r['tokens']], r['vtokens'])
        self.assertEqual(r['intervalGuard'], {'sameTokenTwice': '0x0', 'twoTokens': '0x1'})


if __name__ == '__main__':
    unittest.main()
