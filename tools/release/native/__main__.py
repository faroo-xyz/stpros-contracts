"""Additive unsigned release construction and read-only checkpoint CLI.

Run inside an approved export. Inputs are explicit JSON paths and independent
hashes; no environment-file discovery, wallet, signing or execution API exists.
"""
import argparse
import asyncio
import re
import sys

from ...lib import release_json as j
from . import inputs, plans, checkpoint as observations, timelock
from .rpc import ReleaseRpc
from ._shared import UNDEFINED, field, require, pretty_json, string

RELEASE_ACTIONS = ('preflight', 'authority', 'p1-runbook', 'build-handoff', 'build-u',
                   'estimate-gas', 'testnet-handoff', 'postcheck')


def review_digest(value):
    return isinstance(value, str) and re.fullmatch(r'[a-f0-9]{64}', value, re.I) is not None


async def run_release(action, *, release_config='', review_hash='', rpc_url='', checkpoint='pre',
                      checkpoint_review='', checkpoint_review_hash='', rpc_factory=ReleaseRpc,
                      clock_ms=observations.wall_clock_ms):
    require(action in RELEASE_ACTIONS, 'Unsupported release action')
    if not j.truthy(release_config) or not review_digest(review_hash):
        raise j.ReleaseError('INCOMPLETE', 'config and independent review hash required')
    config = j.checked_json(release_config, review_hash)
    inputs.validate_recorded_topology(config, j.checked_json('tools/release/registry.json'))
    inventory = j.checked_json(config['inventoryFile'])
    artifacts = {name: j.checked_json(path) for name, path in config['artifactFiles'].items()}
    context = inputs.ReleaseContext(config, inventory, artifacts)
    checked = context.preflight()
    require(checkpoint in ('pre', 'execution'), 'Invalid checkpoint')
    if action == 'postcheck' or (action == 'preflight' and checkpoint == 'execution'):
        require(j.truthy(rpc_url), 'Explicit read-only RPC required')
        execution_evidence = UNDEFINED
        if config['mode'] == 'release' and action != 'postcheck':
            require(
                j.truthy(checkpoint_review)
                and review_digest(checkpoint_review_hash),
                'Fresh independently reviewed execution evidence and hash required'
            )
            execution_evidence = j.checked_json(checkpoint_review, checkpoint_review_hash)
        result = await observations.verify_checkpoint(
            context,
            rpc_factory(rpc_url, config['mode'] == 'simulation'),
            'R4' if action == 'postcheck' else 'H',
            {'executionEvidence': execution_evidence},
            clock_ms=clock_ms
        )
        if config['mode'] == 'release':
            timelock.require_action_bearing_coverage(result)
        return result
    live = None
    if config['mode'] == 'release':
        require(j.truthy(rpc_url), 'Release output requires a fresh pinned RPC recheck')
        live = await observations.recheck_observed(context, rpc_factory(rpc_url), clock_ms=clock_ms)
        timelock.require_action_bearing_coverage(live)
    if action == 'preflight':
        result = {**checked, 'deploymentDefinitions': plans.deployment_definitions(config, artifacts)}
    elif action == 'authority':
        result = {
            'checked': checked,
            'roles': inventory['roles'],
            'assumptions': 'Completeness requires the reviewed full history and fresh recheck, not candidate accounts.'
        }
    elif action == 'p1-runbook':
        result = plans.p1_runbook(context)
    elif action == 'build-handoff':
        result = plans.build_handoff(context)
    elif action == 'build-u':
        result = plans.build_u(context)
    elif action == 'testnet-handoff':
        result = plans.testnet_handoff(context)
    else:
        require(j.truthy(rpc_url), 'Explicit local simulation RPC required')
        transactions = plans.build_u(context)['transactions']
        result = await observations.estimate_gas(config, transactions, config['addresses']['timelock'], rpc_factory(rpc_url, True, True))
    if live is not None:
        anchor = {key: live[key] for key in ('reviewedBlock', 'reviewedHash', 'currentBlock', 'currentHash')}
        if j.truthy(field(result, 'meta')):
            result['meta']['description'] += ' Verified block '+string(anchor['currentBlock'])+' ('+anchor['currentHash']+'); reviewed GOV nonce '+string(config['review']['safes']['gov']['nonce'])+'. Recheck before signing.'
        else:
            result = {**result, 'verification': anchor}
    return result


def parser():
    command = argparse.ArgumentParser(description=__doc__)
    groups = command.add_subparsers(dest='group', required=True)
    release = groups.add_parser('release', help='Unsigned release artifacts and read-only verification')
    actions = release.add_subparsers(dest='action', required=True)
    for name in RELEASE_ACTIONS:
        action = actions.add_parser(name)
        action.add_argument('--release-config', default='')
        action.add_argument('--review-hash', default='')
        action.add_argument('--rpc', default='')
        # The original callback checks this after bundle admission; preserve that order.
        action.add_argument('--checkpoint', default='pre', metavar='pre|execution')
        action.add_argument('--checkpoint-review', default='')
        action.add_argument('--checkpoint-review-hash', default='')
    artifact = groups.add_parser('artifact', help='Prepare an unapproved normalized compiler artifact')
    normalization = artifact.add_subparsers(dest='action', required=True).add_parser('normalize')
    normalization.add_argument('--build-info', required=True)
    normalization.add_argument('--compiler-output', required=True)
    normalization.add_argument('--source', required=True)
    normalization.add_argument('--contract', required=True)
    return command


async def dispatch(args):
    if args.group == 'release':
        return await run_release(args.action, release_config=args.release_config, review_hash=args.review_hash,
                                 rpc_url=args.rpc, checkpoint=args.checkpoint, checkpoint_review=args.checkpoint_review,
                                 checkpoint_review_hash=args.checkpoint_review_hash)
    return inputs.freeze_artifact(j.checked_json(args.build_info), j.checked_json(args.compiler_output), args.source, args.contract)


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        result = asyncio.run(dispatch(args))
        rendered = pretty_json(result)
    except Exception as error:
        # A failure produces no success-shaped stdout. Library diagnostics remain
        # distinguishable from owned release failures at the exception boundary.
        print(str(error), file=sys.stderr)
        return 1
    print(rendered)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
