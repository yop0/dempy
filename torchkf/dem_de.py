import torch 
from .dem_structs import *
from .dem_hgm import *
from .dem_dx import *

from typing import List
from pprint import pprint
import numpy as np
import scipy as sp
import scipy.linalg 

def as_matrix_it(*args):
    for arg in args: 
        yield arg.reshape((arg.shape[0], -1))


def dem_eval_err_diff(n: int, d: int, M: HierarchicalGaussianModel, qu: dotdict, qp: dotdict): 
        # Get dimensions
        # ==============
        nl = len(M)
        ne = sum(m.l for m in M) 
        nv = sum(m.m for m in M)
        nx = sum(m.n for m in M)
        nP = sum(M[i].p for i in range(nl - 1))
        ny = M[0].l
        nc = M[-1].l

        # Evaluate functions at each level
        # ================================
        f = []
        g = []
        x = []
        v = []
        nxi = 0
        nvi = 0
        for i in range(nl - 1):
            xi  = qu.x[0, nxi:nxi + M[i].n]
            vi  = qu.v[0, nvi:nvi + M[i].m]

            nxi = nxi + M[i].n
            nvi = nvi + M[i].m

            x.append(xi)
            v.append(vi)

            p = M[i].pE + qp.u[i] @ qp.p[i]
            try: 
                res = M[i].f(xi, vi, p)
            except: 
                raise RuntimeError(f"Error while evaluating model[{i}].f!")
            f.append(res)

            try: 
                res = M[i].g(xi, vi, p)
            except: 
                raise RuntimeError(f"Error while evaluating model[{i}].g!")
            g.append(res)

        f = np.concatenate(f).reshape((nx,))
        g = np.concatenate(g).reshape((ne - nc,))

        # Evaluate derivatives at each level
        df  = list()
        d2f = list()
        dg  = list()
        d2g = list()
        for i in range(nl - 1): 
            xvp = (_ if sum(_.shape) > 0 else np.empty(0) for _ in  (x[i], v[i], qp.p[i], qp.u[i], M[i].pE))
            xvp = tuple(as_matrix_it(*xvp))

            if M[i].df_qup is not None:
                dfi  = M[i].df_qup(*xvp)
                d2fi = M[i].d2f_qup(*xvp) 
            else: 
                dfi, d2fi = compute_df_d2f(lambda x, v, q, u, p: M[i].f(x, v, p + u @ q), xvp, ['dx', 'dv', 'dp', 'du', 'dq'])

            if M[i].dg_qup is not None:
                dgi  = M[i].dg_qup(*xvp)
                d2gi = M[i].d2g_qup(*xvp) 
            else: 
                dgi, d2gi = compute_df_d2f(lambda x, v, q, u, p: M[i].g(x, v, p + u @ q), xvp, ['dx', 'dv', 'dp', 'du', 'dq']) 

            dg.append(dgi)
            df.append(dfi)

            d2f.append(d2fi)
            d2g.append(d2gi)

        # Setup df
        df = dotdict({k: sp.linalg.block_diag(*(dfi[k] for dfi in df)) for k in ['dx', 'dv', 'dp']}) 

        # Setup dgdv manually 
        dgdv = cell(nl, nl-1)
        for i in range(nl - 1): 
            # causes (level i) appear at level i in g(x[i],v[i]) and at level i+1 as -I
            # nb: dg = [dydv[:], dv[0]dv[:], ...]
            dgdv[  i, i] = dg[i].dv 
            dgdv[i+1, i] = -np.eye(M[i].m)

        dgdv = block_matrix(dgdv)

        # Setup dg
        dg = dotdict({k: sp.linalg.block_diag(*(dgi[k] for dgi in dg)) for k in ['dx', 'dp']}) 
        # add an extra row to accomodate the highest hierarchical level
        for k in dg.keys():
            dg[k] = np.concatenate([dg[k], np.zeros((nc, dg[k].shape[1]))], axis=0)
        dg.dv = dgdv

        # Reshape df and dg to avoid errors laters
        df.dx = df.dx.reshape((nx, nx))
        df.dv = df.dv.reshape((nx, nv))
        df.dp = df.dp.reshape((nx, nP))
        dg.dx = dg.dx.reshape((ne, nx))
        dg.dv = dg.dv.reshape((ne, nv))
        dg.dp = dg.dp.reshape((ne, nP))


        # Process d2f, d2g
        d2f = dotdict({i: dotdict({j: [d2fk[i][j] for d2fk in d2f] for j in ['dx', 'dv', 'dp']}) for i in ['dx', 'dv', 'dp']})
        d2g = dotdict({i: dotdict({j: [d2gk[i][j] for d2gk in d2g] for j in ['dx', 'dv', 'dp']}) for i in ['dx', 'dv', 'dp']}) 


        if nP > 0:
            dfdxp = np.stack([sp.linalg.block_diag(*(_[:, ip] for _ in d2f.dp.dx if _.size > 0)) for ip in range(nP)], axis=0)
            dfdvp = np.stack([sp.linalg.block_diag(*(_[:, ip] for _ in d2f.dp.dv if _.size > 0)) for ip in range(nP)], axis=0)

            dgdvp = np.stack([sp.linalg.block_diag(*(_[:, ip] for _ in d2g.dp.dv if _.size > 0)) for ip in range(nP)], axis=0)
            dgdxp = np.stack([sp.linalg.block_diag(*(_[:, ip] for _ in d2g.dp.dx if _.size > 0)) for ip in range(nP)], axis=0)

            dgdxp = [sp.linalg.block_diag(*(_[:, ip] for _ in d2g.dp.dx if _.size > 0)) for ip in range(nP)]
            dgdvp = [sp.linalg.block_diag(*(_[:, ip] for _ in d2g.dp.dv if _.size > 0)) for ip in range(nP)]

            # Add a component with nc rows to accomodate the highest hierarchical level
            dgdxp = np.stack([np.concatenate([dgdxpi, np.zeros((nc, dgdxpi.shape[1]))]) for dgdxpi in dgdxp], axis=0)
            dgdvp = np.stack([np.concatenate([dgdvpi, np.zeros((nc, dgdvpi.shape[1]))]) for dgdvpi in dgdvp], axis=0)

            dfdxp = dfdxp.reshape((nP, nx, nx))
            dfdvp = dfdvp.reshape((nP, nx, nv))
            dgdxp = dgdxp.reshape((nP, ne, nx))
            dgdvp = dgdvp.reshape((nP, ne, nv))
        else: 
            dfdxp = np.empty((nP, nx, nx))
            dfdvp = np.empty((nP, nx, nv))
            dgdxp = np.empty((nP, ne, nx))
            dgdvp = np.empty((nP, ne, nv))

        if nx > 0: 
            dfdpx = np.stack([sp.linalg.block_diag(*(_[:, ix] for _ in d2f.dx.dp if _.size > 0)) for ix in range(nx)], axis=0)
            dgdpx = [sp.linalg.block_diag(*(_[:, ix] for _ in d2g.dx.dp if _.size > 0)) for ix in range(nx)]

            # Add a component with nc rows to accomodate the highest hierarchical level
            dgdpx = np.stack([np.concatenate([dgdpxi, np.zeros((nc, dgdpxi.shape[1]))]) for dgdpxi in dgdpx], axis=0)

            dfdpx = dfdpx.reshape((nx, nx, nP))
            dgdpx = dgdpx.reshape((nx, ne, nP))
        else: 
            dfdpx = np.empty((nx, nx, nP))
            dgdpx = np.empty((nx, ne, nP))

        if nv > 0: 
            dfdpv = np.stack([sp.linalg.block_diag(*(_[:, iv] for _ in d2f.dv.dp if _.size > 0)) for iv in range(nv)], axis=0)
            dgdpv = [sp.linalg.block_diag(*(_[:, iv] for _ in d2g.dv.dp if _.size > 0)) for iv in range(nv)]

            # Add a component with nc rows to accomodate the highest hierarchical level
            dgdpv = np.stack([np.concatenate([dgdpvi, np.zeros((1, dgdpvi.shape[1]))]) for dgdpvi in dgdpv], axis=0)

            dfdpv = dfdpv.reshape((nv, nx, nP))
            dgdpv = dgdpv.reshape((nv, ne, nP))
        else: 
            dfdpv = np.empty((nv, nx, nP))
            dgdpv = np.empty((nv, ne, nP))
        

        dfdpu = np.concatenate([dfdpx, dfdpv], axis=0)
        dgdpu = np.concatenate([dgdpx, dgdpv], axis=0)

        de    = dotdict(
          dy  = np.eye(ne, ny), 
          dc  = np.diag(-np.ones(max(ne, nc) - (nc - ne)), nc - ne)[:ne, :nc]
        )

        # Prediction error (E) - causes        
        Ev = [np.concatenate([qu.y[0], qu.v[0]]) -  np.concatenate([g, qu.u[0]])]
        for i in range(1, n):
            Evi = de.dy @ qu.y[i] + de.dc @ qu.u[i] - dg.dx @ qu.x[i] - dg.dv @ qu.v[i]
            Ev.append(Evi)

        # Prediction error (E) - states
        Ex = [qu.x[1] - f]
        for i in range(1, n-1):
            Exi = qu.x[i + 1] - df.dx @ qu.x[i] - df.dv @ qu.v[i]
            Ex.append(Exi)
        Ex.append(np.zeros_like(Exi))

        Ev = np.concatenate(Ev)
        Ex = np.concatenate(Ex)
        E  = np.concatenate([Ev, Ex])[:, None]

        # generalised derivatives
        dgdp = [dg.dp]
        dfdp = [df.dp]

        qux = qu.x[..., None]
        quv = qu.v[..., None]
        for i in range(1, n):
            dgdpi = dg.dp.copy()
            dfdpi = df.dp.copy()

            for ip in range(nP): 
                dgdpi[:, ip] = (dgdxp[ip] @ qux[i] + dgdvp[ip] @ quv[i]).squeeze(1)
                dfdpi[:, ip] = (dfdxp[ip] @ qux[i] + dfdvp[ip] @ quv[i]).squeeze(1)
            
            dfdp.append(dfdpi)
            dgdp.append(dgdpi)

        df.dp = np.concatenate(dfdp)
        dg.dp = np.concatenate(dgdp)

        de.dy               = np.kron(np.eye(n, n), de.dy)
        df.dy               = np.kron(np.eye(n, n), np.zeros((nx, ny)))
        df.dc               = np.zeros((n*nx, n*nc))
        dg.dx               = np.kron(np.eye(n, n), dg.dx)
        # df.dx = (I * df.dx) - D, Eq. 45
        df.dx               = np.kron(np.eye(n, n), df.dx) - np.kron(np.diag(np.ones(n - 1), 1), np.eye(nx, nx)) 

        # embed to n >= d
        dedc                = np.zeros((n*ne, n*nc)) 
        dedc[:n*ne,:d*nc]   = np.kron(np.eye(n, d), de.dc)
        de.dc               = dedc

        dgdv                = np.zeros((n*ne, n*nv))
        dgdv[:n*ne,:d*nv]   = np.kron(np.eye(n, d), dg.dv)
        dg.dv               = dgdv

        dfdv                = np.zeros((n*nx, n*nv))
        dfdv[:n*nx,:d*nv]   = np.kron(np.eye(n, d), df.dv)
        df.dv               = dfdv

        dE    = dotdict()
        dE.dy = np.concatenate([de.dy, df.dy])
        dE.dc = np.concatenate([de.dc, df.dc])
        dE.dp = - np.concatenate([dg.dp, df.dp])
        dE.du = - block_matrix([
                [dg.dx, dg.dv], 
                [df.dx, df.dv]])

        dE.dup = []
        for ip in range(nP): 
            dfdxpi              = np.kron(np.eye(n,n), dfdxp[ip])
            dgdxpi              = np.kron(np.eye(n,n), dgdxp[ip])
            dfdvpi              = np.zeros((n*nx, n*nv))
            dfdvpi[:,:d*nv]     = np.kron(np.eye(n,d), dfdvp[ip])
            dgdvpi              = np.zeros((n*ne, n*nv))
            dgdvpi[:,:d*nv]     = np.kron(np.eye(n,d), dgdvp[ip])

            dEdupi = -block_matrix([[dgdxpi, dgdvpi], [dfdxpi, dfdvpi]])
            dE.dup.append(dEdupi)

        dE.dpu = [] 
        for i in range(n): 
            for iu in range(nx + nv):
                dfdpui = np.kron(np.eye(n,1), dfdpu[iu])
                dgdpui = np.kron(np.eye(n,1), dgdpu[iu])
                dEdpui = np.concatenate([dgdpui, dfdpui], axis=0)

                dE.dpu.append(dEdpui)

        return E, dE