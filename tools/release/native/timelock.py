"""Action-bearing Timelock history; a bare replay remains UNATTESTED."""
from ...lib import abi, release_json as j
from ._shared import (SCHEMAS, BigInt, UNDEFINED, ZERO32, field, optional, coalesce,
                      require, bigint, equal, exact_set, hash_hex, hash_text, event)

PINNED_TIMELOCK_RUNTIME_HASH = '0xb884c9eeacf02e5a2d3b461ac21224e263170761d4fd4987cd2378b7136d8764'
OPERATION_COVERAGE_DEFINITION = 'faroo-action-bearing-v1'
OPERATION_TOPICS = [hash_text(abi.item_signature(item)) for item in SCHEMAS['timelockEvents']]


def identity(operation_id, calls, predecessor, salt):
    values = [[c['target'] for c in calls], [c['value'] for c in calls], [c['data'] for c in calls], predecessor, salt]
    batch = hash_hex(
        abi.encode_parameters([{'type': t} for t in ('address[]', 'uint256[]', 'bytes[]', 'bytes32', 'bytes32')], values)
    )
    single = UNDEFINED
    if len(calls) == 1:
        single = hash_hex(
            abi.encode_parameters(
                [{'type': t} for t in ('address', 'uint256', 'bytes', 'bytes32', 'bytes32')],
                [calls[0]['target'], calls[0]['value'], calls[0]['data'], predecessor, salt]
            )
        )
    require(operation_id == batch or operation_id == single, 'TIMELOCK_OPERATION_ID')
    return 'batch' if operation_id == batch else 'single'


def state(timestamp, checkpoint_time=UNDEFINED):
    if timestamp == 0:
        return 'Unset'
    if timestamp == 1:
        return 'Done'
    if checkpoint_time is UNDEFINED:
        return 'Pending'
    return 'Waiting' if timestamp > checkpoint_time else 'Ready'


def same_transaction(left, right):
    return left['transactionHash'] == right['transactionHash'] and left['blockHash'] == right['blockHash']


def require_dependency_done(value, operation_id):
    require(value == 1, 'TIMELOCK_DEPENDENCY_NOT_DONE', operation_id)


async def replay_operations(rows, timestamp_at, get_timestamp, checkpoint_time=UNDEFINED):
    operations, events, seen, episodes, empty_metadata = {}, [], set(), [], {}
    dependencies, executed_dependencies, block_times = {}, set(), {}
    decoded = [{'row': row, **event(SCHEMAS['timelockEvents'], row)} for row in rows]
    action_ids = {entry['args']['id'] for entry in decoded if entry['eventName'] == 'CallScheduled'}
    for index, row in enumerate(rows):
        key = (row['blockHash'], bigint(row['logIndex']))
        require(key not in seen, 'TIMELOCK_DUPLICATE_LOG'); seen.add(key)
        if row['blockHash'] not in block_times:
            timestamp = await timestamp_at(row)
            require(isinstance(timestamp, BigInt) and timestamp >= 0, 'TIMELOCK_EVENT_TIMESTAMP')
            block_times[row['blockHash']] = timestamp
        if index:
            previous = rows[index - 1]
            require(
                bigint(row['blockNumber']) > bigint(previous['blockNumber'])
                or (
                    row['blockHash'] == previous['blockHash']
                    and bigint(row['blockNumber']) == bigint(previous['blockNumber'])
                    and bigint(row['logIndex']) > bigint(previous['logIndex'])
                ),
                'TIMELOCK_LOG_ORDER'
            )

    def record(entry):
        row = entry['row']
        events.append(
            {
                'event': entry['eventName'],
                'id': entry['args']['id'],
                'block': row['blockNumber'],
                'logIndex': row['logIndex'],
                'transactionHash': row['transactionHash']
            }
        )

    position = 0
    while position < len(decoded):
        first = decoded[position]; position += 1
        row, kind, args = first['row'], first['eventName'], first['args']; operation_id = args['id']
        record(first); prior = operations.get(operation_id)
        if kind == 'CallScheduled':
            require(prior is None or prior['status'] == 'cancelled', 'TIMELOCK_RESCHEDULE_HISTORY')
            group = [args]
            while position < len(decoded) and decoded[position]['eventName'] == kind and decoded[position]['args']['id'] == operation_id and same_transaction(row, decoded[position]['row']):
                record(decoded[position]); group.append(decoded[position]['args']); position += 1
            require(all(equal(value['index'], bigint(i)) for i, value in enumerate(group)), 'TIMELOCK_CALL_INDICES')
            require(
                all(
                    equal(value['delay'], args['delay'])
                    and value['predecessor'] == args['predecessor'] for value in group
                ),
                'TIMELOCK_BATCH_METADATA'
            )
            salt = ZERO32
            next_event = decoded[position] if position < len(decoded) else None
            if next_event is not None and next_event['eventName'] == 'CallSalt' and next_event['args']['id'] == operation_id and same_transaction(row, next_event['row']):
                require(next_event['args']['salt'] != ZERO32, 'TIMELOCK_SALT_HISTORY')
                salt = next_event['args']['salt']; record(next_event); position += 1
            calls = [{'target': item['target'].lower(), 'value': item['value'], 'data': item['data']} for item in group]
            shape = identity(operation_id, calls, args['predecessor'], salt)
            scheduled_at = block_times[row['blockHash']]; timestamp = bigint(scheduled_at + args['delay'])
            require(timestamp > 1, 'TIMELOCK_SCHEDULE_TIMESTAMP')
            operation = {
                'id': operation_id,
                'status': 'pending',
                'timestamp': timestamp,
                'calls': calls,
                'shape': shape,
                'predecessor': args['predecessor'],
                'salt': salt,
                'delay': args['delay'],
                'scheduledAt': scheduled_at,
                'readyAt': timestamp,
                'scheduleTransaction': row['transactionHash'],
                'executionCount': 0
            }
            if prior is not None and j.truthy(field(prior, 'executionCount')):
                require(
                    prior['executionTransaction'] == row['transactionHash']
                    and prior['executionBlockHash'] == row['blockHash']
                    and j.canonical(prior['calls']) == j.canonical(calls),
                    'TIMELOCK_PARTIAL_EXECUTION'
                )
                for key in ('executionCount', 'executionTransaction', 'executionBlockHash'):
                    operation[key] = prior[key]
            operations[operation_id] = operation; episodes.append(operation)
            if args['predecessor'] != ZERO32:
                dependencies[args['predecessor']] = None
        elif kind == 'CallExecuted':
            require(
                optional(prior, 'status') == 'pending'
                and equal(args['index'], bigint(prior['executionCount']))
                and args['index'] < bigint(len(prior['calls'])),
                'TIMELOCK_EXECUTION_HISTORY'
            )
            call = {'target': args['target'].lower(), 'value': args['value'], 'data': args['data']}
            require(
                j.canonical(call) == j.canonical(prior['calls'][prior['executionCount']]),
                'TIMELOCK_EXECUTION_HISTORY'
            )
            require(
                not j.truthy(field(prior, 'executionTransaction'))
                or (
                    prior['executionTransaction'] == row['transactionHash']
                    and prior['executionBlockHash'] == row['blockHash']
                ),
                'TIMELOCK_PARTIAL_EXECUTION'
            )
            require(block_times[row['blockHash']] >= prior['timestamp'], 'TIMELOCK_EXECUTED_EARLY')
            if prior['predecessor'] != ZERO32:
                predecessor = operations.get(prior['predecessor'])
                require(
                    prior['predecessor'] not in action_ids
                    or optional(predecessor, 'status') == 'done',
                    'TIMELOCK_PREDECESSOR_ORDER'
                )
                executed_dependencies.add(prior['predecessor'])
            prior['executionTransaction'], prior['executionBlockHash'] = row['transactionHash'], row['blockHash']
            prior['executionCount'] += 1
            if prior['executionCount'] == len(prior['calls']):
                prior['status'], prior['timestamp'] = 'done', bigint(1)
        elif kind == 'Cancelled' and operation_id in action_ids:
            require(optional(prior, 'status') == 'pending', 'TIMELOCK_CANCEL_HISTORY')
            prior['status'], prior['timestamp'] = 'cancelled', bigint(0)
        else:
            require(operation_id not in action_ids, 'TIMELOCK_SALT_HISTORY')
            empty = empty_metadata.get(operation_id, {'events': []})
            if kind == 'CallSalt':
                require(
                    args['salt'] != ZERO32
                    and (not j.truthy(field(empty, 'salt')) or empty['salt'] == args['salt'])
                    and field(empty, 'lastEvent') != 'CallSalt',
                    'TIMELOCK_SALT_HISTORY'
                )
                empty['salt'] = args['salt']
            else:
                require(kind == 'Cancelled', 'TIMELOCK_EMPTY_METADATA')
            empty['lastEvent'] = kind
            empty['events'].append({'event': kind, 'transactionHash': row['transactionHash'], 'block': row['blockNumber']})
            empty_metadata[operation_id] = empty
    observed = {}

    async def timestamp(operation_id, code):
        if operation_id not in observed:
            value = await get_timestamp(operation_id)
            require(isinstance(value, BigInt) and value >= 0, code, operation_id)
            observed[operation_id] = value
        return observed[operation_id]

    for operation_id, operation in operations.items():
        require(operation['executionCount'] == 0 or operation['status'] == 'done', 'TIMELOCK_PARTIAL_EXECUTION')
        require(
            equal(await timestamp(operation_id, 'TIMELOCK_OPERATION_STATE'), operation['timestamp']),
            'TIMELOCK_OPERATION_STATE',
            operation_id
        )
        operation['currentState'] = state(operation['timestamp'], checkpoint_time)
    predecessor_states = {}
    for operation_id in dependencies:
        value = await timestamp(operation_id, 'TIMELOCK_DEPENDENCY_STATE')
        if operation_id in executed_dependencies:
            require_dependency_done(value, operation_id)
        predecessor_states[operation_id] = {
            'timestamp': value,
            'state': state(value, checkpoint_time),
            'done': value == 1,
            'actionBearing': operation_id in action_ids
        }
    for operation_id, empty in empty_metadata.items():
        empty['timestamp'] = await timestamp(operation_id, 'TIMELOCK_EMPTY_STATE')
        empty['currentState'] = state(empty['timestamp'], checkpoint_time)
        empty['historyCoverage'] = 'NOT_CLAIMED — silent empty schedule/execute transitions'
    return {
        'definition': OPERATION_COVERAGE_DEFINITION,
        'replayStatus': 'PASS',
        'actionBearingCoverage': 'UNATTESTED',
        'dependencyVerification': 'PASS',
        'literalAllOperationIds': 'NOT_CLAIMED',
        'events': events,
        'episodes': episodes,
        'operations': operations,
        'predecessorStates': predecessor_states,
        'emptyMetadata': empty_metadata,
        'pending': sorted(key for key, op in operations.items() if op['status'] == 'pending')
    }


def check_operation_plan(inventory, expected_pending, done_id=UNDEFINED):
    exact_set(inventory['pending'], expected_pending, 'TIMELOCK_OUTSTANDING_OPERATIONS')
    if j.truthy(done_id):
        require(optional(inventory, 'operations', done_id, 'status') == 'done', 'TIMELOCK_RELEASE_NOT_EXECUTED')
    for operation_id in [*expected_pending, *([done_id] if j.truthy(done_id) else [])]:
        require(
            optional(inventory, 'operations', operation_id, 'predecessor') == ZERO32,
            'TIMELOCK_RELEASE_PREDECESSOR'
        )
    return UNDEFINED


def require_action_bearing_coverage(result):
    import re
    inventory = optional(result, 'timelockOperations')
    manifest_hash = coalesce(optional(result, 'history', 'authenticatedManifestSha256'), '')
    require(
        optional(inventory, 'definition') == OPERATION_COVERAGE_DEFINITION
        and optional(inventory, 'actionBearingCoverage') == 'PASS'
        and optional(inventory, 'replayStatus') == 'PASS'
        and optional(inventory, 'literalAllOperationIds') == 'NOT_CLAIMED'
        and optional(inventory, 'dependencyVerification') == 'PASS'
        and optional(inventory, 'sourceRuntimeHash') == PINNED_TIMELOCK_RUNTIME_HASH
        and isinstance(manifest_hash, str)
        and re.fullmatch(r'[0-9a-f]{64}', manifest_hash)
        and result['history']['through'] >= coalesce(field(result, 'currentBlock'), field(result, 'block')),
        'INCOMPLETE_TIMELOCK_ACTION_COVERAGE'
    )
    return UNDEFINED
