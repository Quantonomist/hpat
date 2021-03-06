from __future__ import print_function, division, absolute_import
import types as pytypes # avoid confusion with numba.types

import numba
from numba import ir, analysis, types, config, numpy_support
from numba.ir_utils import (mk_unique_var, replace_vars_inner, find_topo_order,
                            dprint_func_ir, remove_dead, mk_alloc)

import numpy as np

import hpat
from hpat import pio_api, pio_lower
import h5py

class PIO(object):
    """analyze and transform hdf5 calls"""
    def __init__(self, func_ir, local_vars):
        self.func_ir = func_ir
        self.local_vars = local_vars
        self.h5_globals = []
        self.h5_file_calls = []
        self.h5_files = {}
        # dset_var -> (f_id, dset_name)
        self.h5_dsets = {}
        self.h5_dsets_sizes = {}
        self.h5_close_calls = {}
        self.h5_create_dset_calls = {}
        self.h5_create_group_calls = {}
        # varname -> 'str'
        self.str_const_table = {}
        self.reverse_copies = {}
        self.tuple_table = {}

    def run(self):
        dprint_func_ir(self.func_ir, "starting IO")
        topo_order = find_topo_order(self.func_ir.blocks)
        for label in topo_order:
            new_body = []
            # copies are collected before running the pass since
            # variables typed in locals are assigned late
            self._get_reverse_copies(self.func_ir.blocks[label].body)
            for inst in self.func_ir.blocks[label].body:
                if isinstance(inst, ir.Assign):
                    inst_list = self._run_assign(inst)
                    new_body.extend(inst_list)
                elif isinstance(inst, ir.StaticSetItem):
                    inst_list = self._run_static_setitem(inst)
                    new_body.extend(inst_list)
                else:
                    new_body.append(inst)
            self.func_ir.blocks[label].body = new_body
        remove_dead(self.func_ir.blocks, self.func_ir.arg_names)
        dprint_func_ir(self.func_ir, "after IO")
        if config.DEBUG_ARRAY_OPT==1:
            print("h5 files: ", self.h5_files)
            print("h5 dsets: ", self.h5_dsets)

    def _run_assign(self, assign):
        lhs = assign.target.name
        rhs = assign.value
        # lhs = h5py
        if (isinstance(rhs, ir.Global) and isinstance(rhs.value, pytypes.ModuleType)
                    and rhs.value==h5py):
            self.h5_globals.append(lhs)
        if isinstance(rhs, ir.Expr):
            # f_call = h5py.File
            if rhs.op=='getattr' and rhs.value.name in self.h5_globals and rhs.attr=='File':
                self.h5_file_calls.append(lhs)
            # f = h5py.File(file_name, mode)
            if rhs.op=='call' and rhs.func.name in self.h5_file_calls:
                self.h5_files[lhs] = rhs.args[0].name
                # parallel arg = False for this stage
                loc = assign.target.loc
                scope = assign.target.scope
                parallel_var = ir.Var(scope, mk_unique_var("$const_parallel"), loc)
                parallel_assign = ir.Assign(ir.Const(0, loc), parallel_var, loc)
                rhs.args.append(parallel_var)
                return [parallel_assign, assign]
            # f.close()
            if rhs.op=='call' and rhs.func.name in self.h5_close_calls:
                return self._gen_h5close(assign,
                                            self.h5_close_calls[rhs.func.name])
            # f.create_dataset("points", (N,), dtype='f8')
            if rhs.op=='call' and rhs.func.name in self.h5_create_dset_calls:
                return self._gen_h5create_dset(assign,
                                    self.h5_create_dset_calls[rhs.func.name])

            # f.create_group("subgroup")
            if rhs.op=='call' and rhs.func.name in self.h5_create_group_calls:
                return self._gen_h5create_group(assign,
                                    self.h5_create_group_calls[rhs.func.name])

            # d = f['dset']
            if rhs.op=='static_getitem' and rhs.value.name in self.h5_files:
                self.h5_dsets[lhs] = (rhs.value, rhs.index_var)
            if rhs.op=='getitem' and rhs.value.name in self.h5_files:
                self.h5_dsets[lhs] = (rhs.value, rhs.index)
            # x = f['dset'][:]
            if rhs.op=='static_getitem' and rhs.value.name in self.h5_dsets:
                return self._gen_h5read(assign.target, rhs)
            # f.close or f.create_dataset
            if rhs.op=='getattr' and rhs.value.name in self.h5_files:
                if rhs.attr=='close':
                    self.h5_close_calls[lhs] = rhs.value
                elif rhs.attr=='create_dataset':
                    self.h5_create_dset_calls[lhs] = rhs.value
                elif rhs.attr=='create_group':
                    self.h5_create_group_calls[lhs] = rhs.value
                elif rhs.attr=='keys':
                    pass
                else:
                    raise NotImplementedError("file operation not supported")
            if rhs.op=='build_tuple':
                self.tuple_table[lhs] = rhs.items
        # handle copies lhs = f
        if isinstance(rhs, ir.Var):
            if rhs.name in self.h5_files:
                self.h5_files[lhs] = self.h5_files[rhs.name]
            if rhs.name in self.str_const_table:
                self.str_const_table[lhs] = self.str_const_table[rhs.name]
            if rhs.name in self.h5_dsets:
                self.h5_dsets[lhs] = self.h5_dsets[rhs.name]
            if rhs.name in self.h5_dsets_sizes:
                self.h5_dsets_sizes[lhs] = self.h5_dsets_sizes[rhs.name]
        if isinstance(rhs, ir.Const) and isinstance(rhs.value, str):
            self.str_const_table[lhs] = rhs.value
        return [assign]

    def _run_static_setitem(self, stmt):
        # generate h5 write code for dset[:] = arr
        if stmt.target.name in self.h5_dsets:
            assert stmt.index==slice(None, None, None)
            f_id, dset_name = self.h5_dsets[stmt.target.name]
            return self._gen_h5write(f_id, stmt.target, stmt.value)
        return [stmt]

    def _gen_h5write(self, f_id, dset_var, arr_var):
        scope = dset_var.scope
        loc = dset_var.loc

        # g_pio_var = Global(hpat.pio_api)
        g_pio_var = ir.Var(scope, mk_unique_var("$pio_g_var"), loc)
        g_pio = ir.Global('pio_api', hpat.pio_api, loc)
        g_pio_assign = ir.Assign(g_pio, g_pio_var, loc)
        # attr call: h5write_attr = getattr(g_pio_var, h5write)
        h5write_attr_call = ir.Expr.getattr(g_pio_var, "h5write", loc)
        attr_var = ir.Var(scope, mk_unique_var("$h5write_attr"), loc)
        attr_assign = ir.Assign(h5write_attr_call, attr_var, loc)
        out = [g_pio_assign, attr_assign]

        # ndims args
        ndims = len(self.h5_dsets_sizes[dset_var.name])
        ndims_var = ir.Var(scope, mk_unique_var("$h5_ndims"), loc)
        ndims_assign = ir.Assign(ir.Const(np.int32(ndims), loc), ndims_var, loc)
        # sizes arg
        sizes_var = ir.Var(scope, mk_unique_var("$h5_sizes"), loc)
        tuple_call = ir.Expr.getattr(arr_var, 'shape',loc)
        sizes_assign = ir.Assign(tuple_call, sizes_var, loc)

        zero_var = ir.Var(scope, mk_unique_var("$const_zero"), loc)
        zero_assign = ir.Assign(ir.Const(0, loc), zero_var, loc)
        # starts: assign to zeros
        starts_var = ir.Var(scope, mk_unique_var("$h5_starts"), loc)
        start_tuple_call = ir.Expr.build_tuple([zero_var]*ndims, loc)
        starts_assign = ir.Assign(start_tuple_call, starts_var, loc)
        out += [ndims_assign, zero_assign, starts_assign, sizes_assign]

        # err = h5write(f_id)
        err_var = ir.Var(scope, mk_unique_var("$pio_ret_var"), loc)
        write_call = ir.Expr.call(attr_var, [f_id, dset_var, ndims_var,
                    starts_var, sizes_var, zero_var, arr_var], (), loc)
        write_assign = ir.Assign(write_call, err_var, loc)
        out.append(write_assign)
        return out

    def _gen_h5read(self, lhs_var, rhs):
        f_id, dset  = self.h5_dsets[rhs.value.name]
        # file_name = self.str_const_table[self.h5_files[f_id.name]]
        # dset_str = self.str_const_table[dset.name]
        dset_type = self._get_dset_type(lhs_var.name, self.h5_files[f_id.name], dset.name)
        loc = rhs.value.loc
        scope = rhs.value.scope
        # TODO: generate size, alloc calls
        out = []
        size_vars = self._gen_h5size(f_id, dset, dset_type.ndim, scope, loc, out)
        out.extend(mk_alloc(None, None, lhs_var, tuple(size_vars), dset_type.dtype, scope, loc))
        self._gen_h5read_call(f_id, dset, size_vars, lhs_var, scope, loc, out)
        return out

    def _gen_h5size(self, f_id, dset, ndims, scope, loc, out):
        # g_pio_var = Global(hpat.pio_api)
        g_pio_var = ir.Var(scope, mk_unique_var("$pio_g_var"), loc)
        g_pio = ir.Global('pio_api', hpat.pio_api, loc)
        g_pio_assign = ir.Assign(g_pio, g_pio_var, loc)
        # attr call: h5size_attr = getattr(g_pio_var, h5size)
        h5size_attr_call = ir.Expr.getattr(g_pio_var, "h5size", loc)
        attr_var = ir.Var(scope, mk_unique_var("$h5size_attr"), loc)
        attr_assign = ir.Assign(h5size_attr_call, attr_var, loc)
        out += [g_pio_assign, attr_assign]

        size_vars = []
        for i in range(ndims):
            dim_var = ir.Var(scope, mk_unique_var("$h5_dim_var"), loc)
            dim_assign = ir.Assign(ir.Const(np.int32(i), loc), dim_var, loc)
            out.append(dim_assign)
            size_var = ir.Var(scope, mk_unique_var("$h5_size_var"), loc)
            size_vars.append(size_var)
            size_call = ir.Expr.call(attr_var, [f_id, dset, dim_var], (), loc)
            size_assign = ir.Assign(size_call, size_var, loc)
            out.append(size_assign)
        return size_vars

    def _gen_h5read_call(self, f_id, dset, size_vars, lhs_var, scope, loc, out):
        # g_pio_var = Global(hpat.pio_api)
        g_pio_var = ir.Var(scope, mk_unique_var("$pio_g_var"), loc)
        g_pio = ir.Global('pio_api', hpat.pio_api, loc)
        g_pio_assign = ir.Assign(g_pio, g_pio_var, loc)
        # attr call: h5size_attr = getattr(g_pio_var, h5read)
        h5size_attr_call = ir.Expr.getattr(g_pio_var, "h5read", loc)
        attr_var = ir.Var(scope, mk_unique_var("$h5read_attr"), loc)
        attr_assign = ir.Assign(h5size_attr_call, attr_var, loc)
        out += [g_pio_assign, attr_assign]

        # ndims args
        ndims = len(size_vars)
        ndims_var = ir.Var(scope, mk_unique_var("$h5_ndims"), loc)
        ndims_assign = ir.Assign(ir.Const(np.int32(ndims), loc), ndims_var, loc)
        # sizes arg
        sizes_var = ir.Var(scope, mk_unique_var("$h5_sizes"), loc)
        tuple_call = ir.Expr.build_tuple(size_vars, loc)
        sizes_assign = ir.Assign(tuple_call, sizes_var, loc)

        zero_var = ir.Var(scope, mk_unique_var("$const_zero"), loc)
        zero_assign = ir.Assign(ir.Const(0, loc), zero_var, loc)
        # starts: assign to zeros
        starts_var = ir.Var(scope, mk_unique_var("$h5_starts"), loc)
        start_tuple_call = ir.Expr.build_tuple([zero_var]*ndims, loc)
        starts_assign = ir.Assign(start_tuple_call, starts_var, loc)
        out += [ndims_assign, zero_assign, starts_assign, sizes_assign]

        err_var = ir.Var(scope, mk_unique_var("$h5_err_var"), loc)
        read_call = ir.Expr.call(attr_var, [f_id, dset, ndims_var, starts_var,
                                        sizes_var, zero_var, lhs_var], (), loc)
        out.append(ir.Assign(read_call, err_var, loc))
        return

    def _get_dset_type(self, lhs, file_varname, dset_varname):
        """get data set type from user-specified locals types or actual file"""
        if lhs in self.local_vars:
            return self.local_vars[lhs]
        if self.reverse_copies[lhs] in self.local_vars:
            return self.local_vars[self.reverse_copies[lhs]]

        if file_varname in self.str_const_table and dset_varname in self.str_const_table:
            file_name = self.str_const_table[file_varname]
            dset_str = self.str_const_table[dset_varname]
            f = h5py.File(file_name, "r")
            ndims = len(f[dset_str].shape)
            numba_dtype = numpy_support.from_dtype(f[dset_str].dtype)
            return types.Array(numba_dtype, ndims, 'C')

        raise RuntimeError("data set type not found")

    def _get_reverse_copies(self, body):
        for inst in body:
            if isinstance(inst, ir.Assign) and isinstance(inst.value, ir.Var):
                self.reverse_copies[inst.value.name] = inst.target.name
        return

    def _gen_h5close(self, stmt, f_id):
        lhs_var = stmt.target
        scope = lhs_var.scope
        loc = lhs_var.loc
        # g_pio_var = Global(hpat.pio_api)
        g_pio_var = ir.Var(scope, mk_unique_var("$pio_g_var"), loc)
        g_pio = ir.Global('pio_api', hpat.pio_api, loc)
        g_pio_assign = ir.Assign(g_pio, g_pio_var, loc)
        # attr call: h5close_attr = getattr(g_pio_var, h5close)
        h5close_attr_call = ir.Expr.getattr(g_pio_var, "h5close", loc)
        attr_var = ir.Var(scope, mk_unique_var("$h5close_attr"), loc)
        attr_assign = ir.Assign(h5close_attr_call, attr_var, loc)
        # h5close(f_id)
        close_call = ir.Expr.call(attr_var, [f_id], (), loc)
        close_assign = ir.Assign(close_call, lhs_var, loc)
        return [g_pio_assign, attr_assign, close_assign]

    def _gen_h5create_dset(self, stmt, f_id):
        lhs_var = stmt.target
        scope = lhs_var.scope
        loc = lhs_var.loc
        args = [f_id]+stmt.value.args
        # append the dtype arg (e.g. dtype='f8')
        assert stmt.value.kws and stmt.value.kws[0][0]=='dtype'
        args.append(stmt.value.kws[0][1])
        # g_pio_var = Global(hpat.pio_api)
        g_pio_var = ir.Var(scope, mk_unique_var("$pio_g_var"), loc)
        g_pio = ir.Global('pio_api', hpat.pio_api, loc)
        g_pio_assign = ir.Assign(g_pio, g_pio_var, loc)
        # attr call: h5create_dset_attr = getattr(g_pio_var, h5create_dset)
        h5create_dset_attr_call = ir.Expr.getattr(
                                                g_pio_var, "h5create_dset", loc)
        attr_var = ir.Var(scope, mk_unique_var("$h5create_dset_attr"), loc)
        attr_assign = ir.Assign(h5create_dset_attr_call, attr_var, loc)
        # dset_id = h5create_dset(f_id)
        create_dset_call = ir.Expr.call(attr_var, args, (), loc)
        create_dset_assign = ir.Assign(create_dset_call, lhs_var, loc)
        self.h5_dsets[lhs_var.name] = (f_id, args[1])
        self.h5_dsets_sizes[lhs_var.name] = self.tuple_table[args[2].name]
        return [g_pio_assign, attr_assign, create_dset_assign]

    def _gen_h5create_group(self, stmt, f_id):
        lhs_var = stmt.target
        scope = lhs_var.scope
        loc = lhs_var.loc
        args = [f_id]+stmt.value.args
        # g_pio_var = Global(hpat.pio_api)
        g_pio_var = ir.Var(scope, mk_unique_var("$pio_g_var"), loc)
        g_pio = ir.Global('pio_api', hpat.pio_api, loc)
        g_pio_assign = ir.Assign(g_pio, g_pio_var, loc)
        # attr call: h5create_group_attr = getattr(g_pio_var, h5create_group)
        h5create_group_attr_call = ir.Expr.getattr(
                                            g_pio_var, "h5create_group", loc)
        attr_var = ir.Var(scope, mk_unique_var("$h5create_group_attr"), loc)
        attr_assign = ir.Assign(h5create_group_attr_call, attr_var, loc)
        # group_id = h5create_group(f_id)
        create_group_call = ir.Expr.call(attr_var, args, (), loc)
        create_group_assign = ir.Assign(create_group_call, lhs_var, loc)
        # add to files since group behavior is same as files for many calls
        self.h5_files[lhs_var.name] = "group"
        return [g_pio_assign, attr_assign, create_group_assign]
