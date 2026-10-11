"""Raw-hash-authenticated archives and one invocation-local incremental journal."""
import asyncio
import re

from ...lib import release_json as j
from ._shared import (UNDEFINED, field, optional, coalesce, require, equal, number,
                      bigint, safe_integer, address, unique, hex_number)
from .views import log_order


def history_targets(config):
    d = config['addresses']
    result = [
        d[n] for n in ('oracle', 'stpros', 'bridge', 'distributor', 'proxyAdmin', 'timelock', 'assertions', 'asset')
    ]
    for factory in config['factories']:
        result.extend([factory['address'], factory['beacon'], *[v['address'] for v in factory['vaults']]])
    return sorted(address(x) for x in unique(result))


def integer(value):
    return safe_integer(value) and value >= 0


def full_hash(value):
    return isinstance(value, str) and re.fullmatch(r'0x[0-9a-f]{64}', value) is not None


def raw_hash(value):
    return isinstance(value, str) and re.fullmatch(r'[a-f0-9]{64}', value) is not None


def validate_rows(rows, addresses, from_block, to_block):
    require(isinstance(rows, list), 'HISTORY_ROWS_MISSING')
    seen = set()
    for row in rows:
        require(
            j.truthy(row)
            and not j.truthy(field(row, 'removed'))
            and field(row, 'address') in addresses
            and full_hash(field(row, 'blockHash'))
            and full_hash(field(row, 'transactionHash')),
            'HISTORY_LOG_IDENTITY'
        )
        block = number(bigint(row['blockNumber']))
        topics, data = field(row, 'topics'), field(row, 'data')
        require(
            integer(block)
            and from_block <= block <= to_block
            and bigint(row['logIndex']) >= 0
            and isinstance(topics, list)
            and all(full_hash(t) for t in topics)
            and isinstance(data, str)
            and re.fullmatch(r'0x([0-9a-f]{2})*', data),
            'HISTORY_LOG_RANGE'
        )
        key = (block, bigint(row['logIndex']))
        require(key not in seen, 'HISTORY_DUPLICATE_LOG')
        seen.add(key)


def authenticate_history(config, inventory):
    require(
        j.truthy(field(config, 'historyFile'))
        and raw_hash(optional(config, 'review', 'historySha256')),
        'HISTORY_PROOF_REQUIRED'
    )
    proof = j.checked_json(config['historyFile'], config['review']['historySha256'])
    require(
        field(proof, 'schema') == 'faroo-history-v1'
        and equal(field(proof, 'chainId'), config['chainId'])
        and equal(field(proof, 'blockNumber'), config['observed']['blockNumber'])
        and field(proof, 'blockHash') == config['observed']['blockHash']
        and equal(field(proof, 'fromBlock'), inventory['fromBlock'])
        and field(proof, 'inventorySha256') == config['review']['inventorySha256'],
        'HISTORY_BINDING'
    )
    targets = history_targets(config); coverage = {target: [] for target in targets}; rows, anchors = [], []
    require(
        j.canonical(field(proof, 'targets')) == j.canonical(targets)
        and isinstance(field(proof, 'segments'), list)
        and len(proof['segments']) > 0,
        'HISTORY_TARGETS'
    )
    for segment in proof['segments']:
        require(raw_hash(field(segment, 'planSha256')), 'HISTORY_PLAN_HASH_REQUIRED')
        plan = j.checked_json(segment['planFile'], segment['planSha256'])
        addresses = [address(x['address']) for x in plan['addresses']]
        require(
            equal(field(plan, 'chain_id'), config['chainId'])
            and field(plan, 'chain') == ('mainnet' if config['chainId'] == 1672 else 'testnet')
            and equal(field(plan, 'window'), 1000)
            and integer(field(plan, 'start'))
            and integer(field(plan, 'end'))
            and plan['start'] >= proof['fromBlock']
            and plan['end'] >= plan['start']
            and plan['end'] <= proof['blockNumber'],
            'HISTORY_PLAN_RANGE'
        )
        require(
            len(set(addresses)) == len(addresses)
            and len(addresses) > 0
            and full_hash(field(plan, 'end_block_hash')),
            'HISTORY_PLAN_TARGETS'
        )
        require(plan['filter_id'] == j.historical_filter_id(addresses), 'HISTORY_FILTER')
        require(
            all(a['number'] != plan['end'] or a['hash'] == plan['end_block_hash'] for a in anchors),
            'HISTORY_ANCHOR_CONFLICT'
        )
        anchors.append({'number': plan['end'], 'hash': plan['end_block_hash']})
        for target in targets:
            if target in addresses:
                coverage[target].append([plan['start'], plan['end']])
        next_block = plan['start']
        for ref in segment['chunks']:
            require(raw_hash(field(ref, 'sha256')), 'HISTORY_CHUNK_HASH_REQUIRED')
            chunk = j.checked_json(ref['path'], ref['sha256'])
            require(
                field(chunk, 'chain') == plan['chain']
                and field(chunk, 'filter_id') == plan['filter_id']
                and equal(field(chunk, 'from'), next_block)
                and integer(field(chunk, 'to'))
                and chunk['to'] >= next_block
                and chunk['to'] <= plan['end'],
                'HISTORY_CHUNK_GAP'
            )
            validate_rows(field(chunk, 'logs'), addresses, chunk['from'], chunk['to'])
            window = chunk['from']
            for start, end, count in chunk['windows']:
                require(
                    equal(start, window)
                    and integer(end)
                    and end >= start
                    and end - start < 1000
                    and end <= chunk['to']
                    and integer(count),
                    'HISTORY_WINDOW_GAP'
                )
                require(
                    sum(start <= number(bigint(row['blockNumber'])) <= end for row in chunk['logs']) == count,
                    'HISTORY_WINDOW_COUNT'
                )
                window = end + 1
            require(window == chunk['to'] + 1, 'HISTORY_WINDOW_GAP')
            rows.extend(row for row in chunk['logs'] if row['address'] in targets)
            next_block = chunk['to'] + 1
        require(next_block == plan['end'] + 1, 'HISTORY_CHUNK_GAP')
    for ranges in coverage.values():
        next_block = proof['fromBlock']
        for start, end in sorted(ranges, key=lambda x: x[0]):
            require(start == next_block, 'HISTORY_TARGET_GAP_OR_OVERLAP')
            next_block = end + 1
        require(next_block == proof['blockNumber'] + 1, 'HISTORY_TARGET_GAP_OR_OVERLAP')
    validate_rows(rows, targets, proof['fromBlock'], proof['blockNumber'])
    return {'proof': proof, 'targets': targets, 'rows': sorted(rows, key=log_order), 'anchors': anchors}


class HistorySession:
    """All targets/topics share one journal; only immutable-block reads are cached."""
    def __init__(self, config, authenticated, raw_rpc):
        self.config, self.raw_rpc = config, raw_rpc
        self.proof, self.targets = authenticated['proof'], authenticated['targets']
        self.rows = authenticated['rows']
        self.pins = {a['number']: a['hash'] for a in authenticated['anchors']}
        self.cache, self.tip = {}, None
        self.through, self.log_requests = self.proof['blockNumber'], 0

    @classmethod
    async def create(cls, config, inventory, raw_rpc):
        session = cls(config, authenticate_history(config, inventory), raw_rpc)
        await session.check_pins()
        for target in session.targets:
            require(
                await raw_rpc('eth_getCode', [target, hex_number(session.proof['fromBlock'] - 1)]) == '0x',
                'INVENTORY_START_HAS_CODE',
                target
            )
        return session

    async def __call__(self, method, params):
        if method in ('eth_call', 'eth_getCode', 'eth_getStorageAt') and params[-1] != 'latest':
            key = j.canonical([method, params])
            if key not in self.cache:
                self.cache[key] = asyncio.ensure_future(self.raw_rpc(method, params))
            return await self.cache[key]
        return await self.raw_rpc(method, params)

    def history_logs(self, target, from_block, to_block, topics):
        require(
            target in self.targets
            and from_block >= self.proof['fromBlock']
            and to_block <= self.through,
            'HISTORY_UNCOVERED_RANGE'
        )
        return [
            row for row in self.rows if row['address'] == target
            and from_block <= number(bigint(row['blockNumber'])) <= to_block
            and row['topics']
            and row['topics'][0] in topics
        ]

    async def check_pins(self):
        for block, expected in [*self.pins.items(), *([self.tip] if self.tip is not None else [])]:
            header = await self.raw_rpc('eth_getBlockByNumber', [hex_number(block), False])
            require(optional(header, 'hash') == expected and bigint(header['number']) == bigint(block), 'HISTORY_REORG')

    async def extend(self, head):
        await self.check_pins()
        to_block = number(bigint(head['block']))
        require(to_block >= self.through, 'HISTORY_HEAD_REGRESSION')
        for start in range(int(self.through) + 1, int(to_block) + 1, 1000):
            end = min(start + 999, to_block)
            header = await self.raw_rpc('eth_getBlockByNumber', [hex_number(end), False])
            require(
                j.truthy(optional(header, 'hash'))
                and bigint(header['number']) == bigint(end),
                'HISTORY_WINDOW_HEADER'
            )
            batch = await self.raw_rpc(
                'eth_getLogs',
                [{'address': self.targets, 'fromBlock': hex_number(start), 'toBlock': hex_number(end)}]
            )
            self.log_requests += 1
            validate_rows(batch, self.targets, start, end)
            pins = {number(bigint(row['blockNumber'])): row['blockHash'] for row in batch}
            for block, expected in pins.items():
                current = await self.raw_rpc('eth_getBlockByNumber', [hex_number(block), False])
                require(
                    optional(current, 'hash') == expected
                    and bigint(current['number']) == bigint(block)
                    and all(
                        row['blockHash'] == expected for row in batch if number(bigint(row['blockNumber'])) == block
                    ),
                    'HISTORY_EVENT_REORG'
                )
            require(
                optional(await self.raw_rpc('eth_getBlockByNumber', [hex_number(end), False]), 'hash') == header['hash'],
                'HISTORY_WINDOW_REORG'
            )
            await self.check_pins()
            self.rows.extend(sorted(batch, key=log_order))
            self.tip, self.through = (end, header['hash']), end
        require(
            optional(await self.raw_rpc('eth_getBlockByNumber', [head['block'], False]), 'hash') == head['header']['hash'],
            'HISTORY_HEAD_REORG'
        )
        self.tip = (to_block, head['header']['hash'])

    def statistics(self):
        return {
            'historicalRpcLogRequests': 0,
            'incrementalRpcLogRequests': self.log_requests,
            'through': self.through,
            'authenticatedManifestSha256': self.config['review']['historySha256']
        }
