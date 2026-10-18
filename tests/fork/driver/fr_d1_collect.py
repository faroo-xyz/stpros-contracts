"""Operator entry point of an authorized FR-D1 collection (S1 / S2). Test-only; run inside an approved export.

  python3 tests/fork/driver/fr_d1_collect.py check  --authorization A --sha256 H
  python3 tests/fork/driver/fr_d1_collect.py select --authorization A --sha256 H --ledger-dir L
  python3 tests/fork/driver/fr_d1_collect.py collect --authorization A --sha256 H --ledger-dir L \\
      --history-root R --b0-dir B --run-id ID [--new-vault FACTORY:INDEX:VAULT ...] \\
      [--s1-authorization A1 --s1-sha256 H1 --s1-ledger-dir L1]

``check`` is offline: it loads a record through the same loader as a run and prints the envelope it binds (an
unapproved public record ends with FR_D1_NOT_AUTHORIZED after every envelope check passed). ``select`` (S1 only)
is the single anchor-selection invocation. ``collect`` builds the spec from the pinned anchor (S1) or from the
verified S1 ledger reference (S2), runs the collector under the CR-D3 supervisor with the authorization's
restart bound, and content-verifies the result. The native transport is used only when the record is pinned in
``fr_d1_approvals.COLLECTION``; nothing here reads the environment, a key or a wallet, or sends a transaction.
Every refusal prints ``{status, code, detail}`` and exits non-zero.
"""
import argparse
import asyncio
import datetime
import hashlib
import json
from pathlib import Path
import sys

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
sys.path[:0] = [str(HERE), str(ROOT)]
import collection_plan as cp  # noqa: E402
import collector as col  # noqa: E402
import fr_d1_scope as scope  # noqa: E402
import history as h  # noqa: E402
import observe  # noqa: E402
import transport as tp  # noqa: E402
from transport import CollectionStop  # noqa: E402

COOLDOWN_S = 60


def utc():
    return datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


def _gate(auth, ledger_dir, invocation):
    ledger = tp.BudgetLedger(ledger_dir, auth, invocation=invocation)
    return tp.HttpGate(auth['chains'][0], ledger, approved=col.APPROVED_COLLECTION_AUTHORIZATIONS)


def _vault(text):
    factory, index, vault = text.split(':')
    return factory.lower(), int(index), vault.lower()


def check(args):
    auth = col.load_authorization(args.authorization, args.sha256)
    return {'status': 'LOADED', 'authorizationId': auth['authorizationId'], 'stage': auth.get('stage'),
            'chains': auth['chains'], 'mode': auth['mode'], 'maxHttpPosts': auth['maxHttpPosts']}


def select(args):
    auth = col.load_authorization(args.authorization, args.sha256)
    anchor = asyncio.run(scope.select_anchor(_gate(auth, args.ledger_dir, f'select-{utc()}'), utc()))
    return {'status': 'ANCHOR_PINNED', 'anchor': anchor}


def build_spec(auth, args):
    """(spec, reference): S1 from its own pinned anchor, S2 from the verified S1 ledger reference."""
    chain = auth['chains'][0]
    _, b0_plan = col.b0_segment(args.history_root, args.b0_dir, chain)
    if auth['stage'] == 'S1':
        if args.s1_authorization:
            raise CollectionStop('S1_REFERENCE_UNEXPECTED', 'an S1 run selects its own anchor', 'REFUSED')
        ledger = tp.BudgetLedger(args.ledger_dir, auth, invocation='spec').inspect()
        if ledger.anchor is None:
            raise CollectionStop('ANCHOR_NOT_PINNED', 'run select first', 'REFUSED')
        anchor, vaults, reference = ledger.anchor, [_vault(v) for v in args.new_vault], None
    else:
        if not (args.s1_authorization and args.s1_sha256 and args.s1_ledger_dir):
            raise CollectionStop('S1_REFERENCE_REQUIRED', 'S2 needs the S1 authorization and ledger', 'REFUSED')
        if args.new_vault:
            raise CollectionStop('S2_SPEC_NOT_S1_SPEC', 'S2 takes its vault set from the S1 ledger', 'REFUSED')
        reference = scope.s1_reference(auth, args.s1_authorization, args.s1_sha256, args.s1_ledger_dir,
                                       load=col.load_authorization)
        anchor, vaults = reference['anchor'], [tuple(v) for v in reference['newVaults']]
    spec = cp.spec(chain, anchor_block=anchor['block'], anchor_hash=anchor['hash'], b0_plan=b0_plan,
                   b0_plan_sha256=h.B0_PREFIX[chain]['planSha256'], new_vaults=vaults)
    return spec, reference


def collect(args):
    auth = col.load_authorization(args.authorization, args.sha256)
    if 'scope' not in auth:
        raise CollectionStop('AUTHORIZATION_SCOPE_REQUIRED', 'fr_d1_collect runs v2 records only', 'REFUSED')
    spec, reference = build_spec(auth, args)
    spec_sha = hashlib.sha256(cp.canonical_bytes(spec)).hexdigest()
    made = []

    def make():
        gate = _gate(auth, args.ledger_dir, f'{auth["stage"].lower()}-{args.run_id}-{len(made)}')
        made.append(gate)
        return col.Collector(spec, spec_sha, args.history_root, args.run_id, gate, b0_dir=args.b0_dir, utc=utc,
                             observe=observe.read, collector=auth['stage'], reference=reference)
    summary = asyncio.run(col.supervise(make, max_restarts=auth['maxRestarts'], cooldown_s=COOLDOWN_S,
                                        sleep=asyncio.sleep))
    report = observe.verify_run(args.history_root, args.run_id, spec)          # CONTENT_VERIFIED + B-local state
    return {'status': summary['status'], 'specSha256': spec_sha, 'summary': summary, 'verification': report,
            'httpPostsUsed': made[-1].ledger.used, 'ceiling': auth['maxHttpPosts']}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest='command', required=True)
    for name in ('check', 'select', 'collect'):
        p = sub.add_parser(name, allow_abbrev=False)
        p.add_argument('--authorization', required=True, type=Path)
        p.add_argument('--sha256', required=True)
        if name != 'check':
            p.add_argument('--ledger-dir', required=True, type=Path)
        if name == 'collect':
            p.add_argument('--history-root', required=True, type=Path)
            p.add_argument('--b0-dir', required=True)
            p.add_argument('--run-id', required=True)
            p.add_argument('--new-vault', action='append', default=[])
            p.add_argument('--s1-authorization', type=Path)
            p.add_argument('--s1-sha256')
            p.add_argument('--s1-ledger-dir', type=Path)
    args = parser.parse_args(argv)
    for name in ('history_root', 'ledger_dir'):
        if getattr(args, name, None) is not None:
            setattr(args, name, getattr(args, name).resolve())
    try:
        result = {'check': check, 'select': select, 'collect': collect}[args.command](args)
    except (CollectionStop, h.HistoryRefused) as stop:
        print(json.dumps({'status': stop.status, 'code': stop.code, 'detail': str(stop)[:2000]}, indent=1))
        return 1
    print(json.dumps(result, indent=1, sort_keys=True, default=str))
    return 0


if __name__ == '__main__':
    sys.exit(main())
