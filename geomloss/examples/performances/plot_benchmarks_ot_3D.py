r"""
Wasserstein distances between 3D meshes
==========================================================

Let's compare the performances of several OT solvers
on subsampled versions of the `Stanford dragon <http://graphics.stanford.edu/data/3Dscanrep/>`_,
a standard test surface made up of more than **870,000 triangles**.
In this benchmark, we measure timings on a simple registration task:
the **optimal transport of a sphere onto the (subsampled) dragon**, using
a quadratic ground cost 
:math:`\text{C}(x,y) = \tfrac{1}{2}\|x-y\|^2`
in the ambient space :math:`\mathbb{R}^3`.

"""

######################################################################
# More precisely: having loaded and represented our 3D meshes
# as discrete probability measures
#
# .. math::
#   \alpha ~=~ \sum_{i=1}^N \alpha_i\,\delta_{x_i}, ~~~
#   \beta  ~=~ \sum_{j=1}^M \beta_j\,\delta_{y_j},
#
# we will strive to solve the primal-dual entropic OT problem:
# 
# .. math::
#   \text{OT}_\varepsilon(\alpha,\beta)~&=~
#       \min_{0 \leqslant \pi \ll \alpha\otimes\beta} ~\langle\text{C},\pi\rangle
#           ~+~\varepsilon\,\text{KL}(\pi,\alpha\otimes\beta) \quad\text{s.t.}~~
#        \pi\,\mathbf{1} = \alpha ~~\text{and}~~ \pi^\intercal \mathbf{1} = \beta\\
#    &=~ \max_{f,g} ~~\langle \alpha,f\rangle + \langle \beta,g\rangle
#         - \varepsilon\langle \alpha\otimes\beta, 
#           \exp \tfrac{1}{\varepsilon}[ f\oplus g - \text{C} ] - 1 \rangle
#
# as fast as possible, optimizing on **dual vectors**:
#
# .. math::
#   F_i ~=~ f(x_i), ~~~ G_j ~=~ g(y_j)
#
# that encode an implicit transport plan:
#
# .. math::
#   \pi ~&=~ \exp \tfrac{1}{\varepsilon}( f\oplus g - \text{C})~\cdot~ \alpha\otimes\beta,\\
#   \text{i.e.}~~\pi_{x_i \leftrightarrow y_j}~&=~ \exp \tfrac{1}{\varepsilon}( F_i + G_j - \text{C}(x_i,y_j))~\cdot~ \alpha_i \beta_j.
#

######################################################################
# Comparing OT solvers with each other
# --------------------------------------
#
# First, let's make some standard imports:

import numpy as np
import torch

use_cuda = torch.cuda.is_available()
tensor = torch.cuda.FloatTensor if use_cuda else torch.FloatTensor
numpy = lambda x : x.detach().cpu().numpy()

from matplotlib import pyplot as plt
from mpl_toolkits.mplot3d import Axes3D


#############################################################
# This tutorial is all about highlighting the differences between
# the GeomLoss solvers, packaged in the :mod:`SamplesLoss <geomloss.SamplesLoss>`
# module, and a standard Sinkhorn (= soft-Auction) loop.

from geomloss import SamplesLoss

#############################################################
#
# Our baseline is provided by a simple **Sinkhorn loop**, implemented
# in the **log-domain** for the sake of numerical stability.
# Using the same code, we provide two backends:
# a **tensorized** PyTorch implementation (which has a quadratic memory footprint)
# and a **scalable** KeOps code (which has a **linear** memory footprint).

from pykeops.torch import LazyTensor

def sinkhorn_loop(a_i, x_i, b_j, y_j, blur = .01, nits = 100, backend = "keops"):
    """Straightforward implementation of the Sinkhorn-IPFP-SoftAssign loop in the log domain."""

    # Compute the logarithm of the weights (needed in the softmin reduction) ---
    loga_i, logb_j = a_i.log(), b_j.log()
    loga_i, logb_j = loga_i[:,None,None], logb_j[None,:,None]

    # Compute the cost matrix C_ij = (1/2) * |x_i-y_j|^2 -----------------------
    if backend == "keops":  # C_ij is a *symbolic* LazyTensor
        x_i, y_j = LazyTensor( x_i[:,None,:] ), LazyTensor( y_j[None,:,:] )
        C_ij = ((x_i - y_j) ** 2).sum(-1) / 2  # (N,M,1) LazyTensor
        
    elif backend == "pytorch":  # C_ij is a *full* Tensor, with a quadratic memory footprint
        # N.B.: The separable implementation below is slightly more efficient than:
        # C_ij = ((x_i[:,None,:] - y_j[None,:,:]) ** 2).sum(-1) / 2

        D_xx = (x_i ** 2).sum(-1)[:,None]  # (N,1)
        D_xy = x_i@y_j.t()   # (N,D)@(D,M) = (N,M)
        D_yy = (y_j ** 2).sum(-1)[None,:]  # (1,M)
        C_ij = (D_xx + D_yy) / 2 - D_xy    # (N,M) matrix of halved squared distances

        C_ij = C_ij[:,:,None]  # reshape as a (N,M,1) Tensor

    # Setup the dual variables -------------------------------------------------
    eps = blur ** 2  # "Temperature" epsilon associated to our blurring scale
    F_i, G_j = torch.zeros_like(loga_i), torch.zeros_like(logb_j)  # (scaled) dual vectors

    # Sinkhorn loop = coordinate ascent on the dual maximization problem -------
    for _ in range(nits): 
        F_i = - ( (- C_ij / eps + (G_j + logb_j) ) ).logsumexp(dim=1)[:,None,:]
        G_j = - ( (- C_ij / eps + (F_i + loga_i) ) ).logsumexp(dim=0)[None,:,:]
    
    # Return the dual vectors F and G, sampled on the x_i's and y_j's respectively:
    return eps * F_i, eps * G_j  

# Create a sinkhorn_solver "layer" with the same signature as SamplesLoss:
from functools import partial
sinkhorn_solver = lambda blur, nits, backend: partial(sinkhorn_loop, blur=blur, nits=nits, backend=backend)


################################################################################
# Benchmarking loops
# ------------------------
#
# As usual, writing up a proper benchmark requires a lot of verbose,
# not-so-interesting code. For the sake of readabiliity, key routines are
# abstracted in a separate :doc:`file <./benchmarks_ot_solvers>`
# where error functions, timers and Wasserstein distances are properly defined.
# Feel free to have a look!


from benchmarks_ot_solvers import benchmark_solver, benchmark_solvers

######################################################################
# The GeomLoss routines rely on a **scaling** parameter to tune
# the tradeoff between **speed** (scaling :math:`\rightarrow` 0)
# and **accuracy** (scaling :math:`\rightarrow` 1).
# Meanwhile, the Sinkhorn loop is directly controlled
# by the **number of iterations** that should be chosen with respect to
# the available time budget.


def full_benchmark(source, target, blur, maxtime=None):

    # Compute a suitable "ground truth" ----------------------------------------
    OT_solver = SamplesLoss("sinkhorn", p=2, blur=blur, backend="online",
                            scaling=.999, debias=False, potentials=True)
    _, _, ground_truth = benchmark_solver(OT_solver, blur, sources[0], targets[0])

    results = {}  # Dict of "timings vs errors" arrays

    # Compute statistics for the three backends of GeomLoss: -------------------
    
    for name in ["multiscale-3", "multiscale-5", "online", "tensorized"]:
        if   name == "multiscale-3": backend, truncate = "multiscale", 3
        elif name == "multiscale-5": backend, truncate = "multiscale", 5
        else:                        backend, truncate = name, None

        OT_solvers = [ SamplesLoss("sinkhorn", p=2, blur=blur, scaling=scaling, truncate=truncate,
                                   backend=backend, debias=False, potentials=True)
                       for scaling in [.5, .6, .7, .8, .9, .95, .99] ]

        results[name] = benchmark_solvers("GeomLoss - " + name, OT_solvers, 
                                             source, target, ground_truth, 
                                             blur=blur, display=False, maxtime=maxtime)


    # Compute statistics for a naive Sinkhorn loop -----------------------------

    for backend in ["pytorch", "keops"]:
        OT_solvers = [ sinkhorn_solver(blur, nits = nits, backend = backend)
                       for nits in [5, 10, 20, 50, 100, 200, 500, 1000, 2000, 5000, 10000] ]

        results[backend] = benchmark_solvers("Sinkhorn loop - " + backend, OT_solvers, 
                                             source, target, ground_truth, 
                                             blur = blur, display=False, maxtime=maxtime)
    
    return results, ground_truth

###############################################################################
# Having solved the entropic OT problem with dozens of solvers,
# we will display our results in an "error vs timing" log-log plot:
# 

def display_statistics(title, results, ground_truth, maxtime=None):
    """Displays a "error vs timing" plot in log-log scale."""

    curves = [ ("pytorch",    "Sinkhorn loop - PyTorch backend"),
               ("keops",      "Sinkhorn loop - KeOps backend"),
               ("tensorized", "Sinkhorn with ε-scaling - PyTorch backend"),
               ("online",     "Sinkhorn with ε-scaling - KeOps backend"),
               ("multiscale-3", "Sinkhorn multiscale - truncate=3 (fast)"),
               ("multiscale-5", "Sinkhorn multiscale - truncate=5 (safe)"),
                ]

    fig = plt.figure(figsize=(12,8))
    ax = fig.subplots()
    ax.set_title(title)
    ax.set_ylabel("Relative error made on the entropic Wasserstein distance")
    ax.set_yscale('log') ; ax.set_ylim(top=1e-1, bottom=1e-3)
    ax.set_xlabel("Time (s)")
    ax.set_xscale('log') ; ax.set_xlim(left=1e-3, right=maxtime)

    ax.grid(True, which="major", linestyle="-")
    ax.grid(True, which="minor", linestyle="dotted")

    for key, name in curves:
        timings, errors, costs = results[key]
        ax.plot( timings, np.abs(costs - ground_truth), label = name)

    ax.legend(loc='upper right')
    

def full_statistics(source, target, blur=.01, maxtime=None):
    results, ground_truth = full_benchmark(source, target, blur, maxtime=maxtime)

    display_statistics(
        "Solving a {:,}-by-{:,} OT problem, with a blurring scale σ = {:}".format(len(source[0]), len(target[0]), blur),
        results, ground_truth, maxtime=maxtime)
    
    return results, ground_truth




##############################################
# Building our dataset
# ----------------------------
#
# Our **source measures**: unit spheres, sampled with (roughly) the same number of points
# as the target meshes.
#

from benchmarks_ot_solvers import create_sphere

sources = [ create_sphere( npoints ) for npoints in [1e4, 5e4, 2e5, 8e5] ]

###########################################################
# Then, we fetch our target models from the Stanford repository:

import os

if not os.path.exists('data/dragon_recon/dragon_vrip_res4.ply'):
    import urllib.request
    urllib.request.urlretrieve(
        'http://graphics.stanford.edu/pub/3Dscanrep/dragon/dragon_recon.tar.gz', 
        'data/dragon.tar.gz')

    import shutil
    shutil.unpack_archive('data/dragon.tar.gz', 'data')

##############################################
# To read the raw ``.ply`` ascii files, we rely on the
# `plyfile <https://github.com/dranjan/python-plyfile>`_ package:

from benchmarks_ot_solvers import load_ply_file, display_cloud


############################################################
# Our meshes are encoded using **one weighted Dirac mass per triangle**.
# To keep things simple, we use as **targets** the subsamplings provided
# in the reference Stanford archive. Feel free to re-run
# this script with your own models!
#

# N.B.: Since Plyfile is far from being optimized, this may take some time!
targets = [ load_ply_file( fname, offset = [-0.011,  0.109, -0.008], scale = .04 ) 
            for fname in 
            ['data/dragon_recon/dragon_vrip_res4.ply', # ~ 10,000 triangles
             'data/dragon_recon/dragon_vrip_res3.ply', # ~ 50,000 triangles
             'data/dragon_recon/dragon_vrip_res2.ply', # ~200,000 triangles
             #'data/dragon_recon/dragon_vrip.ply',     # ~800,000 triangles
          ] ]


################################################################################
# Finally, if we don't have access to a GPU, we subsample point clouds
# while making sure that weights still sum up to one:

def subsample(measure, decimation=500):
    weights, locations = measure
    weights, locations = weights[::decimation], locations[::decimation]
    weights = weights / weights.sum()
    return weights.contiguous(), locations.contiguous()

if not use_cuda:
    sources = [subsample(s) for s in sources]
    targets = [subsample(t) for t in targets]


############################################################
# In this simple benchmark, we will only use the **coarse** and **medium** resolutions
# of our meshes: 200,000 points should be more than enough to compute
# sensible approximations of the Wasserstein distance between the Stanford dragon and a unit sphere!


fig = plt.figure(figsize=(12,12))
ax = fig.add_subplot(1, 1, 1, projection='3d')
display_cloud(ax, sources[0], 'red')
display_cloud(ax, targets[0], 'blue')
ax.set_title("Low resolution dataset:\n" \
            +"Source (N={:,}) and target (M={:,}) point clouds".format(len(sources[0][0]), len(targets[0][0])))
plt.tight_layout()

# sphinx_gallery_thumbnail_number = 2
fig = plt.figure(figsize=(12,12))
ax = fig.add_subplot(1, 1, 1, projection='3d')
display_cloud(ax, sources[2], 'red')
display_cloud(ax, targets[2], 'blue')
ax.set_title("Medium resolution dataset:\n" \
            +"Source (N={:,}) and target (M={:,}) point clouds".format(len(sources[2][0]), len(targets[2][0])))
plt.tight_layout()


################################################################################
# Benchmarks
# ----------------------------
#
# 
#

maxtime = 100 if use_cuda else 1

full_statistics(sources[0], targets[0], blur=.10, maxtime=maxtime)

################################################################################
#

full_statistics(sources[0], targets[0], blur=.01, maxtime=maxtime)

################################################################################
# Blabla

full_statistics(sources[2], targets[2], blur=.01, maxtime=maxtime)


plt.show()