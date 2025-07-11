import taichi as ti

import numpy as np
from typing import List

from scipy.sparse import coo_matrix, csc_matrix
from . import maths
from . import sim_utils


@ti.dataclass
class SparseTriplet:
    row: int
    column: int
    value: float

    def get_shape(self):
        return (3, )


@ti.data_oriented
class SparseMatrix:
    def __init__(self, 
                 batch_size: int, 
                 nmax_row: int, 
                 nmax_column: int, 
                 nmax_triplet: int, 
                 store_dense: bool, 
                 **kwargs) -> None:
        
        self.batch_size = batch_size
        self.nmax_row = nmax_row
        self.nmax_column = nmax_column
        self.nmax_triplet = nmax_triplet

        self.n_triplet = sim_utils.GLOBAL_CREATER.ScalarField(dtype=int, shape=(self.batch_size, ))
        """[B, ]"""
        self.n_triplet.fill(0)

        self.triplet = sim_utils.GLOBAL_CREATER.StructField(SparseTriplet, shape=(self.batch_size, nmax_triplet))
        """[B, T]"""
        self.triplet.fill(0)

        self.diag = sim_utils.GLOBAL_CREATER.ScalarField(dtype=float, shape=(self.batch_size, ti.min(nmax_row, nmax_column)))
        """[B, N]"""
        self.diag.fill(0.0)

        self.store_dense = store_dense
        self.dense = None
        """[B, N, M]"""
        if store_dense:
            block_size_list = kwargs.get("block_size_list", [32])

            block_size_all = 1
            for block_size in block_size_list:
                block_size_all *= block_size

            self.dense = ti.field(float)
            self.dense_pointer_list = [
                ti.root.pointer(ti.ijk,
                                (self.batch_size,
                                 (nmax_row + block_size_all - 1) // block_size_all,
                                 (nmax_column + block_size_all - 1) // block_size_all))]
            
            for idx, block_size in enumerate(block_size_list):
                if idx != len(block_size_list) - 1:
                    self.dense_pointer_list.append(self.dense_pointer_list[-1].pointer(
                        ti.ijk, (1, block_size, block_size)))
                else:
                    self.dense_pointer_list.append(self.dense_pointer_list[-1].bitmasked(
                        ti.ijk, (1, block_size, block_size)))
                
            self.dense_pointer_list[-1].place(self.dense)

            sim_utils.GLOBAL_CREATER.LogSparseField(shape=(self.batch_size,
                                                       (nmax_row + block_size_all - 1) // block_size_all * block_size_all,
                                                       (nmax_column + block_size_all - 1) // block_size_all * block_size_all))

    @ti.kernel
    def _get_matrix_kernel(self, ret_val: ti.types.ndarray(), row: int, col: int):
        max_n_triplet = maths.vec_max_func(self.n_triplet, self.batch_size)
        for batch_idx, t in ti.ndrange(self.batch_size, max_n_triplet):
            if t < self.n_triplet[batch_idx]:
                triplet = self.triplet[batch_idx, t]
                i, j, v = triplet.row, triplet.column, triplet.value
                if i < row and j < col:
                    ret_val[batch_idx, i, j] += v

    def get_matrix(self, row: int, col: int):
        ret_val = np.zeros(dtype=float, shape=(self.batch_size, row, col))
        self._get_matrix_kernel(ret_val, row, col)
        return ret_val  

    def get_coo_matrix(self) -> List[coo_matrix]:
        triplet_numpy = self.triplet.to_numpy()
        ret = []
        for batch_idx in range(self.batch_size):
            n_triplet = self.n_triplet[batch_idx]
            ret.append(coo_matrix(
                (triplet_numpy["value"][batch_idx, :n_triplet],
                (triplet_numpy["row"][batch_idx, :n_triplet],
                triplet_numpy["column"][batch_idx, :n_triplet])),
                dtype=float, shape=(self.nmax_row, self.nmax_column)
            ))
        return ret

    def get_csc_matrix(self) -> List[csc_matrix]:
        return [csc_matrix(coo, dtype=float) for coo in self.get_coo_matrix()]

    @ti.func
    def set_zero_func(self):
        self.n_triplet.fill(0)
        self.diag.fill(0.0)

    @ti.kernel
    def set_zero_kernel(self):
        self.set_zero_func()

    @ti.func
    def add_value_func(self, batch_idx, row_i, column_j, value):
        """sparse[i, j] += val"""
        if value != 0.0:
            old_cnt = ti.atomic_add(self.n_triplet[batch_idx], 1)
            if old_cnt < self.nmax_triplet:
                self.triplet[batch_idx, old_cnt] = SparseTriplet(row_i, column_j, value)
                if row_i == column_j:
                    self.diag[batch_idx, row_i] += value
            else:
                print("[ERROR] sparse matrix triplet reach maximum")

    @ti.kernel
    def add_value_kernel(self, batch_idx: int, row_i: int, column_j: int, value: float):
        """sparse[i, j] += val"""
        self.add_value_func(batch_idx, row_i, column_j, value)

    @ti.kernel
    def _triplet_to_dense_kernel(self):
        max_n_triplet = maths.vec_max_func(self.n_triplet, self.batch_size)
        for batch_idx, t in ti.ndrange(self.batch_size, max_n_triplet):
            if t < self.n_triplet[batch_idx]:
                triplet = self.triplet[batch_idx, t]
                i, j, v = triplet.row, triplet.column, triplet.value
                self.dense[batch_idx, i, j] += v

    def _triplet_to_dense(self):
        self.dense_pointer_list[0].deactivate_all()
        self._triplet_to_dense_kernel()

    @ti.kernel
    def _dense_to_triplet_kernel(self, n_row: ti.template(), n_column: ti.template(), force_symmetry: bool):
        for batch_idx, i, j in self.dense:
            if batch_idx < self.batch_size and i < n_row[batch_idx] and j < n_column[batch_idx]:
                if (not force_symmetry) or (i == j):
                    self.add_value_func(batch_idx, i, j, self.dense[batch_idx, i, j])
                elif i < j:
                    avg = (self.dense[batch_idx, i, j] + self.dense[batch_idx, j, i]) / 2
                    self.add_value_func(batch_idx, i, j, avg)
                    self.add_value_func(batch_idx, j, i, avg)

    def _dense_to_triplet(self, n_row: ti.Field, n_column: ti.Field, force_symmetry: bool):
        self.set_zero_kernel()
        self._dense_to_triplet_kernel(n_row, n_column, force_symmetry)

    def compress(self, n_row: ti.Field, n_column: ti.Field, force_symmetry: bool):
        assert self.store_dense, "Cannot compress if not store dense data. "
        self._triplet_to_dense()
        self._dense_to_triplet(n_row, n_column, force_symmetry)

    @ti.func
    def mul_vec_func(self, batch_mask, ans, vec, n_field):
        """sparse(bxnxn) @ vec(bxn) = ans(bxn)

        Notice:
            'ans' and 'vec' are different vectors."""
        max_n = maths.vec_max_func(n_field, self.batch_size)
        for batch_idx, i in ti.ndrange(self.batch_size, max_n):
            if i < n_field[batch_idx] and batch_mask[batch_idx] != 0.0:
                ans[batch_idx, i] = 0.0
        
        max_t = maths.vec_max_func(self.n_triplet, self.batch_size)
        for batch_idx, t in ti.ndrange(self.batch_size, max_t):
            if t < self.n_triplet[batch_idx] and batch_mask[batch_idx] != 0.0:
                triplet = self.triplet[batch_idx, t]
                i, j, v = triplet.row, triplet.column, triplet.value
                if i < n_field[batch_idx] and j < n_field[batch_idx]:
                    ans[batch_idx, i] += v * vec[batch_idx, j]

    @ti.kernel
    def mul_vec_kernel(self, batch_mask: ti.template(), ans: ti.template(), vec: ti.template(), n_field: ti.template()) -> int:
        """sparse(bxnxn) @ vec(bxn) = ans(bxn)

        Notice:
            'ans' and 'vec' are different vectors."""
        self.mul_vec_func(batch_mask, ans, vec, n_field)
        return 0

    @ti.kernel
    def get_diag_kernel(self, ans: ti.template(), n_field: ti.template()):
        """use sparse's diagonal elements to set ans"""
        max_n = maths.vec_max_func(n_field, self.batch_size)
        for batch_idx, i in ti.ndrange(self.batch_size, max_n):
            if i < n_field[batch_idx]:
                ans[batch_idx, i] = self.diag[batch_idx, i]

    def get_diag(self) -> np.ndarray:
        return self.diag.to_numpy()

    @ti.func
    def sparse_add_diag_func(self, diag: ti.template(), sparse: ti.template(),
                             u: ti.template(), v: ti.template(),
                             n_field: ti.template()):
        """self = u * diag + v * sparse

        Args:
            - diag: [B, N]
            - sparse: SparseMatrix [B, N, M]
            - u, v: float [B, ]
            - n: dim of diag [B, ]

        Notice:
            - 'self' must be different from 'sparse'.
        """
        self.set_zero_func()
        
        max_n = maths.vec_max_func(n_field, self.batch_size)
        for batch_idx, i in ti.ndrange(self.batch_size, max_n):
            if i < n_field[batch_idx]:
                self.add_value_func(batch_idx, i, i, u[batch_idx] * diag[batch_idx, i])

        max_t = maths.vec_max_func(sparse.n_triplet, self.batch_size)
        for batch_idx, t in ti.ndrange(self.batch_size, max_t):
            if t < sparse.n_triplet[batch_idx]:
                triplet = sparse.triplet[batch_idx, t]
                i, j, val = triplet.row, triplet.column, triplet.value
                if i < n_field[batch_idx] and j < n_field[batch_idx]:
                    self.add_value_func(batch_idx, i, j, v[batch_idx] * val)

    @ti.kernel
    def sparse_add_diag_kernel(self, diag: ti.template(), sparse: ti.template(), 
                               u: ti.template(), v: ti.template(), 
                               n_field: ti.template()):
        """self = u * diag + v * sparse

        Args:
            - diag: [B, N]
            - sparse: SparseMatrix [B, N, M]
            - u, v: float [B, ]
            - n: dim of diag [B, ]

        Notice:
            - 'self' must be different from 'sparse'.
        """
        self.sparse_add_diag_func(diag, sparse, u, v, n_field)

    @ti.func
    def add_sparse_func(self, sparse):
        """self += sparse

        Args:
            - sparse: SparseMatrix

        Notice:
            - 'self' must be different from 'sparse'.
        """
        max_t = maths.vec_max_func(sparse.n_triplet, self.batch_size)
        for batch_idx, t in ti.ndrange(self.batch_size, max_t):
            if t < sparse.n_triplet[batch_idx]:
                triplet = sparse.triplet[batch_idx, t]
                i, j, val = triplet.row, triplet.column, triplet.value
                self.add_value_func(batch_idx, i, j, val)

    @ti.kernel
    def add_sparse_kernel(self, sparse: ti.template()):
        """self += sparse

        Args:
            - sparse: SparseMatrix

        Notice:
            - 'self' must be different from 'sparse'.
        """
        self.add_sparse_func(sparse)

    @ti.func
    def sparse_add_sparse_func(self, sparse1: ti.template(), sparse2: ti.template()):
        """self = sparse1 + sparse2

        Args:
            - sparse1: SparseMatrix
            - sparse2: SparseMatrix

        Notice:
            - 'self' must be different from 'sparse1' and 'sparse2'.
        """
        self.set_zero_func()

        max_t1 = maths.vec_max_func(sparse1.n_triplet, self.batch_size)
        for batch_idx, t in ti.ndrange(self.batch_size, max_t1):
            if t < sparse1.n_triplet[batch_idx]:
                triplet = sparse1.triplet[batch_idx, t]
                i, j, val = triplet.row, triplet.column, triplet.value
                self.add_value_func(batch_idx, i, j, val)

        max_t2 = maths.vec_max_func(sparse2.n_triplet, self.batch_size)
        for batch_idx, t in ti.ndrange(self.batch_size, max_t2):
            if t < sparse2.n_triplet[batch_idx]:
                triplet = sparse2.triplet[batch_idx, t]
                i, j, val = triplet.row, triplet.column, triplet.value
                self.add_value_func(batch_idx, i, j, val)

    @ti.kernel
    def sparse_add_sparse_kernel(self, sparse1: ti.template(), sparse2: ti.template()):
        """self = sparse1 + sparse2

        Args:
            - sparse1: SparseMatrix
            - sparse2: SparseMatrix

        Notice:
            - 'self' must be different from 'sparse1' and 'sparse1'.
        """
        self.sparse_add_sparse_func(sparse1, sparse2)

    @ti.kernel
    def get_block_diag_kernel(self, ans: ti.template(), block_dim: int):
        """
        Get block diagonal elements.

        Args:
            - ans: ti.Field, shape = (B, ?, M, M), where M is the block_dim
                - ans[b, i, j, k] = self[b, i * M + j, i * M + k]
        """
        ans.fill(0.0)
        m = block_dim

        max_t = maths.vec_max_func(self.n_triplet, self.batch_size)
        for batch_idx, t in ti.ndrange(self.batch_size, max_t):
            if t < self.n_triplet[batch_idx]:
                triplet = self.triplet[batch_idx, t]
                i, j, v = triplet.row, triplet.column, triplet.value
                ii = i // m
                jj = j // m
                if ii == jj:
                    ans[batch_idx, ii, i % m, j % m] += v
