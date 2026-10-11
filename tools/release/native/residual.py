"""Approved residual retirement reconciliation; no Safe-forwarding bypass."""
import asyncio

from ...lib import abi, release_json as j
from ._shared import (SCHEMAS, UNDEFINED, field, optional, require, bigint, equal,
                      hash_hex, hash_text, event)
from .views import Reader

TERM_NAMES = ['openWindow', 'lockDuration', 'settleTimelockWindow', 'roundCap', 'perAddressCap',
              'minSubscription', 'performanceFeeBps', 'maxLossBps', 'openedAt', 'openDeadline',
              'counterparty', 'feeRecipient']
RESIDUAL_TOPICS = [hash_text(abi.item_signature(item)) for item in SCHEMAS['residualEvents']]


def terms_hash(terms):
    values = ['0x' + format(bigint(value), 'x').rjust(64, '0') for value in terms]
    return hash_hex(abi.encode_parameters([{'type': 'bytes32[12]'}], [values]))


async def observe_residual(config, rpc, block, target, phase, round_index):
    call = Reader(rpc, block)
    terms = await asyncio.gather(*(call(target, name, items=SCHEMAS['residualReads']) for name in TERM_NAMES))
    require(
        terms_hash(terms) == await call(config['addresses']['assertions'], 'roundTermsHash', [target]),
        'RESIDUAL_TERMS_HASH'
    )
    mode = await call(target, 'settleMode', items=SCHEMAS['residualReads'])
    funded = await call(target, 'profitFunded', items=SCHEMAS['residualReads'])
    amount = await call(target, 'settleAmount', items=SCHEMAS['residualReads'])
    managed = await call(target, 'totalManagedAssets')
    backing = await call(config['addresses']['stpros'], 'balanceOf', [target])
    require(backing >= managed + (amount if phase == 2 and mode == 0 and funded else 0), 'RESIDUAL_FUNDED_BACKING')
    return {'phase': phase, 'round': round_index, 'terms': terms}


def require_direct_call(transaction, row):
    destination = optional(transaction, 'to')
    destination = destination.lower() if isinstance(destination, str) else UNDEFINED
    require(
        destination == row['address'],
        'RESIDUAL_ROUND_CALL_EVIDENCE',
        'INCOMPLETE_SAFE_RECOVERY_EVIDENCE: reviewed historical Safe implementation, authority, nonce and execution/receipt proof required; no forwarding-model bypass'
    )


async def replay_residual(before, after, rows, timestamp, post, chain_id, transaction_at):
    round_index, phase, terms, retired = bigint(before['round']), before['phase'], list(before['terms']), False
    for row in rows:
        decoded = event(SCHEMAS['residualEvents'], row)
        name, args = decoded['eventName'], decoded['args']
        if name == 'RoundOpened':
            require(phase == 3 and args['roundIndex'] == round_index + 1 and not retired, 'RESIDUAL_ROUND_SEQUENCE')
            transaction = await transaction_at(row)
            require(
                optional(transaction, 'hash') == row['transactionHash']
                and transaction['blockHash'] == row['blockHash']
                and bigint(transaction['blockNumber']) == bigint(row['blockNumber']),
                'RESIDUAL_ROUND_CALL_EVIDENCE'
            )
            require_direct_call(transaction, row)
            call = abi.decode_function(SCHEMAS['openRoundV1' if chain_id == 1672 else 'openRoundV3'], transaction['input'])
            require(call['functionName'] == 'openNextRound', 'RESIDUAL_ROUND_CALL_EVIDENCE')
            params = call['args'][0]
            for index in range(8):
                terms[index] = params[TERM_NAMES[index]]
            require(
                args['roundCap'] == bigint(terms[3])
                and args['openDeadline'] == args['openedAt'] + bigint(terms[0])
                and (chain_id != 1672 or params['openedAt'] == 0 or equal(params['openedAt'], args['openedAt'])),
                'RESIDUAL_ROUND_PARAMETERS'
            )
            round_index, phase = args['roundIndex'], 0
            terms[8], terms[9] = args['openedAt'], args['openDeadline']
        else:
            if field(args, 'roundIndex') is not UNDEFINED:
                require(equal(args['roundIndex'], round_index), 'RESIDUAL_ROUND_SEQUENCE')
            if name == 'OpenPeriodExtended':
                require(
                    phase == 0
                    and args['previousOpenDeadline'] == bigint(terms[9])
                    and args['newOpenDeadline'] > args['previousOpenDeadline'],
                    'RESIDUAL_EXTENSION'
                )
                terms[9] = args['newOpenDeadline']
            elif name == 'RoundLocked':
                require(phase == 0 and args['lockedAt'] == bigint(terms[9]), 'RESIDUAL_LOCK_SEQUENCE'); phase = 1
            elif name == 'SettlementProposed':
                require(phase in (0, 1, 2), 'RESIDUAL_PROPOSAL_SEQUENCE'); phase = 2
            elif name == 'SettlementCancelled':
                require(phase == 2, 'RESIDUAL_CANCEL_SEQUENCE'); phase = 1
            elif name == 'SettlementFinalized':
                require(phase == 2, 'RESIDUAL_FINALIZE_SEQUENCE'); phase = 3
            elif name == 'Retired':
                require(post and not retired, 'RESIDUAL_RETIRE_SEQUENCE'); phase, retired = 3, True
            else:
                require(name == 'EmergencyCancelled' or (post and name == 'ParSettlementForced'), 'RESIDUAL_EVENT'); phase = 3
    if phase == 0 and timestamp >= bigint(terms[9]):
        phase = 1
    require(round_index == bigint(after['round']) and equal(phase, after['phase']), 'RESIDUAL_STATE_UNEXPLAINED')
    for index, term in enumerate(terms):
        require(bigint(term) == bigint(after['terms'][index]), 'RESIDUAL_TERMS_UNEXPLAINED')
    require(not post or retired, 'RESIDUAL_RETIRE_EVENT_MISSING')
    return UNDEFINED
