"""Functions used in find_orientations"""
from __future__ import print_function, division, absolute_import

import os
import time
import logging
import multiprocessing as mp

import numpy as np
import timeit
from scipy import ndimage, cluster

have_sklearn = False
try:
    import sklearn
    vstring = sklearn.__version__.split('.')
    if vstring[0] == '0' and int(vstring[1]) >= 14:
        from sklearn.cluster import dbscan
        from sklearn.metrics.pairwise import pairwise_distances
        have_sklearn = True
except ImportError:
    pass


from hexrd import instrument
from hexrd import matrixutil as mutil
from hexrd.xrd.xrdutil import EtaOmeMaps
from hexrd.xrd import transforms_CAPI as xfcapi
from hexrd.xrd import rotations as rot

print (__name__)
logger = logging.getLogger(__name__)


# ==================== Functions


def analysis_id(cfg):
    return '%s_%s' % (
        cfg.analysis_name.strip().replace(' ', '-'),
        cfg.material.active.strip().replace(' ', '-')
    )


def get_eta_ome(cfg, clean=False):
    """Return eta-omega maps"""
    # make eta_ome maps
    imsd = cfg.image_series
    instr = cfg.instrument.hedm
    plane_data = cfg.material.plane_data
    active_hkls = cfg.find_orientations.orientation_maps.active_hkls
    build_map_threshold = cfg.find_orientations.orientation_maps.threshold
    ome_period = np.radians(cfg.find_orientations.omega.period)

    # Use existing ones if available
    maps_fname = cfg.find_orientations.orientation_maps.file
    if maps_fname is None:
        maps_fname = "_".join(
            [analysis_id(cfg),
             "t" + str(build_map_threshold),
             "maps.npz"]
        )
    if os.path.exists(maps_fname) and not clean:
        print("INFO: loading existing eta_ome maps")
        eta_ome = EtaOmeMaps(maps_fname)
        return eta_ome

    print("INFO: building eta_ome maps")
    start = timeit.default_timer()

    # trim plane_data to only use active hkls
    if active_hkls is not None:
        excl = np.ones_like(plane_data.exclusions, dtype=bool)
        excl[active_hkls] = False
        plane_data.exclusions = excl
        
    eta_ome = instrument.GenerateEtaOmeMaps(
        imsd, instr, plane_data,
        active_hkls=active_hkls,
        threshold=build_map_threshold,
        ome_period=cfg.find_orientations.omega.period
    )

    print("INFO:  ...took %f seconds" % (timeit.default_timer() - start))

    # save them
    eta_ome.save(maps_fname)

    return eta_ome

# ============================== Fibers

def generate_orientation_fibers(
        eta_ome, chi, threshold, seed_hkl_ids, fiber_ndiv,
        filt_stdev=0.8, ncpus=1):
    """
    From ome-eta maps and hklid spec, generate list of
    quaternions from fibers
    """
    # seed_hkl_ids must be consistent with this...
    pd_hkl_ids = eta_ome.iHKLList[seed_hkl_ids]

    # grab angular grid infor from maps
    del_ome = eta_ome.omegas[1] - eta_ome.omegas[0]
    del_eta = eta_ome.etas[1] - eta_ome.etas[0]

    # labeling mask
    structureNDI_label = ndimage.generate_binary_structure(2, 1)

    # crystallography data from the pd object
    pd = eta_ome.planeData
    hkls = pd.hkls
    tTh = pd.getTTh()
    bMat = pd.latVecOps['B']
    csym = pd.getLaueGroup()

    params = dict(
        bMat=bMat,
        chi=chi,
        csym=csym,
        fiber_ndiv=fiber_ndiv)

    # =========================================================================
    # Labeling of spots from seed hkls
    # =========================================================================

    qfib = []
    input_p = []
    numSpots = []
    coms = []
    for i in seed_hkl_ids:
        # First apply filter
        this_map_f = -ndimage.filters.gaussian_laplace(
            eta_ome.dataStore[i], filt_stdev)

        labels_t, numSpots_t = ndimage.label(
            this_map_f > threshold,
            structureNDI_label
            )
        coms_t = np.atleast_2d(
            ndimage.center_of_mass(
                this_map_f,
                labels=labels_t,
                index=np.arange(1, np.amax(labels_t)+1)
                )
            )
        numSpots.append(numSpots_t)
        coms.append(coms_t)
        pass

    for i in range(len(pd_hkl_ids)):
        for ispot in range(numSpots[i]):
            if not np.isnan(coms[i][ispot][0]):
                ome_c = eta_ome.omeEdges[0] + (0.5 + coms[i][ispot][0])*del_ome
                eta_c = eta_ome.etaEdges[0] + (0.5 + coms[i][ispot][1])*del_eta
                input_p.append(
                    np.hstack(
                        [hkls[:, pd_hkl_ids[i]],
                         tTh[pd_hkl_ids[i]], eta_c, ome_c]
                    )
                )
                pass
            pass
        pass

    # do the mapping
    start = time.time()
    qfib = None
    if ncpus > 1:
        # multiple process version
        # QUESTION: Need a chunksize?
        pool = mp.Pool(ncpus, discretefiber_init, (params, ))
        qfib = pool.map(discretefiber_reduced, input_p)  # chunksize=chunksize)
        pool.close()
    else:
        # single process version.
        global paramMP
        discretefiber_init(params)  # sets paramMP
        qfib = map(discretefiber_reduced, input_p)
        paramMP = None  # clear paramMP
    elapsed = (time.time() - start)
    logger.info("fiber generation took %.3f seconds", elapsed)
    return np.hstack(qfib)


def discretefiber_init(params):
    global paramMP
    paramMP = params


def discretefiber_reduced(params_in):
    """
    input parameters are [hkl_id, com_ome, com_eta]
    """
    bMat       = paramMP['bMat']
    chi        = paramMP['chi']
    csym       = paramMP['csym']
    fiber_ndiv = paramMP['fiber_ndiv']

    hkl = params_in[:3].reshape(3, 1)

    gVec_s = xfcapi.anglesToGVec(
        np.atleast_2d(params_in[3:]),
        chi=chi,
        ).T

    tmp = mutil.uniqueVectors(
        rot.discreteFiber(
            hkl,
            gVec_s,
            B=bMat,
            ndiv=fiber_ndiv,
            invert=False,
            csym=csym
            )[0]
        )
    return tmp

def run_cluster(compl, qfib, qsym, cfg, min_samples=None, compl_thresh=None, radius=None):
    """
    """
    algorithm = cfg.find_orientations.clustering.algorithm

    # check for override on completeness threshold
    if compl_thresh is not None:
        min_compl = compl_thresh

    # check for override on radius
    if radius is not None:
        cl_radius = radius

    start = time.clock() # time this

    num_above = sum(np.array(compl) > min_compl)
    if num_above == 0:
        # nothing to cluster
        qbar = cl = np.array([])
    elif num_above == 1:
        # short circuit
        qbar = qfib[:, np.array(compl) > min_compl]
        cl = [1]
    else:
        # use compiled module for distance
        # just to be safe, must order qsym as C-contiguous
        qsym  = np.array(qsym.T, order='C').T
        def quat_distance(x, y):
            return xfcapi.quat_distance(np.array(x, order='C'), np.array(y, order='C'), qsym)

        qfib_r = qfib[:, np.array(compl) > min_compl]

        num_ors = qfib_r.shape[1]

        if num_ors > 25000:
            if algorithm == 'sph-dbscan' or algorithm == 'fclusterdata':
                logger.info("falling back to euclidean DBSCAN")
                algorithm = 'ort-dbscan'
            #raise RuntimeError, \
            #    "Requested clustering of %d orientations, which would be too slow!" %qfib_r.shape[1]

        logger.info(
            "Feeding %d orientations above %.1f%% to clustering",
            num_ors, 100*min_compl
            )

        if algorithm == 'dbscan' and not have_sklearn:
            algorithm = 'fclusterdata'
            logger.warning(
                "sklearn >= 0.14 required for dbscan; using fclusterdata"
                )

        if algorithm == 'dbscan' or algorithm == 'ort-dbscan' or algorithm == 'sph-dbscan':
            # munge min_samples according to options
            if min_samples is None or cfg.find_orientations.use_quaternion_grid is not None:
                min_samples = 1

            if algorithm == 'sph-dbscan':
                logger.info("using spherical DBSCAN")
                # compute distance matrix
                pdist = pairwise_distances(
                    qfib_r.T, metric=quat_distance, n_jobs=1
                    )

                # run dbscan
                core_samples, labels = dbscan(
                    pdist,
                    eps=np.radians(cl_radius),
                    min_samples=min_samples,
                    metric='precomputed'
                    )
            else:
                if algorithm == 'ort-dbscan':
                    logger.info("using euclidean orthographic DBSCAN")
                    pts = qfib_r[1:, :].T
                    eps = 0.25*np.radians(cl_radius)
                else:
                    logger.info("using euclidean DBSCAN")
                    pts = qfib_r.T
                    eps = 0.5*np.radians(cl_radius)

                # run dbscan
                core_samples, labels = dbscan(
                    pts,
                    eps=eps,
                    min_samples=min_samples,
                    metric='minkowski', p=2,
                    )

            # extract cluster labels
            cl = np.array(labels, dtype=int) # convert to array
            noise_points = cl == -1 # index for marking noise
            cl += 1 # move index to 1-based instead of 0
            cl[noise_points] = -1 # re-mark noise as -1
            logger.info("dbscan found %d noise points", sum(noise_points))
        elif algorithm == 'fclusterdata':
            logger.info("using spherical fclusetrdata")
            cl = cluster.hierarchy.fclusterdata(
                qfib_r.T,
                np.radians(cl_radius),
                criterion='distance',
                metric=quat_distance
                )
        else:
            raise RuntimeError(
                "Clustering algorithm %s not recognized" % algorithm
                )

        # extract number of clusters
        if np.any(cl == -1):
            nblobs = len(np.unique(cl)) - 1
        else:
            nblobs = len(np.unique(cl))

        """ PERFORM AVERAGING TO GET CLUSTER CENTROIDS """
        qbar = np.zeros((4, nblobs))
        for i in range(nblobs):
            npts = sum(cl == i + 1)
            qbar[:, i] = rot.quatAverageCluster(
                qfib_r[:, cl == i + 1].reshape(4, npts), qsym
            ).flatten()
            pass
        pass

    if (algorithm == 'dbscan' or algorithm == 'ort-dbscan') \
      and qbar.size/4 > 1:
        logger.info("\tchecking for duplicate orientations...")
        cl = cluster.hierarchy.fclusterdata(
            qbar.T,
            np.radians(cl_radius),
            criterion='distance',
            metric=quat_distance)
        nblobs_new = len(np.unique(cl))
        if nblobs_new < nblobs:
            logger.info("\tfound %d duplicates within %f degrees" \
                        %(nblobs-nblobs_new, cl_radius))
            tmp = np.zeros((4, nblobs_new))
            for i in range(nblobs_new):
                npts = sum(cl == i + 1)
                tmp[:, i] = rot.quatAverageCluster(
                    qbar[:, cl == i + 1].reshape(4, npts), qsym
                ).flatten()
                pass
            qbar = tmp
            pass
        pass

    logger.info("clustering took %f seconds", time.clock() - start)
    logger.info(
        "Found %d orientation clusters with >=%.1f%% completeness"
        " and %2f misorientation",
        qbar.size/4,
        100.*min_compl,
        cl_radius
        )

    return np.atleast_2d(qbar), cl
