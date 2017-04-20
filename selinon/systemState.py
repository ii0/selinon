#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# ######################################################################
# Copyright (C) 2016-2017  Fridolin Pokorny, fridolin.pokorny@gmail.com
# This file is part of Selinon project.
# ######################################################################
"""
Main system actions done by Selinon
"""

from collections import deque
import copy
import datetime
from functools import reduce
import itertools
import json

from celery.result import AsyncResult

from .config import Config
from .errors import CacheMissError
from .errors import FlowError
from .lockPool import LockPool
from .selective import compute_selective_run
from .selinonTaskEnvelope import SelinonTaskEnvelope
from .storagePool import StoragePool
from .trace import Trace


class SystemState(object):  # pylint: disable=too-many-instance-attributes
    """
    Main system actions done by Selinon
    """
    # Throttled nodes in the current worker: node name -> next schedule time
    # Throttle should be safe with concurrency
    _throttle_lock_pool = LockPool()
    _throttled_tasks = {}
    _throttled_flows = {}
    _node_state_cache_lock = LockPool()

    @property
    def node_args(self):
        """
        :return: arguments for a node
        """
        return self._node_args

    def _get_async_result(self, node_name, node_id):  # pylint: disable=invalid-name,redefined-builtin
        cache = Config.async_result_cache[self._flow_name]
        trace_msg = {
            'flow_name': self._flow_name,
            'node_args': self._node_args,
            'parent': self._parent,
            'dispatcher_id': self._dispatcher_id,
            'queue': Config.dispatcher_queues[self._flow_name],
            'node_id': node_id,
            'node_name': node_name,
            'selective': self._selective
        }

        with self._node_state_cache_lock.get_lock(self._flow_name):
            try:
                Trace.log(Trace.NODE_STATE_CACHE_GET, trace_msg)
                res = cache.get(node_id)
                Trace.log(Trace.NODE_STATE_CACHE_HIT, trace_msg)
            except CacheMissError:
                Trace.log(Trace.NODE_STATE_CACHE_MISS, trace_msg)
                res = AsyncResult(id=node_id)
                # we can cache only results of tasks that have finished or failed, not the ones that are going to
                # be processed
                if res.successful() or res.failed():
                    Trace.log(Trace.NODE_STATE_CACHE_ADD, trace_msg)
                    cache.add(node_id, res)

        return res

    def _instantiate_active_nodes(self, arr):
        """
        :return: convert node references from argument to AsyncResult
        """
        return [{'name': node['name'], 'id': node['id'],
                 'result': self._get_async_result(node['name'], node['id'])} for node in arr]

    @staticmethod
    def _deinstantiate_active_nodes(arr):
        """
        :return: node references for Dispatcher arguments
        """
        return [{'name': x['name'], 'id': x['id']} for x in arr]

    @staticmethod
    def _idxs2items(edge_node_table, arr):
        # we keep only the index to edges_table in order to avoid serialization and optimize number representation
        ret = []
        for i in arr:
            ret.append(edge_node_table[i])
        return ret

    def __repr__(self):
        # Make tests more readable
        return str(self.to_dict())

    def __init__(self, dispatcher_id, flow_name, node_args=None, retry=None, state=None, parent=None, selective=None):
        # pylint: disable=too-many-arguments
        state_dict = state or {}

        self._dispatcher_id = dispatcher_id
        self._flow_name = flow_name
        self._node_args = node_args
        self._parent = parent or {}
        self._selective = selective or False
        self._active_nodes = self._instantiate_active_nodes(state_dict.get('active_nodes', []))
        self._finished_nodes = state_dict.get('finished_nodes', {})
        self._failed_nodes = state_dict.get('failed_nodes', {})
        self._waiting_edges_idx = state_dict.get('waiting_edges', [])
        # Instantiate lazily later if we will know that there is something to process
        self._waiting_edges = []
        self._retry = retry

        # TODO: fix this - for some reasons serializer uses strings in values
        if self._selective:
            for flow in self._selective['waiting_edges_subset']:
                self._selective['waiting_edges_subset'][flow] = \
                    {int(k): v for k, v in self._selective['waiting_edges_subset'][flow].items()}

    def to_dict(self):
        """
        :return: converted system state to dict
        """
        return {
            'active_nodes': self._deinstantiate_active_nodes(self._active_nodes),
            'finished_nodes': self._finished_nodes,
            'failed_nodes': self._failed_nodes,
            'waiting_edges': self._waiting_edges_idx
        }

    @property
    def selective(self):
        """ Edges that should be started selectively as computed by compute_selective """
        return self._selective

    def _get_countdown(self, node_name, is_flow):
        """
        Get countdown for throttling

        :param node_name: node name
        :param is_flow: true if node_name is a flow
        :return: countdown seconds for the current schedule
        """
        if is_flow:
            throttle_conf = Config.throttle_flows
            throttled_nodes = self._throttled_flows
        else:
            throttle_conf = Config.throttle_tasks
            throttled_nodes = self._throttled_tasks

        with self._throttle_lock_pool.get_lock(node_name):
            if throttle_conf[node_name]:
                current_datetime = datetime.datetime.now()
                if node_name not in throttled_nodes:
                    # we throttle for the first time
                    throttled_nodes[node_name] = current_datetime
                    return None

                next_run = current_datetime + throttle_conf[node_name]
                countdown = (throttled_nodes[node_name] + throttle_conf[node_name] - current_datetime).total_seconds()
                throttled_nodes[node_name] = next_run

                return countdown if countdown > 0 else None
            else:
                return None

    def _get_successful_and_failed(self, reused):
        """
        :return: all successful and failed nodes in system from active nodes
        """
        ret_successful = reused
        ret_failed = []

        new_active_nodes = []
        for node in self._active_nodes:
            if node['result'].successful():
                Trace.log(Trace.NODE_SUCCESSFUL, {'flow_name': self._flow_name,
                                                  'dispatcher_id': self._dispatcher_id,
                                                  'node_name': node['name'],
                                                  'node_id': node['id'],
                                                  'selective': self._selective})
                ret_successful.append(node)
            elif node['result'].failed():
                Trace.log(Trace.NODE_FAILURE, {'flow_name': self._flow_name,
                                               'dispatcher_id': self._dispatcher_id,
                                               'node_name': node['name'],
                                               'node_id': node['id'],
                                               'what': node['result'].traceback,
                                               'selective': self._selective})
                # We keep track of failed nodes to handle failures once all nodes finish
                if node['name'] not in self._failed_nodes:
                    self._failed_nodes[node['name']] = []
                self._failed_nodes[node['name']].append(node['id'])
                ret_failed.append(node['id'])
            else:
                new_active_nodes.append(node)

        self._active_nodes = new_active_nodes
        return ret_successful, ret_failed

    def _execute_selective_run_func(self, node_name, node_args, parent):
        trace_msg = {
            'flow_name': self._flow_name,
            'dispatcher_id': self._dispatcher_id,
            'selective': self._selective,
            'node_name': node_name,
            'node_args': node_args,
            'parent': parent,
        }

        storage_pool = StoragePool(parent, self._flow_name)
        selective_func = Config.selective_run_task[node_name]

        result = selective_func(self._flow_name, node_name, node_args, self._selective['task_names'], storage_pool)
        Trace.log(Trace.SELECTIVE_RUN_FUNC, trace_msg, {'result': result})

        return result

    def _start_node(self, node_name, parent, node_args, edge=None, force_propagate_node_args=False):
        """
        Start a node in the system

        :param node_name: name of a node to be started
        :param parent: parent nodes of the starting node
        :param node_args: arguments for the starting node
        :param edge: edge that triggered node start, can be None for fallbacks
        """
        # pylint: disable=too-many-arguments
        from .dispatcher import Dispatcher

        if Config.is_flow(node_name):
            start_node_args = None
            if force_propagate_node_args or Config.should_propagate_node_args(self._flow_name, node_name):
                start_node_args = node_args

            start_parent = None
            if Config.should_propagate_parent(self._flow_name, node_name):
                start_parent = parent

            selective = None
            if edge and edge.get('selective'):
                selective = compute_selective_run(node_name, **edge['selective'])

            kwargs = {
                'flow_name': node_name,
                'node_args': start_node_args,
                'parent': start_parent,
                'selective': selective or self.selective
            }

            countdown = self._get_countdown(node_name, is_flow=True)
            async_result = Dispatcher().apply_async(kwargs=kwargs,
                                                    queue=Config.dispatcher_queues[node_name],
                                                    countdown=countdown)

            Trace.log(Trace.SUBFLOW_SCHEDULE, {
                'flow_name': self._flow_name,
                'condition_str': None if not edge else edge['condition_str'],
                'foreach_str': None if not edge else edge.get('foreach_str'),
                'selective_edge_conf': None if not edge else edge.get('selective', False),
                'child_flow_name': node_name,
                'dispatcher_id': self._dispatcher_id,
                'child_dispatcher_id': async_result.task_id,
                'queue': Config.dispatcher_queues[node_name],
                'countdown': countdown,
                'child_selective': selective,
                'selective': self._selective,
                'node_args': start_node_args
            })

        else:
            kwargs = {
                'task_name': node_name,
                'flow_name': self._flow_name,
                'parent': parent,
                'node_args': node_args,
                'dispatcher_id': self._dispatcher_id
            }

            countdown = self._get_countdown(node_name, is_flow=False)
            async_result = SelinonTaskEnvelope().apply_async(kwargs=kwargs,
                                                             queue=Config.task_queues[node_name],
                                                             countdown=countdown)

            Trace.log(Trace.TASK_SCHEDULE, kwargs, {
                'task_id': async_result.task_id,
                'queue': Config.task_queues[node_name],
                'condition_str': None if not edge else edge['condition_str'],
                'foreach_str': None if not edge else edge.get('foreach_str'),
                'selective_edge': False,  # always False as we are starting a task
                'countdown': countdown,
                'selective': self._selective
            })

        record = {
            'name': node_name,
            'id': async_result.task_id,
            'result': async_result
        }

        if node_name not in Config.nowait_nodes.get(self._flow_name, []):
            self._active_nodes.append(record)

        return record

    def _fire_edge(self, edge_idx, edge, storage_pool, parent, node_args):
        """ Fire edge - start new nodes as described in edge table

        :param edge: edge that should be fired
        :param storage_pool: storage pool which makes results of previous tasks available
        :param parent: parent nodes
        :param node_args: node arguments
        :return: list of nodes that were scheduled
        """
        # pylint: disable=too-many-arguments,too-many-branches
        started = []
        selective_reuse = []
        trace_msg = {
            'nodes_to': edge['to'],
            'nodes_from': edge['from'],
            'flow_name': self._flow_name,
            'foreach_str': edge.get('foreach_str'),
            'condition_str': edge['condition_str'],
            'parent': parent,
            'node_args': self._node_args,
            'dispatcher_id': self._dispatcher_id,
            'selective': self._selective
        }

        nodes2start = None
        if self._selective:
            if edge_idx not in self._selective['waiting_edges_subset'][self._flow_name].keys():
                Trace.log(Trace.SELECTIVE_OMIT_EDGE, trace_msg)
                return [], []
            else:
                nodes2start = self._selective['waiting_edges_subset'][self._flow_name][edge_idx]

        if 'foreach' in edge:
            iterable = edge['foreach'](storage_pool, node_args)
            Trace.log(Trace.FOREACH_RESULT, trace_msg, {'result': iterable})
            # handle None as well
            if not iterable:
                return started, []
            for res in iterable:
                for node_name in edge['to']:
                    if nodes2start and node_name not in nodes2start:
                        Trace.log(Trace.SELECTIVE_OMIT_NODE, trace_msg, {'omitted_node': node_name})
                        continue

                    if self._selective and node_name not in self._selective['task_names'] \
                            and not Config.is_flow(node_name):
                        selective_run_func_result = self._execute_selective_run_func(node_name, res, parent)
                        if selective_run_func_result:
                            Trace.log(Trace.SELECTIVE_TASK_REUSE, trace_msg, {
                                'task_name': node_name,
                                'task_id': selective_run_func_result
                            })
                            selective_reuse.append({
                                'name': node_name,
                                'id': selective_run_func_result,
                                'result': self._get_async_result(node_name, selective_run_func_result)
                            })
                            continue

                    if edge.get('foreach_propagate_result'):
                        record = self._start_node(node_name, parent, res, edge, force_propagate_node_args=True)
                    else:
                        record = self._start_node(node_name, parent, node_args, edge)
                    started.append(record)
        else:
            for node_name in edge['to']:
                if nodes2start and node_name not in nodes2start:
                    Trace.log(Trace.SELECTIVE_OMIT_NODE, trace_msg, {'omitted_node': node_name})
                    continue

                if self._selective and node_name not in self._selective['task_names'] and not Config.is_flow(node_name):
                    selective_run_func_result = self._execute_selective_run_func(node_name, node_args, parent)
                    if selective_run_func_result:
                        Trace.log(Trace.SELECTIVE_TASK_REUSE, trace_msg, {
                            'task_name': node_name,
                            'task_id': selective_run_func_result
                        })
                        selective_reuse.append({
                            'name': node_name,
                            'id': selective_run_func_result,
                            'result': self._get_async_result(node_name, selective_run_func_result)
                        })
                        continue

                record = self._start_node(node_name, parent, node_args, edge)
                started.append(record)

        return started, selective_reuse

    def _run_fallback(self):
        """ Run fallback in the system

        :return: fallbacks that were run
        """
        # we sort it first to make evaluation dependent on alphabetical order
        ret = []
        failed_nodes = sorted(self._failed_nodes.items())

        # pylint: disable=too-many-nested-blocks
        for i in range(len(failed_nodes), 0, -1):
            for combination in itertools.combinations(failed_nodes, i):
                change = True
                while change:
                    change = False
                    try:
                        failure_nodes = Config.failures[self._flow_name]
                        failure_node = reduce(lambda n, c: n['next'][c[0]], combination[1:],
                                              failure_nodes[combination[0][0]])
                    except KeyError:
                        # such failure not found in the tree of permutations - this means that this
                        # flow will always fail, but run defined fallbacks for defined failures first
                        continue

                    if isinstance(failure_node['fallback'], list) and failure_node['fallback']:
                        traced_nodes_arr = []

                        for node in combination:
                            traced_nodes_arr.append({'name': node[0], 'id': self._failed_nodes[node[0]][0]})
                            self._failed_nodes[node[0]].pop(0)
                            if not self._failed_nodes[node[0]]:
                                del self._failed_nodes[node[0]]

                        Trace.log(Trace.FALLBACK_START, {'flow_name': self._flow_name,
                                                         'dispatcher_id': self._dispatcher_id,
                                                         'nodes': traced_nodes_arr,
                                                         'selective': self._selective,
                                                         'fallback': failure_node['fallback']})

                        for node in failure_node['fallback']:
                            # TODO: parent should be tasks from traced_nodes_arr, we need to compute sub-flow ids
                            record = self._start_node(node, parent=None, node_args=self._node_args)
                            ret.append(record)

                        # wait for fallback to finish in order to avoid time dependent flow evaluation
                        return ret
                    elif failure_node['fallback'] is True:
                        change = True
                        traced_nodes_arr = []
                        for node in combination:
                            traced_nodes_arr.append({'name': node[0], 'id': self._failed_nodes[node[0]][0]})
                            self._failed_nodes[node[0]].pop(0)
                            if not self._failed_nodes[node[0]]:
                                del self._failed_nodes[node[0]]
                                # we have reached zero in failed nodes, we cannot continue with failure
                                # combination otherwise we get KeyError
                                change = False

                        Trace.log(Trace.FALLBACK_START, {'flow_name': self._flow_name,
                                                         'dispatcher_id': self._dispatcher_id,
                                                         'nodes': traced_nodes_arr,
                                                         'selective': self._selective,
                                                         'fallback': True})
                        # continue with fallback in other combinations, nothing started

        return ret

    def _update_waiting_edges(self, nodes):
        """
        Update waiting edges (edges that wait for a node finish) based on node name

        :param node_name: node that will trigger an edge
        """
        res = []

        for node in nodes:
            for idx, edge in enumerate(Config.edge_table[self._flow_name]):
                if node['name'] not in edge['from']:
                    continue

                if idx in self._waiting_edges_idx:
                    continue

                if node['name'] in Config.nowait_nodes.get(self._flow_name, []):
                    continue

                if self._selective and idx not in self._selective['waiting_edges_subset'][self._flow_name]:
                    continue

                res.append(node)
                self._waiting_edges.append(edge)
                self._waiting_edges_idx.append(idx)

        return res

    def _extend_parent_from_flow(self, dst_dict, flow_name, flow_id, key, compound=False):
        # pylint: disable=too-many-arguments,too-many-locals
        """ Compute parent in a flow in case of propagate_parent flag

        :param dst_dict: a dictionary that should be extended with calculated parents
        :param flow_name: a flow name for which propagate_parent is calculated
        :param flow_id: flow identifier as identified in Celery
        :param key: key under which should be result stored
        :param compound: true if propagate_compound was used
        """
        def follow_keys(d, k):
            # pylint: disable=invalid-name,missing-docstring
            if not compound:
                # A key is always a flow name except the last key, which is a task name with list of task ids
                for m_k in k:
                    if m_k not in d:
                        d[m_k] = {}
                    d = d[m_k]
                return d
            else:
                # If we have compound, the key to list of task is flow name.
                # This will allow to use conditions as expected.
                # e.g.:
                #  {'flow1': {'Task1': ['task1_id1', 'task1_id2'],
                #            {'Task2': ['task2_id1', 'task2_id2']}
                if flow_name not in d:
                    d[flow_name] = {}
                return d[flow_name]

        def push_flow(s, p_n_name, flow_result, k):
            # pylint: disable=invalid-name,missing-docstring
            for n_name, n_ids in flow_result[key].items():
                if not compound:
                    shallow_k = copy.copy(k)
                    shallow_k.append(p_n_name)
                    s.append((n_name, n_ids, shallow_k))
                else:
                    s.append((n_name, n_ids, k))

        res = dst_dict

        async_result = self._get_async_result(flow_name, flow_id)
        stack = deque()
        push_flow(stack, flow_name, async_result.result, deque())

        while stack:
            node_name, node_ids, keys = stack.pop()
            if Config.is_flow(node_name):
                for node_id in node_ids:
                    push_flow(stack, node_name, self._get_async_result(node_name, node_id).result, keys)
            else:
                dst = follow_keys(res, keys)

                if node_name not in dst:
                    dst[node_name] = []

                for node_id in node_ids:
                    dst[node_name].append(node_id)

        return res

    def _start_new_from_finished(self, new_finished):  # pylint: disable=too-many-locals
        """ Start new based on finished nodes

        :param new_finished: finished nodes based on which we should start new nodes
        :return: newly started nodes
        """
        # pylint: disable=too-many-nested-blocks,too-many-branches
        new_started_nodes = []
        selective_reuse = []

        if len(new_finished) == 1 and not self._active_nodes and not self._finished_nodes:
            # propagate arguments from newly finished node if configured to do so
            if Config.node_args_from_first.get(self._flow_name, False):
                self._node_args = StoragePool.retrieve(self._flow_name, new_finished[0]['name'], new_finished[0]['id'])

        for node in new_finished:
            # We could optimize this by pre-computing affected edges in pre-generated config file for each
            # node and computing intersection with waiting edges, but let's stick with this solution for now
            edges = []
            for waiting_edges_idx, edge_table_idx in enumerate(self._waiting_edges_idx):
                # we have to store index to edge table, but inspect possible edge fires only in waiting edges
                if node['name'] in self._waiting_edges[waiting_edges_idx]['from']:
                    edges.append((edge_table_idx, self._waiting_edges[waiting_edges_idx]))

            for i, edge in edges:
                from_nodes = dict.fromkeys(edge['from'], [])

                for from_name in from_nodes:
                    if from_name == node['name']:
                        from_nodes[from_name] = [{'name': node['name'], 'id': node['id']}]
                    else:
                        from_nodes[from_name] = [{'name': from_name, 'id': n}
                                                 for n in self._finished_nodes.get(from_name, [])]

                # if there are multiple nodes of a same type waiting on an edge:
                #
                #   A    B
                #    \  /
                #     \/
                #     C
                #
                # A: <id1>, <id2>
                # B: <id3>, <id4>
                #
                # Compute all combinations: (<id1>, <id3>), (<id1>, <id4>), (<id2>, <id3>), (<id2>, <id4>)
                #
                for start_nodes in itertools.product(*from_nodes.values()):
                    parent = {}
                    storage_id_mapping = {}

                    for start_node in start_nodes:
                        if Config.is_flow(start_node['name']):
                            compound = Config.should_propagate_compound_finished(self._flow_name,
                                                                                 start_node['name'])
                            if Config.should_propagate_finished(self._flow_name, start_node['name']) or compound:
                                parent = self._extend_parent_from_flow(parent, start_node['name'], start_node['id'],
                                                                       'finished_nodes', compound)
                        else:
                            parent[start_node['name']] = start_node['id']
                            storage_id_mapping[start_node['name']] = start_node['id']

                    # We could also examine results of subflow, there could be passed a list of subflows with
                    # finished_nodes to 'condition' in order to do inspection
                    storage_pool = StoragePool(storage_id_mapping, self._flow_name)
                    if edge['condition'](storage_pool, self._node_args):
                        records, reused = self._fire_edge(i, edge, storage_pool,
                                                          parent=parent, node_args=self._node_args)
                        new_started_nodes.extend(records)
                        selective_reuse.extend(reused)
                    else:
                        Trace.log(Trace.EDGE_COND_FALSE, {
                            'nodes_to': edge['to'],
                            'nodes_from': edge['from'],
                            'condition': edge['condition_str'],
                            'flow_name': self._flow_name,
                            'parent': parent,
                            'node_args': self._node_args,
                            'dispatcher_id': self._dispatcher_id,
                            'selective': self._selective
                        })

            node_name = node['name']
            if not self._finished_nodes.get(node_name):
                self._finished_nodes[node_name] = []
            self._finished_nodes[node_name].append(node['id'])

        return new_started_nodes, selective_reuse

    def _start_and_update_retry(self):
        """ Start the flow and update retry

        :return: new/next retry
        """
        new_started_nodes = []
        selective_reuse = []

        Trace.log(Trace.FLOW_START, {'flow_name': self._flow_name,
                                     'dispatcher_id': self._dispatcher_id,
                                     'queue': Config.dispatcher_queues[self._flow_name],
                                     'selective': self._selective,
                                     'node_args': self._node_args})

        for i, start_edge in Config.get_starting_edges(self._flow_name):
            storage_pool = StoragePool(self._parent, self._flow_name)
            if start_edge['condition'](storage_pool, self._node_args):
                records, reused = self._fire_edge(i, start_edge, storage_pool,
                                                  node_args=self._node_args, parent=self._parent)

                new_started_nodes.extend(records)
                selective_reuse.extend(reused)
            else:
                Trace.log(Trace.EDGE_COND_FALSE, {
                    'nodes_to': start_edge['to'],
                    'nodes_from': start_edge['from'],
                    'condition': start_edge['condition_str'],
                    'flow_name': self._flow_name,
                    'parent': self._parent,
                    'node_args': self._node_args,
                    'dispatcher_id': self._dispatcher_id,
                    'selective': self._selective
                })

        # Update here so the strategy has correct new_started_nodes without nowait
        new_started_nodes = self._update_waiting_edges(new_started_nodes)
        return new_started_nodes, selective_reuse

    def _continue_and_update_retry(self, previously_reused):
        """ Continue with the flow based on previous runs

        :return: tuple describing newly started and reused nodes by selective run
        """
        new_started_nodes = []
        selective_reuse = []
        fallback_started = []

        new_finished, new_failed = self._get_successful_and_failed(previously_reused)

        if new_finished or new_failed:
            # Instantiate results lazily
            self._waiting_edges = self._idxs2items(Config.edge_table[self._flow_name], self._waiting_edges_idx)
            self._update_waiting_edges(new_finished)

            new_started_nodes, selective_reuse = self._start_new_from_finished(new_finished)

            # We wait until all active nodes finish if there are some failed nodes try to recover from failure,
            # otherwise mark flow as failed
            if not self._active_nodes and self._failed_nodes:
                fallback_started = self._run_fallback()
                if not fallback_started and self._failed_nodes:
                    # We will propagate state information so we can correctly propagate parent and failed nodes in
                    # parent flow if there is any
                    # We use JSON in order to use result backend with JSON configured so objects are not pickled
                    state_dict = self.to_dict()
                    state_info = {
                        'finished_nodes': state_dict['finished_nodes'],
                        'failed_nodes': state_dict['failed_nodes']
                    }
                    raise FlowError(json.dumps(state_info))

        return new_started_nodes, selective_reuse, fallback_started

    def update(self):
        """ Check the current state in the system and start new nodes if possible

        :return: retry count - can be None (do not retry dispatcher) or time in seconds to retry
        """
        fallback_started = []

        if not self._active_nodes and not self._finished_nodes and not self._waiting_edges and not self._failed_nodes:
            # we are starting up
            started, reused = self._start_and_update_retry()
        else:
            started, reused, fallback_started = self._continue_and_update_retry([])

        while reused:
            # We do not need to retry if there are some tasks that we can continue with
            started, reused, fallback_started = self._continue_and_update_retry(reused)

        self._retry = Config.strategies[self._flow_name]({
            'previous_retry': self._retry,
            'active_nodes': self._active_nodes,
            'failed_nodes': self._failed_nodes,
            'new_started_nodes': started,
            'new_fallback_nodes': fallback_started,
            'finished_nodes': self._finished_nodes
        })

        return self._retry
