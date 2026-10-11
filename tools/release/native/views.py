"""Pinned contract reads and ordered log access shared by observers."""
from . import rpc as transport
from ...lib.release_json import truthy
from ._shared import SCHEMAS, UNDEFINED, require, bigint, number, call_data, decode_result


class Reader:
    def __init__(self, rpc, block):
        self.rpc, self.block = rpc, block

    async def __call__(self, target, name, args=(), items=None):
        items = SCHEMAS['reads'] if items is None else items
        data = await self.rpc('eth_call', [{'to': target, 'data': call_data(items, name, args)}, self.block])
        return decode_result(items, name, data)


async def pinned(config, rpc, tag='latest'):
    return await transport.pinned(config['chainId'], rpc, tag)


def log_order(row):
    return bigint(row['blockNumber']), bigint(row['logIndex'])


async def range_logs(rpc, target, from_block, to_block, topics):
    if hasattr(rpc, 'history_logs'):
        return rpc.history_logs(target, from_block, to_block, topics)
    result = []
    for start in range(int(from_block), int(to_block) + 1, 1000):
        end = min(start + 999, to_block)
        rows = await rpc(
            'eth_getLogs',
            [{'address': target, 'fromBlock': hex(start), 'toBlock': hex(int(end)), 'topics': [topics]}]
        )
        for row in rows:
            require(
                not truthy(row.get('removed'))
                and row['address'].lower() == target
                and start <= number(bigint(row['blockNumber'])) <= end,
                'LOG_REORG_OR_TARGET'
            )
        result.extend(rows)
    return sorted(result, key=log_order)


async def logs(rpc, target, from_block, to_block, topics):
    require(from_block > 0 and to_block >= from_block, 'HISTORY_RANGE')
    if hasattr(rpc, 'history_logs'):
        return rpc.history_logs(target, from_block, to_block, topics)
    require(await rpc('eth_getCode', [target, hex(int(from_block) - 1)]) == '0x', 'INVENTORY_START_HAS_CODE', target)
    return await range_logs(rpc, target, from_block, to_block, topics)
