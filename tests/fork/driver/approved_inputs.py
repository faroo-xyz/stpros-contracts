"""Approved release inputs for the Fork driver. Test-only.

The testnet run has two input modes:
- **SYNTHETIC** (``governance`` None or ``{'source': 'SYNTHETIC_MODEL'}``):
  - labelled synthetic actors and the simulation policy of ``release_config.build``;
  - never release parameters, and never a formal E2E PASS.
- **APPROVED_RECORD** (``{'source': 'APPROVED_RECORD', 'record': (path, sha256)}``): every role and parameter comes
  from one E2E release record, which must be:
  - pinned in ``e2e_identity.APPROVED_E2E_RECORDS``, with its Safe manifests pinned (FR-X8);
  - bound to this chain, this Fork anchor and the frozen sources;
  - verified against the node **before T-0**.

The same record is the E2E hook's record.

Record ``ownerInputs`` (each item counts only with a non-empty ``approvals[item]`` reference):

    approvals     {actors, safes, keeper, reporter, destinations, policy, commission, gasCap}: reference strings
    destinations  ordered SlpDistributor list;  destinationsHash: keccak256 of the raw getSlps() return bytes
    policy        {expectedGrossAprPpm, maxAprPpm, maxUpdateAmount, updateInterval}
    commission    {decision: 'K1' | 'K2', account, ratePpm, treasuryGateException (K1 only: Agent A's written ruling)}
    gas           {estimator: 'a', confirmedGasCap}

Unapproved inputs are handled by kind:
- **Build inputs** (actors, Safes, keeper, reporter, destinations, policy) are needed to build U_t. An unapproved
  one, or one that differs from the node, stops the run BLOCKED (or FAIL) before T-0.
- **Gate inputs** (commission, gas cap) are release gates. An unapproved one stays a BLOCKED row and the run goes on.
  The driver never changes the commission recipient.
"""
import json
from pathlib import Path
import sys

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
sys.path[:0] = [str(HERE), str(ROOT)]
from models import truth  # noqa: E402
import safe_governance as sg  # noqa: E402
from tools.lib import abi  # noqa: E402
from tools.lib.keccak import keccak256  # noqa: E402

REQUIRED_ACTORS = ('gov', 'can', 'guardian', 'ops', 'treasury', 'deployer', 'reporter', 'keeper')
SAFE_ROLES = ('gov', 'can', 'guardian', 'ops', 'treasury')
BUILD_ITEMS = ('actors', 'safes', 'keeper', 'reporter', 'destinations', 'policy')
GATE_ITEMS = ('commission', 'gasCap')
SYNTHETIC_LABEL = ('SYNTHETIC: simulation policy (APR 250,000 / 500,000 stress), labelled actors and observed '
                   'destinations; never release parameters')


def _lower(a):
    return a.lower() if isinstance(a, str) else a


def _blocked(rid, detail):
    return {'id': rid, 'verdict': 'BLOCKED', 'expected': '1', 'actual': '0', 'detail': detail[:300]}


def approvals(record):
    return ((record or {}).get('ownerInputs') or {}).get('approvals') or {}


def missing_approvals(record):
    a = approvals(record)
    return [i for i in BUILD_ITEMS + GATE_ITEMS if not (isinstance(a.get(i), str) and a[i].strip())]


def run_anchor(run):
    """The anchor a record binds: the Fork block of a real topology, else the node head when the run started."""
    binding = run.topo.get('binding') or {}
    if binding.get('forkBlock') and binding.get('forkBlockHash'):
        return {'block': binding['forkBlock'], 'hash': binding['forkBlockHash']}
    return run.start_anchor


def source_binding_problems(record):
    """The record's frozen commit, E2E spec, research inventory and canonical artifact hashes (``e2e_identity``)."""
    import e2e
    import e2e_identity
    spec = e2e.load_spec()
    return e2e_identity.binding_problems(record, e2e._sha(json.dumps(spec, sort_keys=True).encode()), spec,
                                         e2e.artifact_entries()[1])


def destinations_now(chain, distributor):
    raw = chain.call(distributor, 'getSlps()')
    return [a.lower() for a in abi.decode_parameters([{'type': 'address[]'}], raw)[0]], keccak256(bytes.fromhex(raw[2:]))


def load(run, ref):
    """(record or None, rows). Rows: BLOCKED when the record cannot count, FAIL when its bindings differ."""
    import campaign_input
    import e2e_identity
    record, problems = e2e_identity.load_record(*ref)
    if record is None:
        return None, [_blocked('INPUT.record.approved', '; '.join(problems) or 'record not usable')]
    rows = [truth('INPUT.record.approved', True),
            truth('INPUT.record.chainId', record.get('chainId') == run.m['chainId'], f'record chain {record.get("chainId")}'),
            truth('INPUT.record.anchor==fork', record.get('anchor') == run_anchor(run),
                  f'record anchor {record.get("anchor")} is not the Fork anchor {run_anchor(run)}')]
    binding = source_binding_problems(record)
    rows.append(truth('INPUT.record.sourceBinding', not binding, '; '.join(binding)))
    manifests = {r: ((record.get('safes') or {}).get(r) or {}).get('safeManifestSha256') for r in SAFE_ROLES}
    unpinned = sorted(r for r, h in manifests.items() if h not in campaign_input.APPROVED_SAFE_IDENTITY_MANIFESTS)
    rows.append(truth('INPUT.safes.manifestsApproved', not unpinned, f'Safe identity manifest not approved for {unpinned} (FR-X8)')
                if not unpinned else _blocked('INPUT.safes.manifestsApproved',
                                              f'Safe identity manifest not approved for {unpinned} (FR-X8)'))
    return record, rows


def check_build_inputs(run, record):
    """Pre-T-0 checks of every build input against the record and the node. Returns (inputs or None, rows)."""
    chain, m = run.chain, run.m
    rows, oi, ok_items = [], (record.get('ownerInputs') or {}), approvals(record)
    for item in BUILD_ITEMS:
        if not (isinstance(ok_items.get(item), str) and ok_items[item].strip()):
            rows.append(_blocked(f'INPUT.{item}.approved', f'{item}: no owner approval reference in the record'))
        else:
            rows.append(truth(f'INPUT.{item}.approved', True))
    act = {k: _lower(v) for k, v in (record.get('actors') or {}).items()}
    missing = [r for r in REQUIRED_ACTORS if not (isinstance(act.get(r), str) and len(act[r]) == 42)]
    rows.append(truth('INPUT.actors.complete', not missing, f'actors missing {missing}'))
    if missing:
        return None, rows
    safes = {}
    for role in SAFE_ROLES:
        s = (record.get('safes') or {}).get(role)
        if not s or _lower(s.get('address')) != act[role]:
            rows.append(truth(f'INPUT.safes.{role}.present', False,
                              f'{role} requires an approved Safe at the approved actor address (no EOA substitute)'))
            continue
        problems = sg.safe_problems(chain, role, s, check_nonce=True)
        rows.append(truth(f'INPUT.safes.{role}.identity==fork', not problems, '; '.join(problems)))
        safes[role] = s
    policy = sg.policy_problems(safes, deployer=act['deployer'], keeper=act['keeper'], reporter=act['reporter'])
    rows.append(truth('INPUT.safes.signerPolicy', not policy, '; '.join(policy)))
    if act['deployer'] in set(act[r] for r in SAFE_ROLES):
        rows.append(truth('INPUT.deployer.notASafe', False, 'the deployer is one of the governance Safes'))
    keeper_now = chain.addr(m['distributor'], 'keeper()')
    reporter_now = chain.addr(m['oracle'], 'slp()')
    rows += [truth('INPUT.keeper==fork', keeper_now == act['keeper'], f'fork keeper {keeper_now}, approved {act["keeper"]}'),
             truth('INPUT.reporter==fork', reporter_now == act['reporter'], f'fork reporter {reporter_now}, approved {act["reporter"]}')]
    dests, digest = destinations_now(chain, m['distributor'])
    approved = [_lower(d) for d in oi.get('destinations') or []]
    rows += [truth('INPUT.destinations.ordered==fork', dests == approved, f'fork {dests}, approved {approved}'),
             truth('INPUT.destinations.hash==fork', digest == _lower(oi.get('destinationsHash')), f'fork hash {digest}')]
    p = oi.get('policy') or {}
    try:
        g, mx = int(p['expectedGrossAprPpm']), int(p['maxAprPpm'])
        cap, interval = int(p['maxUpdateAmount']), int(p['updateInterval'])
        rule = mx == 2 * g and 0 < mx <= 500_000 and cap > 0 and interval >= 3600
    except (KeyError, TypeError, ValueError):
        g = mx = cap = interval = None
        rule = False
    rows.append(truth('INPUT.policy.rule', rule, 'maxAprPpm = 2 x expectedGrossAprPpm <= 500,000; maxUpdateAmount > 0; interval >= 3600'))
    if rule:
        # testnet migration keeps the V3 absolute cap and sets interval = max(V3, 1 h): the approved values must match
        cap_now, interval_now = chain.uint(m['oracle'], 'maxUpdateAmount()'), chain.uint(m['oracle'], 'updateInterval()')
        rows += [truth('INPUT.policy.maxUpdateAmount==fork', cap == cap_now, f'fork {cap_now}, approved {cap}'),
                 truth('INPUT.policy.updateInterval==max(fork,1h)', interval == max(interval_now, 3600),
                       f'fork {interval_now} -> {max(interval_now, 3600)}, approved {interval}')]
    usable = all(r['verdict'] == 'PASS' for r in rows)
    inputs = {'actors': act, 'safes': safes, 'destinations': approved, 'destinationsHash': digest,
              'policy': {'expectedGrossAprPpm': g, 'maxAprPpm': mx, 'maxUpdateAmount': str(cap), 'updateInterval': interval},
              'source': 'APPROVED_RECORD'} if usable else None
    return inputs, rows


def check_gate_inputs(run, record):
    """Commission (K1/K2) and gas-cap rows. BLOCKED rows keep the gate closed; the run continues."""
    chain, m = run.chain, run.m
    oi, ok_items, rows = (record.get('ownerInputs') or {}), approvals(record), []
    act = {k: _lower(v) for k, v in (record.get('actors') or {}).items()}
    c = oi.get('commission') or {}
    account_now = chain.addr(m['oracle'], 'commissionAccount()')
    rate_now = chain.uint(m['oracle'], 'commissionRatePpm()')
    if not (isinstance(ok_items.get('commission'), str) and ok_items['commission'].strip()) or c.get('decision') not in ('K1', 'K2'):
        rows.append(_blocked('INPUT.commission.approved', 'TD-9 not decided (K1 or K2): the commission recipient stays as it is, '
                                                          'the Treasury Safe gate stays BLOCKED'))
    elif c['decision'] == 'K2':
        rows.append(truth('INPUT.commission.K2.fork==treasurySafe', account_now == act['treasury'] == _lower(c.get('account'))
                          and rate_now == c.get('ratePpm'),
                          f'fork commission {account_now} at {rate_now}; K2 requires the Treasury Safe {act["treasury"]} '
                          f'(changed on chain by E1 before the anchor; the driver never changes it)'))
    else:
        same = account_now == _lower(c.get('account')) and rate_now == c.get('ratePpm')
        rows.append(truth('INPUT.commission.K1.fork==approvedAccount', same, f'fork commission {account_now} at {rate_now}'))
        if not (isinstance(c.get('treasuryGateException'), str) and c['treasuryGateException'].strip()):
            rows.append(_blocked('INPUT.commission.K1.treasuryGate', 'K1 keeps E1 as the commission account: the Treasury '
                                                                     'Safe gate needs the written NOT-APPLICABLE ruling (K1b)'))
        else:
            rows.append({'id': 'INPUT.commission.K1.treasuryGate', 'verdict': 'PASS', 'expected': '1', 'actual': '1',
                         'detail': f'NOT APPLICABLE by written ruling {c["treasuryGateException"]} (an exception, recorded)'})
    if not (isinstance(ok_items.get('gasCap'), str) and ok_items['gasCap'].strip()):
        rows.append(_blocked('INPUT.gasCap.approved', 'TD-5 / TD-6 not approved: the frozen 2.2M cap stays the only cap'))
    else:
        rows.append(truth('INPUT.gasCap.approved', True))
    return rows


def approved_gas(record):
    """The approved testnet cap and estimator, or None."""
    if 'gasCap' in missing_approvals(record):
        return None
    gas = (record.get('ownerInputs') or {}).get('gas') or {}
    return {'estimator': gas.get('estimator'), 'confirmedGasCap': gas.get('confirmedGasCap')}


def config_problems(config, inputs):
    """The release config built for the CLI must carry exactly the approved inputs."""
    out = []
    for role in REQUIRED_ACTORS:
        if _lower(config['actors'].get(role)) != inputs['actors'][role]:
            out.append(f'config actor {role} {config["actors"].get(role)} is not the approved {inputs["actors"][role]}')
    if [_lower(d) for d in config['destinations']] != inputs['destinations']:
        out.append('config destinations differ from the approved ordered list')
    for key, value in inputs['policy'].items():
        if str(config['policy'].get(key)) != str(value):
            out.append(f'config policy {key} {config["policy"].get(key)} is not the approved {value}')
    return out
