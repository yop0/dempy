import numpy as np
import warnings
import time

from math import prod

from .dem_structs import *
from .dem_dx import compute_sym_df_d2f, compile_symb_func


class GaussianModel(dotdict): 
    def __init__(self, 
        f=None, g=None, fsymb=None, gsymb=None, m=None, n=None, l=None, p=None, x=None, v=None, 
        pE=None, pC=None, hE=None, hC=None, gE=None, gC=None, 
        Q=None, R=None, V=None, W=None, xP=None, vP=None, sv=None, sw=None, constraints=None): 
        self.f  : Callable         = f  # forward function (must be numpy compatible) - takes 3 vector arguments, return 1 vector of size n
        self.g  : Callable         = g  # observation function (must be numpy compatible) - takes 3 vector arguments, return 1 vector of size l

        self.fsymb: Callable       = fsymb # symbolic declaration of f using sympy
        self.gsymb: Callable       = gsymb # symbolic declaration of g using sympy

        self.m  : int              = m  # number of inputs
        self.n  : int              = n  # number of states
        self.l  : int              = l  # number of outputs
        self.p  : int              = p  # number of parameters

        self.x  : np.ndarray       = x  # explicitly specified states
        self.v  : np.ndarray       = v  # explicitly specified inputs

        self.pE : np.ndarray       = pE # prior expectation of parameters p
        self.pC : np.ndarray       = pC # prior covariance of parameters p
        self.hE : np.ndarray       = hE # prior expectation of hyperparameters h (log-precision of cause noise)
        self.hC : np.ndarray       = hC # prior covariance of hyperparameters h (log-precision of cause noise)
        self.gE : np.ndarray       = gE # prior expectation of hyperparameters g (log-precision of state noise)
        self.gC : np.ndarray       = gC # prior covariance of hyperparameters g (log-precision of state noise)

        self.Q  : List[np.ndarray] = Q  # precision components (input noise)
        self.R  : List[np.ndarray] = R  # precision components (state noise)
        self.V  : np.ndarray       = V  # fixed precision (input noise)
        self.W  : np.ndarray       = W  # fixed precision (state noise)
        self.xP : np.ndarray       = xP # precision (states)
        self.vP : np.ndarray       = vP # precision (inputs)

        self.constraints: np.ndarray = constraints

        self.sv : np.ndarray       = sv # smoothness (input noise)
        self.sw : np.ndarray       = sw # smoothness (state noise)

        self.df      : dotdict     = None
        self.d2f     : dotdict     = None
        self.dg      : dotdict     = None
        self.d2g     : dotdict     = None

        self.num_diff : bool       = False

class HierarchicalGaussianModel(list): 
    def __init__(self, *models: GaussianModel, dt=None, use_numerical_derivatives=False, n_jobs=1): 
        self.dt = 1 if dt is None else dt 
        self._use_numerical_derivatives = use_numerical_derivatives
        self._n_jobs = n_jobs
        models = self.prepare_models(*models)
        super().__init__(models)

    def prepare_models(self, *models):
        # inspired from spm_DEM_set by Karl Friston

        M = list(models)

        # order 
        g   = len(M)

        # check supra-ordinate level and add one (with flat priors) if necessary
        if callable(M[-1].g) or callable(M[-1].gsymb): 
            M.append(GaussianModel(l=M[-1].m))
            g = len(M)
        M[-1].n = 0
        M[-1].m = 0
        M[-1].p = 0

        for i in range(g): 
            # check for hidden states
            if M[i].f is not None and M[i].n is None and M[i].x is None:
                raise ValueError('please specify hidden states or their number')

            # default fields for static models (hidden states)
            if not callable(M[i].f) and not callable(M[i].fsymb): 
                M[i].f = lambda *x: np.zeros((0,1))
                M[i].x = np.zeros((0,1))
                M[i].n = 0

            # consistency and format check on states, parameters and functions
            # ================================================================

            # prior expectation of parameters pE
            # ----------------------------------
            if   M[i].pE is None: 
                 M[i].pE = np.zeros((0,1))
            elif len(M[i].pE.shape) == 1: 
                 M[i].pE = M[i].pE[..., None]

            p       = M[i].pE.shape[0]
            M[i].p  = p
            M[i].nP = p # store the original number of params, p will be overidden by the number of singular values

            # and prior covariances pC
            if  M[i].pC is None:
                M[i].pC = np.zeros((p, p))

            # convert variance to covariance
            elif not hasattr(M[i].pC, 'shape') or len(M[i].pC.shape) == 0:  
                M[i].pC = np.eye(p) * M[i].pC

            # convert variances to covariance
            elif len(M[i].pC.shape) == 1: 
                M[i].pC = np.diag(M[i].pC)

            # check size
            if M[i].pC.shape[0] != p or M[i].pC.shape[1] != p: 
                raise ValueError(f'Wrong shape for model[{i}].pC: expected ({p},{p}) but got {M[i].pC.shape}.')

            # expectations and covariances of constrained parameters
            # ------------------------------------------------------
            if M[i].constraints is not None: 
                if M[i].constraints.size != M[i].p: 
                    raise ValueError(f'The size of constraints ({M[i].constraints.size} '
                        f'does not match that of parameter expectations ({M[i].p}).')
                else: 
                    M[i].pE[M[i].constraints == 'positive'] = np.log(1 + M[i].pE[M[i].constraints == 'positive'])
                    M[i].pE[M[i].constraints == 'negative'] = np.log(1 - M[i].pE[M[i].constraints == 'negative'])
                    M[i].pC[M[i].constraints == 'positive'] = np.log(1+M[i].pC[M[i].constraints == 'positive'])
                    M[i].pC[M[i].constraints == 'negative'] = np.log(1+M[i].pC[M[i].constraints == 'negative'])

        # get inputs
        v = np.zeros((0,0)) if M[-1].v is None else M[-1].v
        if prod(v.shape) == 0:
            if M[-2].m is not None: 
                v = np.zeros((M[-2].m, 1))
            elif M[-1].l is not None: 
                v = np.zeros((M[-1].l, 1))
        M[-1].l  = v.shape[0]
        M[-1].v  = v

        # check functions
        for i in reversed(range(g - 1)):
            x      = np.zeros((M[i].n, 1)) if M[i].x is None else M[i].x

            if prod(x.shape) == 0 and M[i].n > 0:
                x = np.zeros((M[i].n, 1))
            
            # check f function
            if callable(M[i].f) and callable(M[i].fsymb): 
                raise ValueError(f"Got bot 'f' and 'fsymb' functions for model[{i}]!")

            if callable(M[i].fsymb):
                M[i].f = compile_symb_func(M[i].fsymb, M[i].n, M[i].m, M[i].p, input_keys='xvp')

            # check function f(x, v, P)
            elif not callable(M[i].f): 
                raise ValueError(f"Not callable function: model[{i}].f!")
            
            try: 
                f = M[i].f(x, v, M[i].pE)
            except: 
                raise ValueError(f"Error while calling function: model[{i}].f")

            if f.shape != x.shape:
                raise ValueError(f"Wrong shape for output of model[{i}].f (expected {x.shape}, got {f.shape}).")

            # check g function
            if callable(M[i].g) and callable(M[i].gsymb): 
                raise ValueError(f"Got bot 'g' and 'gsymb' functions for model[{i}]!")

            if callable(M[i].gsymb):
                M[i].g = compile_symb_func(M[i].gsymb, M[i].n, M[i].m, M[i].p, input_keys='xvp')

            # check function g(x, v, P)
            if not callable(M[i].g): 
                raise ValueError(f"Not callable function for model[{i}].g!")

            if M[i].m is not None and M[i].m != v.shape[0]:
                warnings.warn(f'Declared input shape of model {i} ({M[i].m}) '
                    f'does not match output shape of model[{i+1}].g ({v.shape[0]})!')
            M[i].m = v.shape[0]
            try: 
                v = M[i].g(x, v, M[i].pE)
            except: 
                raise ValueError(f"Error while calling function: model[{i}].g")
            if M[i].l is not None and M[i].l != v.shape[0]:
                warnings.warn(f'Declared output shape of model {i} ({M[i].l}) '
                    f'does not match output of model[{i}].g ({v.shape[0]})!')

            M[i].l = v.shape[0]
            M[i].n = x.shape[0]

            M[i].v = v
            M[i].x = x

            if not M[i].num_diff and not self._use_numerical_derivatives:
                print('Compiling derivatives, it might take some time... ')

                if M[i].df is None and M[i].d2f is None: 
                    print('  Compiling f... ', end='')
                    ffunc = M[i].fsymb if M[i].fsymb is not None else M[i].f
                    try:
                        start_time = time.time()
                        M[i].df, M[i].d2f = compute_sym_df_d2f(ffunc, M[i].n, M[i].m, M[i].p, input_keys='xvp')
                        print(f'f() ok. (compiled in {(time.time() - start_time):.2f}s)')

                    except Exception as e: 
                        raise RuntimeError(f'Failed to obtain analytical derivatives for M[{i}].f.')
                        raise e

                elif M[i].df is not None and M[i].d2f is not None:
                    pass
                    # ... todo: check and stuff
                else: raise ValueError('Either both of (or none of) df, d2f must be provided')


                # compute g-derivatives in the general case
                if M[i].dg is None and M[i].d2g is None: 
                    print('  Compiling g... ', end='')
                    gfunc = M[i].gsymb if M[i].gsymb is not None else M[i].g
                    try:
                        start_time = time.time()
                        M[i].dg, M[i].d2g = compute_sym_df_d2f(gfunc, M[i].n, M[i].m, M[i].p, input_keys='xvp')
                        print(f'g() ok. (compiled in {(time.time() - start_time):.2f}s)')

                    except Exception: 
                        raise RuntimeError(f'Failed to obtain analytical derivatives for M[{i}].g.')

                elif M[i].dg is not None and M[i].d2g is not None:
                    pass
                    # ... todo: check and stuff
                else: raise ValueError('Either both of (or none of) dg, d2g must be provided')
                

                print('Done. ')

        # full priors on states
        for i in range(g): 

            # hidden states
            M[i].xP = np.empty(0) if M[i].xP is None else M[i].xP
            if prod(M[i].xP.shape) == len(M[i].xP.shape): 
                M[i].xP = np.eye(M[i].n) * M[i].xP.squeeze()
            elif len(M[i].xP.shape) == 1 and M[i].xP.shape[0] == M[i].n: 
                M[i].xP = np.diag(M[i].xP)
            elif len(M[i].xP.shape) > 2 or (len(M[i].xP.shape) == 2 and any(dim != M[i].n for dim in M[i].xP.shape)):
                raise ValueError(f'Wrong shape for M[{i}].xP: expected ({M[i].n},{M[i].n}), got {M[i].xP.shape}.')
            else: 
                M[i].xP = np.zeros((M[i].n, M[i].n))

            # hidden causes
            M[i].vP = np.empty(0) if M[i].vP is None else M[i].vP
            if prod(M[i].vP.shape) == len(M[i].vP.shape):   
                M[i].vP = np.eye(M[i].n) * M[i].vP.squeeze()
            elif len(M[i].vP.shape) == 1 and M[i].vP.shape[0] == M[i].n: 
                M[i].vP = np.diag(M[i].vP)
            elif len(M[i].vP.shape) > 2 or (len(M[i].vP.shape) == 2 and any(dim != M[i].n for dim in M[i].vP.shape)):
                raise ValueError(f'Wrong shape for M[{i}].vP: expected ({M[i].n},{M[i].n}), got {M[i].vP.shape}.')
            else: 
                M[i].vP = np.zeros((M[i].n, M[i].n))

        nx = sum(M[i].n for i in range(g))

        # Hyperparameters and components (causes: Q V and hidden states R, W)
        # ===================================================================

        # check hyperpriors hE - [log]hyper-parameters and components
        # -----------------------------------------------------------
        pP = 1
        for i in range(g):

            M[i].Q = [] if M[i].Q is None else M[i].Q
            M[i].R = [] if M[i].R is None else M[i].R
            M[i].V = np.empty(0) if M[i].V is None else M[i].V
            M[i].W = np.empty(0) if M[i].W is None else M[i].W

            # check hyperpriors (expectation)
            M[i].hE = np.zeros((len(M[i].Q), 1)) if M[i].hE is None else np.array(M[i].hE).reshape((-1,1))
            M[i].gE = np.zeros((len(M[i].R), 1)) if M[i].gE is None else np.array(M[i].gE).reshape((-1,1))

            #  check hyperpriors (covariances)
            try:
                assert(M[i].hC is not None)
                M[i].hC * M[i].hE
            except: 
                M[i].hC = np.eye(len(M[i].hE)) / pP 
            try:
                assert(M[i].gC is not None)
                M[i].gC * M[i].gE
            except: 
                M[i].gC = np.eye(len(M[i].gE)) / pP 
 
            # check Q and R (precision components)

            # check components and assume iid if not specified
            if len(M[i].Q) > (M[i].hE.size): 
                M[i].hE = np.zeros((len(M[i].Q), 1)) + M[i].hE[0]
            elif len(M[i].Q) < (M[i].hE.size): 
                M[i].Q  = [np.eye(M[i].l)]
                M[i].hE = M[i].hE[0].reshape((1,1))

            if (M[i].hE.size) > (M[i].hC.size): 
                M[i].hC = np.eye(len(M[i].Q)) * M[i].hC[0]
            
            if len(M[i].R) > (M[i].gE.size): 
                M[i].gE = np.zeros((len(M[i].R), 1)) + M[i].gE[0]
            elif len(M[i].R) < (M[i].gE.size): 
                M[i].R = [np.eye(M[i].n)]
                M[i].gE = M[i].gE[0].reshape((1,1))
            
            if (M[i].gE.size) > (M[i].gC.size): 
                M[i].gC = np.eye(len(M[i].R)) * M[i].gC[0]

            # check consistency and sizes (Q)
            # -------------------------------
            for j in range(len(M[i].Q)):
                if len(M[i].Q[j]) != M[i].l: 
                    raise ValueError(f"Wrong shape for model[{i}].Q[{i}]"
                                     f"(expected ({M[i].l},{M[i].l}), got {M[i].Q[j].shape})")
            
            # check consistency and sizes (R)
            # -------------------------------
            for j in range(len(M[i].R)):
                if len(M[i].R[j]) != M[i].n: 
                    raise ValueError(f"Wrong shape for model[{i}].R[{i}]"
                                     f"(expected ({M[i].n},{M[i].n}), got {M[i].R[j].shape})")
            
            # check V and W (lower bound on precisions)
            # -----------------------------------------
            if len(M[i].V.shape) == 1 and len(M[i].V) == M[i].l: 
                M[i].V = np.diag(M[i].V)
            elif len(M[i].V) != M[i].l:
                try: 
                    M[i].V = np.eye(M[i].l) * M[i].V[0]
                except:
                    if len(M[i].hE) == 0:
                        M[i].V = np.eye(M[i].l)
                    else: 
                        M[i].V = np.zeros((M[i].l, M[i].l))

            if len(M[i].W.shape) == 1 and len(M[i].W) == M[i].n: 
                M[i].W = np.diag(M[i].W)
            elif len(M[i].W) != M[i].n:
                try: 
                    M[i].W = np.eye(M[i].n) * M[i].W[0]
                except:
                    if len(M[i].gE) == 0:
                        M[i].W = np.eye(M[i].n)
                    else: 
                        M[i].W = np.zeros((M[i].n,M[i].n))

            # check smoothness parameter
            s = 0 if nx == 0 else 1/2.
            M[i].sv = s if M[i].sv is None else M[i].sv
            M[i].sw = s if M[i].sw is None else M[i].sw

        return M