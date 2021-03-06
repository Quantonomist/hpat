from numba import types, cgutils
from numba.targets.imputils import lower_builtin
from numba.targets.arrayobj import make_array
import numba.targets.arrayobj
import numpy as np
import hpat
from hpat import distributed_api
import time
from llvmlite import ir as lir
import hdist
import llvmlite.binding as ll
ll.add_symbol('hpat_dist_get_rank', hdist.hpat_dist_get_rank)
ll.add_symbol('hpat_dist_get_size', hdist.hpat_dist_get_size)
ll.add_symbol('hpat_dist_get_end', hdist.hpat_dist_get_end)
ll.add_symbol('hpat_dist_get_node_portion', hdist.hpat_dist_get_node_portion)
ll.add_symbol('hpat_dist_get_time', hdist.hpat_dist_get_time)
ll.add_symbol('hpat_dist_reduce_i4', hdist.hpat_dist_reduce_i4)
ll.add_symbol('hpat_dist_reduce_i8', hdist.hpat_dist_reduce_i8)
ll.add_symbol('hpat_dist_reduce_f4', hdist.hpat_dist_reduce_f4)
ll.add_symbol('hpat_dist_reduce_f8', hdist.hpat_dist_reduce_f8)
ll.add_symbol('hpat_dist_arr_reduce', hdist.hpat_dist_arr_reduce)
ll.add_symbol('hpat_dist_exscan_i4', hdist.hpat_dist_exscan_i4)
ll.add_symbol('hpat_dist_exscan_i8', hdist.hpat_dist_exscan_i8)
ll.add_symbol('hpat_dist_exscan_f4', hdist.hpat_dist_exscan_f4)
ll.add_symbol('hpat_dist_exscan_f8', hdist.hpat_dist_exscan_f8)
ll.add_symbol('hpat_dist_irecv', hdist.hpat_dist_irecv)
ll.add_symbol('hpat_dist_isend', hdist.hpat_dist_isend)
ll.add_symbol('hpat_dist_wait', hdist.hpat_dist_wait)
ll.add_symbol('hpat_dist_get_item_pointer', hdist.hpat_dist_get_item_pointer)
ll.add_symbol('hpat_get_dummy_ptr', hdist.hpat_get_dummy_ptr)

@lower_builtin(distributed_api.get_rank)
def dist_get_rank(context, builder, sig, args):
    fnty = lir.FunctionType(lir.IntType(32), [])
    fn = builder.module.get_or_insert_function(fnty, name="hpat_dist_get_rank")
    return builder.call(fn, [])

@lower_builtin(distributed_api.get_size)
def dist_get_size(context, builder, sig, args):
    fnty = lir.FunctionType(lir.IntType(32), [])
    fn = builder.module.get_or_insert_function(fnty, name="hpat_dist_get_size")
    return builder.call(fn, [])

@lower_builtin(distributed_api.get_end, types.int64, types.int64, types.int32, types.int32)
def dist_get_end(context, builder, sig, args):
    fnty = lir.FunctionType(lir.IntType(64), [lir.IntType(64), lir.IntType(64),
                                            lir.IntType(32), lir.IntType(32)])
    fn = builder.module.get_or_insert_function(fnty, name="hpat_dist_get_end")
    return builder.call(fn, [args[0], args[1], args[2], args[3]])

@lower_builtin(distributed_api.get_node_portion, types.int64, types.int64, types.int32, types.int32)
def dist_get_portion(context, builder, sig, args):
    fnty = lir.FunctionType(lir.IntType(64), [lir.IntType(64), lir.IntType(64),
                                            lir.IntType(32), lir.IntType(32)])
    fn = builder.module.get_or_insert_function(fnty, name="hpat_dist_get_node_portion")
    return builder.call(fn, [args[0], args[1], args[2], args[3]])

@lower_builtin(distributed_api.dist_reduce, types.int64)
@lower_builtin(distributed_api.dist_reduce, types.int32)
@lower_builtin(distributed_api.dist_reduce, types.float32)
@lower_builtin(distributed_api.dist_reduce, types.float64)
def lower_dist_reduce(context, builder, sig, args):
    ltyp = args[0].type
    fnty = lir.FunctionType(ltyp, [ltyp])
    typ_map = {types.int32:"i4", types.int64:"i8", types.float32:"f4", types.float64:"f8"}
    typ_str = typ_map[sig.args[0]]
    fn = builder.module.get_or_insert_function(fnty, name="hpat_dist_reduce_{}".format(typ_str))
    return builder.call(fn, [args[0]])

@lower_builtin(distributed_api.dist_arr_reduce, types.npytypes.Array)
def lower_dist_arr_reduce(context, builder, sig, args):
    # store an int to specify data type
    typ_enum = hpat.pio_lower._h5_typ_table[sig.args[0].dtype]
    typ_arg = cgutils.alloca_once_value(builder, lir.Constant(lir.IntType(32), typ_enum))
    ndims = sig.args[0].ndim

    out = make_array(sig.args[0])(context, builder, args[0])
    # store size vars array struct to pointer
    size_ptr = cgutils.alloca_once(builder, out.shape.type)
    builder.store(out.shape, size_ptr)
    size_arg = builder.bitcast(size_ptr, lir.IntType(64).as_pointer())

    ndim_arg = cgutils.alloca_once_value(builder, lir.Constant(lir.IntType(32), sig.args[0].ndim))
    call_args = [builder.bitcast(out.data, lir.IntType(8).as_pointer()),
                size_arg, builder.load(ndim_arg), builder.load(typ_arg)]

    # array, shape, ndim, extra last arg type for type enum
    arg_typs = [lir.IntType(8).as_pointer(), lir.IntType(64).as_pointer(),
        lir.IntType(32), lir.IntType(32)]
    fnty = lir.FunctionType(lir.IntType(32), arg_typs)
    fn = builder.module.get_or_insert_function(fnty, name="hpat_dist_arr_reduce")
    return builder.call(fn, call_args)

@lower_builtin(time.time)
def dist_get_time(context, builder, sig, args):
    fnty = lir.FunctionType(lir.DoubleType(), [])
    fn = builder.module.get_or_insert_function(fnty, name="hpat_dist_get_time")
    return builder.call(fn, [])

@lower_builtin(distributed_api.dist_cumsum, types.npytypes.Array, types.npytypes.Array)
def lower_dist_cumsum(context, builder, sig, args):

    dtype = sig.args[0].dtype
    zero = dtype(0)

    def cumsum_impl(in_arr, out_arr):
        c = zero
        for v in np.nditer(in_arr):
            c += v.item()
        prefix_var = distributed_api.dist_exscan(c)
        for i in range(in_arr.size):
            prefix_var += in_arr[i]
            out_arr[i] = prefix_var
        return 0

    res = context.compile_internal(builder, cumsum_impl, sig, args,
                                    locals=dict(c=dtype,
                                    prefix_var=dtype))
    return res


@lower_builtin(distributed_api.dist_exscan, types.int64)
@lower_builtin(distributed_api.dist_exscan, types.int32)
@lower_builtin(distributed_api.dist_exscan, types.float32)
@lower_builtin(distributed_api.dist_exscan, types.float64)
def lower_dist_exscan(context, builder, sig, args):
    ltyp = args[0].type
    fnty = lir.FunctionType(ltyp, [ltyp])
    typ_map = {types.int32:"i4", types.int64:"i8", types.float32:"f4", types.float64:"f8"}
    typ_str = typ_map[sig.args[0]]
    fn = builder.module.get_or_insert_function(fnty, name="hpat_dist_exscan_{}".format(typ_str))
    return builder.call(fn, [args[0]])


@lower_builtin(distributed_api.irecv, types.npytypes.Array, types.int32,
types.int32, types.int32, types.boolean)
def lower_dist_irecv(context, builder, sig, args):
    # store an int to specify data type
    typ_enum = hpat.pio_lower._h5_typ_table[sig.args[0].dtype]
    typ_arg = cgutils.alloca_once_value(builder, lir.Constant(lir.IntType(32), typ_enum))

    out = make_array(sig.args[0])(context, builder, args[0])

    call_args = [builder.bitcast(out.data, lir.IntType(8).as_pointer()),
                args[1], builder.load(typ_arg),
                args[2], args[3], args[4]]

    # array, size, extra arg type for type enum
    # pe, tag, cond
    arg_typs = [lir.IntType(8).as_pointer(),
        lir.IntType(32), lir.IntType(32), lir.IntType(32), lir.IntType(32),
        lir.IntType(1)]
    fnty = lir.FunctionType(lir.IntType(32), arg_typs)
    fn = builder.module.get_or_insert_function(fnty, name="hpat_dist_irecv")
    return builder.call(fn, call_args)

@lower_builtin(distributed_api.isend, types.npytypes.Array, types.int32,
types.int32, types.int32, types.boolean)
def lower_dist_isend(context, builder, sig, args):
    # store an int to specify data type
    typ_enum = hpat.pio_lower._h5_typ_table[sig.args[0].dtype]
    typ_arg = cgutils.alloca_once_value(builder, lir.Constant(lir.IntType(32), typ_enum))

    out = make_array(sig.args[0])(context, builder, args[0])

    call_args = [builder.bitcast(out.data, lir.IntType(8).as_pointer()),
                args[1], builder.load(typ_arg),
                args[2], args[3], args[4]]

    # array, size, extra arg type for type enum
    # pe, tag, cond
    arg_typs = [lir.IntType(8).as_pointer(),
        lir.IntType(32), lir.IntType(32), lir.IntType(32), lir.IntType(32),
        lir.IntType(1)]
    fnty = lir.FunctionType(lir.IntType(32), arg_typs)
    fn = builder.module.get_or_insert_function(fnty, name="hpat_dist_isend")
    return builder.call(fn, call_args)

@lower_builtin(distributed_api.wait, types.int32, types.boolean)
def lower_dist_wait(context, builder, sig, args):
    fnty = lir.FunctionType(lir.IntType(32), [lir.IntType(32), lir.IntType(1)])
    fn = builder.module.get_or_insert_function(fnty, name="hpat_dist_wait")
    return builder.call(fn, args)

@lower_builtin(distributed_api.dist_setitem, types.Array, types.Any, types.Any,
    types.intp, types.intp)
def dist_setitem_array(context, builder, sig, args):
    """add check for access to be in processor bounds of the array"""
    # TODO: replace array shape if array is small
    #  (processor chuncks smaller than setitem range causes index normalization)
    # remove start and count args to call regular get_item_pointer2
    count = args.pop()
    start = args.pop()
    sig.args = tuple([sig.args[0], sig.args[1], sig.args[2]])
    regular_get_item_pointer2 = cgutils.get_item_pointer2
    # add bounds check for distributed access,
    # return a dummy pointer if out of bounds
    def dist_get_item_pointer2(builder, data, shape, strides, layout, inds,
                      wraparound=False):
        # get local index or -1 if out of bounds
        fnty = lir.FunctionType(lir.IntType(64), [lir.IntType(64), lir.IntType(64), lir.IntType(64)])
        fn = builder.module.get_or_insert_function(fnty, name="hpat_dist_get_item_pointer")
        first_ind = builder.call(fn, [inds[0], start, count])
        inds = tuple([first_ind, *inds[1:]])
        # regular local pointer with new indices
        in_ptr = regular_get_item_pointer2(builder, data, shape, strides, layout, inds, wraparound)
        ret_ptr = cgutils.alloca_once(builder, in_ptr.type)
        builder.store(in_ptr, ret_ptr)
        not_inbound = builder.icmp_signed('==', first_ind, lir.Constant(lir.IntType(64), -1))
        # get dummy pointer
        dummy_fnty  = lir.FunctionType(lir.IntType(8).as_pointer(), [])
        dummy_fn = builder.module.get_or_insert_function(dummy_fnty, name="hpat_get_dummy_ptr")
        dummy_ptr = builder.bitcast(builder.call(dummy_fn, []), in_ptr.type)
        with builder.if_then(not_inbound, likely=True):
            builder.store(dummy_ptr, ret_ptr)
        return builder.load(ret_ptr)

    # replace inner array access call for setitem generation
    cgutils.get_item_pointer2 = dist_get_item_pointer2
    numba.targets.arrayobj.setitem_array(context, builder, sig, args)
    cgutils.get_item_pointer2 = regular_get_item_pointer2
    return lir.Constant(lir.IntType(32), 0)
