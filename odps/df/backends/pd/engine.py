#!/usr/bin/env python
# -*- coding: utf-8 -*-
# Licensed to the Apache Software Foundation (ASF) under one
# or more contributor license agreements.  See the NOTICE file
# distributed with this work for additional information
# regarding copyright ownership.  The ASF licenses this file
# to you under the Apache License, Version 2.0 (the
# "License"); you may not use this file except in compliance
# with the License.  You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing,
# software distributed under the License is distributed on an
# "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY
# KIND, either express or implied.  See the License for the
# specific language governing permissions and limitations
# under the License.

import itertools
import time

from .compiler import PandasCompiler
from ..core import Engine
from ..frame import ResultFrame
from ... import DataFrame
from ...expr.core import ExprProxy, ExprDictionary
from ...expr.expressions import *
from ...backends.odpssql.types import df_schema_to_odps_schema, df_type_to_odps_type
from ..errors import CompileError
from ...utils import is_source_collection
from ....utils import init_progress_ui
from ....models import Schema, Partition
from ....errors import ODPSError
from ....types import PartitionSpec
from .... import compat
from ..context import context
from . import analyzer as ana


_replaced_expr = dict()


class PandasEngine(Engine):
    def __init__(self, odps=None, global_optimize=True):
        self._odps = odps

        self._global_optimize = global_optimize

    def _run(self, dag, ui, expr_dag, start_progress=0,
             max_progress=1, close_ui=True):
        ui.status('Try to execute by local pandas...')
        try:
            results = ExprDictionary()
            while True:
                topos = dag.topological_sort()

                no_sub = True
                for node in topos:
                    expr, func = node
                    if expr in results:
                        continue
                    res = func(results)
                    if isinstance(res, tuple):
                        src = expr
                        expr = res[0]
                        res = res[1]
                        no_sub = False
                        results[src] = res
                    results[expr] = res
                if no_sub:
                    break

            ui.update(start_progress+max_progress)

            try:
                return results[expr_dag.root]
            except KeyError as e:
                if len(results) == 1:
                    return compat.lvalues(results)[0]
                raise e
        finally:
            if close_ui:
                ui.close()

    def _optimize(self, expr, src_expr, dag=None):
        if self._global_optimize:
            # we copy the entire ast,
            # and all the modification will be applied to the copied ast.
            if dag is None:
                # the expr is already copied one
                expr = context.register_to_copy_expr(src_expr, expr)
            dag = context.build_dag(src_expr, expr, dag=dag)

            # analyze first
            ana.Analyzer(dag).analyze()

            from ..optimize import Optimizer
            expr = Optimizer(dag).optimize()
        else:
            dag = context.build_dag(src_expr, expr, dag=dag)

        # no rewriter applied here
        return expr, dag

    def _pre_process(self, expr, src_expr=None, dag=None):
        src_expr = src_expr or expr

        if isinstance(expr, Scalar) and expr.name is None:
            expr = expr.rename('__rand_%s' % int(time.time()))

        if isinstance(expr, (Scalar, SequenceExpr)):
            expr = self._convert_table(expr)

        if is_source_collection(expr) and context.is_dag_built(src_expr):
            replaced = expr
        else:
            replaced = _replaced_expr.get(ExprProxy(src_expr))
        if replaced is not None:
            return replaced, dag or context.get_dag(src_expr)

        expr, dag = self._optimize(expr, src_expr, dag=dag)
        _replaced_expr[ExprProxy(src_expr, _replaced_expr)] = expr
        return expr, dag

    def _compile(self, expr, dag):
        backend = PandasCompiler(dag)
        return backend.compile(expr)

    def compile(self, expr):
        expr, dag = self._pre_process(expr)
        return self._compile(expr, dag)

    def _execute(self, expr, ui, expr_dag, src_expr=None, close_ui=True,
                 start_progress=0, max_progress=1, head=None, tail=None):
        src_expr = src_expr or expr

        dag = self._compile(expr, expr_dag)

        df = self._run(dag, ui, expr_dag, start_progress=start_progress,
                       max_progress=max_progress, close_ui=close_ui)

        if not isinstance(src_expr, Scalar):
            src_expr._cache_data = df
            if head:
                values = df.values[:head]
            elif tail:
                values = df.values[-tail:]
            else:
                values = df.values
            return ResultFrame(values, schema=expr_dag.root.schema)
        else:
            res = df.values[0][0]
            src_expr._cache_data = res
            return res

    def execute(self, expr, ui=None, start_progress=0, max_progress=1,
                head=None, tail=None, **kw):
        src_expr = kw.get('src_expr', expr)
        close_ui = ui is None
        ui = ui or init_progress_ui()

        if isinstance(expr, Scalar) and expr.value is not None:
            try:
                ui.update(start_progress+max_progress)
                return expr.value
            finally:
                if close_ui:
                    ui.close()

        expr, expr_dag = self._pre_process(expr, src_expr, dag=kw.get('dag'))

        return self._execute(expr, ui, expr_dag, close_ui=close_ui,
                             start_progress=start_progress, max_progress=max_progress,
                             head=head, tail=tail, src_expr=src_expr)

    @classmethod
    def _convert_pd_type(cls, values, table):
        import pandas as pd

        retvals = []
        for val, t in compat.izip(values, table.schema.types):
            if pd.isnull(val):
                retvals.append(None)
            else:
                retvals.append(val)

        return retvals

    def _write_table_no_partitions(self, frame, table, ui, partition=None,
                                   start_progress=0, max_progress=1):
        def gen():
            df = frame.values
            size = len(df)

            for i, row in zip(itertools.count(), df.values):
                if i % 50 == 0:
                    percent = start_progress + float(i) / size * max_progress
                    ui.update(min(percent, 1))

                yield table.new_record(self._convert_pd_type(row, table))

            ui.update(start_progress+max_progress)

        with table.open_writer(partition=partition) as writer:
            writer.write(gen())

    def _write_table_with_partitions(self, frame, table, partitions, ui,
                                     start_progress=0, max_progress=1):
        df = frame.values
        vals_to_partitions = dict()
        for ps in df[partitions].drop_duplicates().values:
            p = ','.join('='.join([str(n), str(v)]) for n, v in zip(partitions, ps))
            table.create_partition(p)
            vals_to_partitions[tuple(ps)] = p

        size = len(df)
        curr = [0]
        for name, group in df.groupby(partitions):
            name = name if isinstance(name, tuple) else (name, )
            group = group[[it for it in group.columns.tolist() if it not in partitions]]

            def gen():
                for i, row in zip(itertools.count(), group.values):
                    curr[0] += i
                    if curr[0] % 50 == 0:
                        percent = start_progress + float(curr[0]) / size * max_progress
                        ui.update(min(percent, 1))

                    yield table.new_record(self._convert_pd_type(row, table))

            with table.open_writer(partition=vals_to_partitions[name]) as writer:
                writer.write(gen())

    def _write_table(self, frame, table, ui, partitions=None, partition=None,
                     start_progress=0, max_progress=1):
        ui.status('Try to upload to ODPS with tunnel...')
        if partitions is None:
            self._write_table_no_partitions(frame, table, ui, partition=partition,
                                            start_progress=start_progress,
                                            max_progress=max_progress)
        else:
            self._write_table_with_partitions(frame, table, partitions, ui,
                                              start_progress=start_progress,
                                              max_progress=max_progress)

    def _persist(self, expr, name, expr_dag, ui, project=None, partitions=None, partition=None,
                 odps=None, close_ui=True, lifecycle=None, start_progress=0, max_progress=1,
                 execute_percent=0.5, create_table=True, drop_partition=False,
                 create_partition=False, **kwargs):
        src_expr = kwargs.get('src_expr', expr)
        odps = odps or self._odps
        if odps is None:
            raise ODPSError('ODPS entrance should be provided')

        try:
            df = self._execute(expr, ui, expr_dag, start_progress=start_progress,
                               max_progress=max_progress*execute_percent, close_ui=close_ui,
                               src_expr=src_expr)
            schema = Schema(columns=df.columns)

            if partitions is not None:
                if isinstance(partitions, tuple):
                    partitions = list(partitions)
                if not isinstance(partitions, list):
                    partitions = [partitions, ]

                for p in partitions:
                    if p not in schema:
                        raise ValueError(
                                'Partition field(%s) does not exist in DataFrame schema' % p)

                columns = [c for c in schema.columns if c.name not in partitions]
                ps = [Partition(name=t, type=schema.get_type(t)) for t in partitions]
                schema = Schema(columns=columns, partitions=ps)
            elif partition is not None:
                t = self._odps.get_table(name, project=project)
                for col in expr.schema.columns:
                    if col.name.lower() not in t.schema:
                        raise CompileError('Column %s does not exist in table' % col.name)
                    t_col = t.schema[col.name.lower()]
                    if df_type_to_odps_type(col.type) != t_col.type:
                        raise CompileError('Column %s\'s type does not match, expect %s, got %s' % (
                            col.name, t_col.type, col.type))

                if drop_partition:
                    t.delete_partition(partition, if_exists=True)
                if create_partition:
                    t.create_partition(partition, if_not_exists=True)

            if partition is None and create_table:
                schema = df_schema_to_odps_schema(schema)
                table = odps.create_table(name, schema, project=project, lifecycle=lifecycle)
            else:
                table = odps.get_table(name, project=project)
            self._write_table(df, table, ui=ui, partitions=partitions, partition=partition,
                              start_progress=start_progress+max_progress*execute_percent,
                              max_progress=max_progress*(1-execute_percent))

            if partition:
                partition = PartitionSpec(partition)
                filters = []
                for k in partition.keys:
                    filters.append(lambda x: x[k] == partition[k])
                return DataFrame(odps.get_table(name)).filter(*filters)
            return DataFrame(odps.get_table(name))

        finally:
            if close_ui:
                ui.close()

    def persist(self, expr, name, project=None, partitions=None, partition=None,
                odps=None, ui=None, lifecycle=None,
                start_progress=0, max_progress=1, execute_percent=0.5,
                drop_partition=False, create_partition=False, **kwargs):
        close_ui = ui is None
        ui = ui or init_progress_ui()

        src_expr = kwargs.pop('src_expr', expr)
        expr, expr_dag = self._pre_process(expr)

        return self._persist(expr, name, expr_dag, ui, project=project, partitions=partitions,
                             partition=partition, odps=odps, close_ui=close_ui, lifecycle=lifecycle,
                             execute_percent=execute_percent,
                             start_progress=start_progress, max_progress=max_progress,
                             drop_partition=drop_partition, create_partition=create_partition,
                             src_expr=src_expr, **kwargs)

