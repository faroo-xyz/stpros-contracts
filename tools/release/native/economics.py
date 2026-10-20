"""Complete holder/withdrawal inventories and ordered economic reconciliation."""
import asyncio
import copy

from ...lib import abi, release_json as j
from ._shared import (SCHEMAS, UNDEFINED, ZERO, field, optional, require, bigint,
                      number, equal, exact_set, hash_text, event, string, integer_divide)
from .views import Reader, logs, range_logs, log_order
from . import inputs, residual

DEPOSIT = hash_text('Deposit(address,address,uint256,uint256)')
WITHDRAW = hash_text('Withdraw(address,address,address,uint256,uint256)')
CLAIM = hash_text('WithdrawalCompleted(address,address,uint256)')
UNBONDING = hash_text('UnbondingPeriodChanged(uint256,uint256)')
TRANSFER = hash_text('Transfer(address,address,uint256)')
POOL_TOPICS = [hash_text(abi.item_signature(item)) for item in SCHEMAS['poolEvents']]


def words(row, count):
    return abi.decode_parameters([{'type': 'uint256'}] * count, row['data'])


async def scan_economics(config, inventory, rpc, block, post=False, migrated=UNDEFINED):
    if migrated is UNDEFINED:
        migrated = post
    call, holders = Reader(rpc, block), {}
    for token in inventory['holders']:
        accounts, ledger = set(), {}
        for row in await logs(rpc, token, inventory['fromBlock'], number(bigint(block)), [TRANSFER]):
            sender, recipient, amount = '0x'+row['topics'][1][-40:], '0x'+row['topics'][2][-40:], bigint(row['data'])
            for account, delta in [(sender, -amount), (recipient, amount)]:
                if account != ZERO:
                    accounts.add(account); ledger[account] = ledger.get(account, 0) + delta

        async def balance_row(account):
            balance = await call(token, 'balanceOf', [account])
            require(balance == ledger[account], 'TRANSFER_REPLAY', token)
            return {'address': account, 'balance': str(balance)}

        rows = await asyncio.gather(*(balance_row(account) for account in sorted(accounts)))
        total = sum(bigint(row['balance']) for row in rows)
        supply = await call(token, 'totalSupply')
        require(total == supply, 'HOLDER_SUM', token)
        holders[token] = {'accounts': rows, 'totalSupply': str(supply)}
        if not post:
            exact_set(
                [a['address'] for a in inventory['holders'][token]['accounts']],
                list(accounts),
                'HOLDER_TRUNCATION'
            )
            for row in rows:
                original = next(a for a in inventory['holders'][token]['accounts'] if a['address'] == row['address'])
                require(bigint(original['balance']) == bigint(row['balance']), 'HOLDER_BALANCE_DRIFT')
    token, owners = config['addresses']['stpros'], set()
    for row in await logs(rpc, token, inventory['fromBlock'], number(bigint(block)), [WITHDRAW]):
        owners.add('0x'+row['topics'][3][-40:])
    items = SCHEMAS['withdrawalsV1' if config['chainId'] == 1672 and not migrated else 'withdrawalsV3']

    async def owner_records(owner):
        head, tail, rows = await asyncio.gather(
            call(token, 'withdrawalHead', [owner]),
            call(token, 'withdrawalTail', [owner]),
            call(token, 'getWithdrawals', [owner], items)
        )
        require(tail - head == len(rows), 'QUEUE_RANGE')
        return {
            'owner': owner,
            'head': str(head),
            'tail': str(tail),
            'records': [{k: str(v) if isinstance(v, abi.BigInt) else v for k, v in row.items()} for row in rows]
        }

    records = await asyncio.gather(*(owner_records(owner) for owner in sorted(owners)))
    pending_sum = sum(bigint(row['pending']) for owner in records for row in owner['records'])
    queued = await call(token, 'queuedWithdrawal'); completed = await call(token, 'completedWithdrawal')
    require(pending_sum == queued - completed, 'QUEUE_SUM')
    if not post:
        require(j.canonical(records) == j.canonical(inventory['withdrawals']['accounts']), 'QUEUE_RECORDS_DRIFT')
    if migrated:
        for factory in config['factories']:
            for vault in [v for v in factory['vaults'] if v['endState'] == 'RETIRED']:
                for holder in holders[vault['address']]['accounts']:
                    require(
                        await call(vault['address'], 'maxRedeem', [holder['address']]) == bigint(holder['balance']),
                        'RETIRED_REDEEM'
                    )
    return {
        'holders': holders,
        'withdrawals': {
            'accounts': records,
            'queued': str(queued),
            'completed': str(completed),
            'sumPending': str(pending_sum)
        }
    }


def empty_pool_nav(tokens, shares):
    return 0 if shares == 0 else tokens


def replay_pool(config, rows, before, after):
    tokens, shares = bigint(before['poolT']), bigint(before['poolV'])
    mint_or_burn = pool_event = share_commission = oracle_commission = None
    same_transaction = lambda left, right: left['transactionHash'] == right['transactionHash'] and left['blockHash'] == right['blockHash']
    addresses = config['addresses']
    for row in rows:
        decoded = event(SCHEMAS['poolEvents'], row); name, args = decoded['eventName'], decoded['args']
        if row['address'] == addresses['stpros'] and name == 'Transfer':
            if args['from'] != ZERO and args['to'] != ZERO:
                continue
            require(
                mint_or_burn is None
                and args['value'] > 0
                and not (args['from'] == ZERO and args['to'] == ZERO),
                'POOL_SHARE_SEQUENCE'
            )
            mint_or_burn = {**row, **args}; continue
        if row['address'] == addresses['oracle']:
            require(args['token'].lower() == addresses['asset'], 'UNKNOWN_POOL_ACTIVITY')
            require(name != 'SetTokenAmount', 'UNREVIEWED_POOL_SETTER')
            if name == 'PoolUpdated':
                require(
                    pool_event is None
                    and mint_or_burn is not None
                    and same_transaction(mint_or_burn, row),
                    'POOL_EVENT_SEQUENCE'
                )
                pool_event = {**row, **args}; continue
            if name == 'CommissionMinted':
                require(
                    oracle_commission is None
                    and share_commission is not None
                    and same_transaction(share_commission, row),
                    'POOL_COMMISSION_SEQUENCE'
                )
                oracle_commission = {**row, **args}; continue
            require(
                name == 'TokenAmountUpdated'
                and args['addedAmount'] > 0
                and shares > 0
                and pool_event is None,
                'POOL_UPDATE_SEQUENCE'
            )
            fee = integer_divide(args['addedAmount'] * bigint(config['observed']['core']['commissionPpm']), 1000000)
            commission_shares = integer_divide(fee * shares, tokens + args['addedAmount'] - fee)
            if commission_shares > 0:
                require(
                    mint_or_burn is not None
                    and share_commission is not None
                    and oracle_commission is not None
                    and same_transaction(oracle_commission, row)
                    and same_transaction(mint_or_burn, row),
                    'POOL_COMMISSION_SEQUENCE'
                )
                require(
                    mint_or_burn['from'] == ZERO
                    and mint_or_burn['to'].lower() == config['observed']['core']['treasury']
                    and mint_or_burn['value'] == commission_shares
                    and share_commission['shares'] == commission_shares
                    and share_commission['caller'].lower() == addresses['oracle']
                    and share_commission['account'].lower() == config['observed']['core']['treasury']
                    and oracle_commission['vToken'].lower() == addresses['stpros']
                    and oracle_commission['account'].lower() == config['observed']['core']['treasury']
                    and oracle_commission['tokenAmount'] == fee
                    and oracle_commission['vTokenAmount'] == commission_shares,
                    'POOL_COMMISSION_ACCOUNTING'
                )
            else:
                require(
                    mint_or_burn is None
                    and share_commission is None
                    and oracle_commission is None,
                    'POOL_UNEXPLAINED_SHARES'
                )
            tokens += args['addedAmount']; shares += commission_shares
            require(args['newTokenAmount'] == tokens, 'POOL_UPDATE_AMOUNT')
            mint_or_burn = share_commission = oracle_commission = None; continue
        require(row['address'] == addresses['stpros'], 'POOL_EVENT_TARGET')
        if name == 'CommissionMinted':
            require(
                mint_or_burn is not None
                and share_commission is None
                and same_transaction(mint_or_burn, row),
                'POOL_COMMISSION_SEQUENCE'
            )
            share_commission = {**row, **args}; continue
        require(
            name in ('Deposit', 'Withdraw')
            and mint_or_burn is not None
            and pool_event is not None
            and same_transaction(mint_or_burn, row)
            and same_transaction(pool_event, row)
            and share_commission is None
            and oracle_commission is None,
            'POOL_EVENT_SEQUENCE'
        )
        mint = name == 'Deposit'
        ownership = mint_or_burn['from'] == ZERO and mint_or_burn['to'].lower() == args['owner'].lower() if mint else mint_or_burn['to'] == ZERO and mint_or_burn['from'].lower() == args['owner'].lower()
        require(
            args['assets'] > 0
            and args['shares'] > 0
            and mint_or_burn['value'] == args['shares']
            and ownership,
            'POOL_SHARE_ACCOUNTING'
        )
        if mint:
            tokens += args['assets']; shares += args['shares']
        else:
            require(tokens >= args['assets'] and shares >= args['shares'], 'POOL_REDEMPTION_BOUNDS')
            tokens -= args['assets']; shares -= args['shares']; tokens = empty_pool_nav(tokens, shares)
        require(
            equal(pool_event['isMint'], mint)
            and pool_event['tokenAmount'] == tokens
            and pool_event['vTokenAmount'] == shares,
            'POOL_EVENT_ACCOUNTING'
        )
        mint_or_burn = pool_event = None
    require(
        mint_or_burn is None
        and pool_event is None
        and share_commission is None
        and oracle_commission is None,
        'POOL_INCOMPLETE_SEQUENCE'
    )
    require(tokens == bigint(after['poolT']) and shares == bigint(after['poolV']), 'UNRECONCILED_POOL')
    return UNDEFINED


def request_receiver(receiver, owner, migrated):
    return ZERO if migrated and receiver == owner else receiver


async def reconcile_economics(config, inventory, rpc, block, before, after, post=False):
    from_block, to_block = config['observed']['blockNumber'] + 1, number(bigint(block))
    if from_block > to_block:
        return UNDEFINED
    upgraded = hash_text('Upgraded(address)')
    # the waiting-period change is part of U only in the V4 mainnet scope; elsewhere the request stays the frozen one
    scoped = inputs.v4_mainnet_scope(config)
    rows = await range_logs(rpc, config['addresses']['stpros'], from_block, to_block,
                            [DEPOSIT, WITHDRAW, CLAIM, upgraded, *([UNBONDING] if scoped else [])])
    legacy_version = config['observed']['proxies'][config['addresses']['stpros']]['version']
    require(
        equal(legacy_version, config['legacy'][config['addresses']['stpros']]['version'])
        and (
            (config['chainId'] == 1672 and equal(legacy_version, 1))
            or (config['chainId'] == 688689 and equal(legacy_version, 2))
        ),
        'CLAIM_EVENT_MODEL'
    )
    migrated = False
    accounts = {
        a['owner']: copy.deepcopy(a, {id(UNDEFINED): UNDEFINED}) for a in before['economic']['withdrawals']['accounts']
    }
    queued, completed = bigint(before['economic']['withdrawals']['queued']), bigint(before['economic']['withdrawals']['completed'])
    # each new request snapshots the waiting period in force when it is queued; only U changes it, once, after the
    # StPROS upgrade, to the V4 policy value (earlier requests keep theirs)
    period, period_changed = bigint(before['core']['unbondingPeriod']), False
    for row in rows:
        if row['topics'][0] == UNBONDING:
            old, new = words(row, 2)
            require(
                post and migrated and not period_changed and old == period
                and new == inputs.target_unbonding_period(config) and new != period,
                'UNREVIEWED_UNBONDING_HISTORY'
            )
            period, period_changed = new, True
            continue
        if row['topics'][0] == upgraded:
            require(
                post
                and not migrated
                and '0x'+row['topics'][1][-40:] == config['implementations']['stpros'],
                'UNREVIEWED_UPGRADE_HISTORY'
            )
            migrated = True
            if config['chainId'] == 1672:
                for account in accounts.values():
                    for record in account['records']:
                        record['receiver'] = ZERO
            continue
        if row['topics'][0] == DEPOSIT:
            continue
        withdrawal = row['topics'][0] == WITHDRAW
        owner = '0x' + row['topics'][3 if withdrawal else 1][-40:]
        if owner not in accounts:
            accounts[owner] = {'owner': owner, 'head': '0', 'tail': '0', 'records': []}
        account = accounts[owner]
        amount = words(row, 2 if withdrawal else 1)[0]
        if withdrawal:
            header = await rpc('eth_getBlockByNumber', [row['blockNumber'], False])
            require(optional(header, 'hash') == row['blockHash'], 'ECONOMIC_EVENT_BLOCK')
            record = {
                'queued': str(queued),
                'pending': str(amount),
                'createdAt': str(bigint(header['timestamp'])),
                'unbondingPeriod': str(period)
            }
            if config['chainId'] == 688689 or migrated:
                record['receiver'] = request_receiver('0x'+row['topics'][2][-40:], owner, migrated)
            account['records'].append(record); account['tail'] = str(bigint(account['tail']) + 1); queued += amount
        else:
            aggregate, receiver = legacy_version == 1 and not migrated, '0x'+row['topics'][2][-40:]
            require(len(account['records']) > 0, 'CLAIM_WITHOUT_RECORD')
            require(amount > 0, 'CLAIM_AMOUNT')
            require(receiver != ZERO, 'CLAIM_RECEIVER')
            header = await rpc('eth_getBlockByNumber', [row['blockNumber'], False])
            require(optional(header, 'hash') == row['blockHash'], 'ECONOMIC_EVENT_BLOCK')
            remaining = amount
            while remaining > 0:
                require(len(account['records']) > 0, 'CLAIM_AMOUNT')
                record = account['records'][0]; pending = bigint(record['pending'])
                require(pending > 0 and (remaining >= pending if aggregate else remaining <= pending), 'CLAIM_AMOUNT')
                require(
                    not j.truthy(field(record, 'receiver'))
                    or record['receiver'] == ZERO
                    or receiver == record['receiver'],
                    'CLAIM_RECEIVER'
                )
                require(
                    bigint(header['timestamp']) >= bigint(record['createdAt']) + bigint(record['unbondingPeriod']),
                    'CLAIM_UNBONDING'
                )
                paid = pending if aggregate else remaining; remaining -= paid
                if paid == pending:
                    account['records'].pop(0); account['head'] = str(bigint(account['head']) + 1)
                else:
                    record['pending'] = str(pending - paid); record['queued'] = str(bigint(record['queued']) + paid)
            completed += amount
    require(
        queued == bigint(after['economic']['withdrawals']['queued'])
        and completed == bigint(after['economic']['withdrawals']['completed']),
        'UNRECONCILED_QUEUE_TOTALS'
    )
    expected = sorted(accounts.values(), key=lambda a: a['owner'])
    require(
        j.canonical(expected) == j.canonical(after['economic']['withdrawals']['accounts']),
        'UNRECONCILED_WITHDRAWAL_RECORDS'
    )
    if config['chainId'] == 688689:
        streams = await asyncio.gather(
            *(
                range_logs(rpc, target, from_block, to_block, POOL_TOPICS) for target in [config['addresses']['stpros'], config['addresses']['oracle']]
            )
        )
        pool_rows = sorted([row for stream in streams for row in stream], key=log_order)
        replay_pool(config, pool_rows, before['core'], after['core'])
    for factory in config['factories']:
        for vault in factory['vaults']:
            managed = before['vaultAssets'][vault['address']]
            is_residual = inputs.residual_vault(config, vault)
            finalized = hash_text('SettlementFinalized(uint256,uint8,uint256,uint256,uint256,uint256)')
            vault_rows = await range_logs(
                rpc,
                vault['address'],
                from_block,
                to_block,
                [DEPOSIT, WITHDRAW, *(residual.RESIDUAL_TOPICS if is_residual else [])]
            )
            for row in vault_rows:
                if row['topics'][0] in (DEPOSIT, WITHDRAW):
                    managed += (1 if row['topics'][0] == DEPOSIT else -1) * words(row, 2)[0]
                elif row['topics'][0] == finalized:
                    amount, net_profit, fee, _ = words(row, 4); mode = bigint(row['topics'][2])
                    require(
                        mode <= 1
                        and (net_profit + fee == amount if mode == 0 else net_profit == 0 and fee == 0),
                        'RESIDUAL_SETTLEMENT_ACCOUNTING'
                    )
                    managed += net_profit if mode == 0 else -amount
                require(managed >= 0, 'RESIDUAL_NEGATIVE_ASSETS')
            if is_residual:
                call = Reader(rpc, block)
                phase = number(await call(vault['address'], 'phase')); round_index = await call(vault['address'], 'roundIndex')
                state = optional(after, 'residualStates', vault['address'])
                if state is None or state is UNDEFINED:
                    state = await residual.observe_residual(config, rpc, block, vault['address'], phase, round_index)
                header = await rpc('eth_getBlockByNumber', [block, False])
                async def transaction_at(row):
                    return await rpc('eth_getTransactionByHash', [row['transactionHash']])
                await residual.replay_residual(
                    before['residualStates'][vault['address']],
                    state,
                    [row for row in vault_rows if row['topics'][0] in residual.RESIDUAL_TOPICS],
                    bigint(header['timestamp']),
                    post,
                    config['chainId'],
                    transaction_at
                )
            require(managed == after['vaultAssets'][vault['address']], 'UNRECONCILED_VAULT_ASSETS', vault['address'])
    return UNDEFINED
