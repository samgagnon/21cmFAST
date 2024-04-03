"""
The main wrapper for the underlying 21cmFAST C-code.

The module provides both low- and high-level wrappers, using the very low-level machinery
in :mod:`~py21cmfast._utils`, and the convenient input and output structures from
:mod:`~py21cmfast.inputs` and :mod:`~py21cmfast.outputs`.

This module provides a number of:

* Low-level functions which simplify calling the background C functions which populate
  these output objects given the input classes.
* High-level functions which provide the most efficient and simplest way to generate the
  most commonly desired outputs.

**Low-level functions**

The low-level functions provided here ease the production of the aforementioned output
objects. Functions exist for each low-level C routine, which have been decoupled as far
as possible. So, functions exist to create :func:`initial_conditions`,
:func:`perturb_field`, :class:`ionize_box` and so on. Creating a brightness temperature
box (often the desired final output) would generally require calling each of these in
turn, as each depends on the result of a previous function. Nevertheless, each function
has the capability of generating the required previous outputs on-the-fly, so one can
instantly call :func:`ionize_box` and get a self-consistent result. Doing so, while
convenient, is sometimes not *efficient*, especially when using inhomogeneous
recombinations or the spin temperature field, which intrinsically require consistent
evolution of the ionization field through redshift. In these cases, for best efficiency
it is recommended to either use a customised manual approach to calling these low-level
functions, or to call a higher-level function which optimizes this process.

Finally, note that :mod:`py21cmfast` attempts to optimize the production of the large amount of
data via on-disk caching. By default, if a previous set of data has been computed using
the current input parameters, it will be read-in from a caching repository and returned
directly. This behaviour can be tuned in any of the low-level (or high-level) functions
by setting the `write`, `direc`, `regenerate` and `match_seed` parameters (see docs for
:func:`initial_conditions` for details). The function :func:`~query_cache` can be used
to search the cache, and return empty datasets corresponding to each (and these can then be
filled with the data merely by calling ``.read()`` on any data set). Conversely, a
specific data set can be read and returned as a proper output object by calling the
:func:`~py21cmfast.cache_tools.readbox` function.


**High-level functions**

As previously mentioned, calling the low-level functions in some cases is non-optimal,
especially when full evolution of the field is required, and thus iteration through a
series of redshift. In addition, while :class:`InitialConditions` and
:class:`PerturbedField` are necessary intermediate data, it is *usually* the resulting
brightness temperature which is of most interest, and it is easier to not have to worry
about the intermediate steps explicitly. For these typical use-cases, two high-level
functions are available: :func:`run_coeval` and :func:`run_lightcone`, whose purpose
should be self-explanatory. These will optimally run all necessary intermediate
steps (using cached results by default if possible) and return all datasets of interest.


Examples
--------
A typical example of using this module would be the following.

>>> import py21cmfast as p21

Get coeval cubes at redshift 7,8 and 9, without spin temperature or inhomogeneous
recombinations:

>>> coeval = p21.run_coeval(
>>>     redshift=[7,8,9],
>>>     cosmo_params=p21.CosmoParams(hlittle=0.7),
>>>     user_params=p21.UserParams(HII_DIM=100)
>>> )

Get coeval cubes at the same redshift, with both spin temperature and inhomogeneous
recombinations, pulled from the natural evolution of the fields:

>>> all_boxes = p21.run_coeval(
>>>                 redshift=[7,8,9],
>>>                 user_params=p21.UserParams(HII_DIM=100),
>>>                 flag_options=p21.FlagOptions(INHOMO_RECO=True),
>>>                 do_spin_temp=True
>>>             )

Get a self-consistent lightcone defined between z1 and z2 (`z_step_factor` changes the
logarithmic steps between redshift that are actually evaluated, which are then
interpolated onto the lightcone cells):

>>> lightcone = p21.run_lightcone(redshift=z2, max_redshift=z2, z_step_factor=1.03)
"""

from __future__ import annotations

import logging
import numpy as np
import os
import warnings
from astropy import units as un
from astropy.cosmology import z_at_value
from copy import deepcopy
from pathlib import Path
from scipy.interpolate import interp1d
from typing import Any, Callable, Sequence

from ._cfg import config
from ._utils import OutputStruct, _check_compatible_inputs, _process_exitcode, asarray
from .c_21cmfast import ffi, lib
from .cache_tools import get_boxes_at_redshift
from .inputs import (
    AstroParams,
    CosmoParams,
    FlagOptions,
    UserParams,
    global_params,
    validate_all_inputs,
)
from .lightcones import Lightconer, RectilinearLightconer
from .outputs import (
    AngularLightcone,
    BrightnessTemp,
    Coeval,
    HaloBox,
    HaloField,
    InitialConditions,
    IonizedBox,
    LightCone,
    PerturbedField,
    PerturbHaloField,
    TsBox,
    XraySourceBox,
    _OutputStructZ,
)
from .photoncons import (
    _get_photon_nonconservation_data,
    get_photoncons_dz,
    setup_photon_cons,
)

logger = logging.getLogger(__name__)


def _configure_inputs(
    defaults: list,
    *datasets,
    ignore: list = ["redshift"],
    flag_none: list | None = None,
):
    """Configure a set of input parameter structs.

    This is useful for basing parameters on a previous output.
    The logic is this: the struct _cannot_ be present and different in both defaults and
    a dataset. If it is present in _either_ of them, that will be returned. If it is
    present in _neither_, either an error will be raised (if that item is in `flag_none`)
    or it will pass.

    Parameters
    ----------
    defaults : list of 2-tuples
        Each tuple is (key, val). Keys are input struct names, and values are a default
        structure for that input.
    datasets : list of :class:`~_utils.OutputStruct`
        A number of output datasets to cross-check, and draw parameter values from.
    ignore : list of str
        Attributes to ignore when ensuring that parameter inputs are the same.
    flag_none : list
        A list of parameter names for which ``None`` is not an acceptable value.

    Raises
    ------
    ValueError :
        If an input parameter is present in both defaults and the dataset, and is different.
        OR if the parameter is present in neither defaults not the datasets, and it is
        included in `flag_none`.
    """
    # First ensure all inputs are compatible in their parameters
    _check_compatible_inputs(*datasets, ignore=ignore)

    if flag_none is None:
        flag_none = []

    output = [0] * len(defaults)
    for i, (key, val) in enumerate(defaults):
        # Get the value of this input from the datasets
        data_val = None
        for dataset in datasets:
            if dataset is not None and hasattr(dataset, key):
                data_val = getattr(dataset, key)
                break

        # If both data and default have values
        if not (val is None or data_val is None or data_val == val):
            raise ValueError(
                "%s has an inconsistent (%s,%s) value with %s"
                % (key, val, data_val, dataset.__class__.__name__)
            )
        else:
            if val is not None:
                output[i] = val
            elif data_val is not None:
                output[i] = data_val
            elif key in flag_none:
                raise ValueError(
                    "For %s, a value must be provided in some manner" % key
                )
            else:
                output[i] = None

    return output


def configure_redshift(redshift, *structs):
    """
    Check and obtain a redshift from given default and structs.

    Parameters
    ----------
    redshift : float
        The default redshift to use
    structs : list of :class:`~_utils.OutputStruct`
        A number of output datasets from which to find the redshift.

    Raises
    ------
    ValueError :
        If both `redshift` and *all* structs have a value of `None`, **or** if any of them
        are different from each other (and not `None`).
    """
    zs = {s.redshift for s in structs if s is not None and hasattr(s, "redshift")}
    zs = list(zs)

    if len(zs) > 1 or (
        len(zs) == 1
        and redshift is not None
        and not np.isclose(zs[0], redshift, atol=1e-5)
    ):
        raise ValueError("Incompatible redshifts in inputs")
    elif len(zs) == 1:
        return zs[0]
    elif redshift is None:
        raise ValueError(
            "Either redshift must be provided, or a data set containing it."
        )
    else:
        return redshift


def _verify_types(**kwargs):
    """Ensure each argument has a type of None or that matching its name."""
    for k, v in kwargs.items():
        for j, kk in enumerate(
            [
                "init",
                "perturb",
                "ionize",
                "spin_temp",
                "halo_field",
                "pt_halos",
                "halobox",
                "xray_source_box",
            ]
        ):
            if kk in k:
                break
        cls = [
            InitialConditions,
            PerturbedField,
            IonizedBox,
            TsBox,
            HaloField,
            PerturbHaloField,
            HaloBox,
            XraySourceBox,
        ][j]

        if v is not None and not isinstance(v, cls):
            raise ValueError(f"{k} must be an instance of {cls.__name__}")


def _setup_inputs(
    input_params: dict[str, Any],
    input_boxes: dict[str, OutputStruct] | None = None,
    redshift=-1,
):
    """
    Verify and set up input parameters to any function that runs C code.

    Parameters
    ----------
    input_boxes
        A dictionary of OutputStruct objects that are meant as inputs to the current
        calculation. These will be verified against each other, and also used to
        determine redshift, if appropriate.
    input_params
        A dictionary of keys and dicts / input structs. This should have the random
        seed, cosmo/user params and optionally the flag and astro params.
    redshift
        Optional value of the redshift. Can be None. If not provided, no redshift is
        returned.

    Returns
    -------
    random_seed
        The random seed to use, determined from either explicit input or input boxes.
    input_params
        The configured input parameter structs, in the order in which they were given.
    redshift
        If redshift is given, it will also be output.
    """
    input_boxes = input_boxes or {}

    if "flag_options" in input_params and "user_params" not in input_params:
        raise ValueError("To set flag_options requires user_params")
    if "astro_params" in input_params and "flag_options" not in input_params:
        raise ValueError("To set astro_params requires flag_options")

    if input_boxes:
        _verify_types(**input_boxes)

    params = _configure_inputs(list(input_params.items()), *list(input_boxes.values()))

    if redshift != -1:
        redshift = configure_redshift(
            redshift,
            *[
                v
                for k, v in input_boxes.items()
                if hasattr(v, "redshift") and "prev" not in k
            ],
        )

    # This turns params into a dict with all the input parameters in it.
    params = dict(zip(input_params.keys(), params))

    params["user_params"] = UserParams(params["user_params"])
    params["cosmo_params"] = CosmoParams(params["cosmo_params"])

    if "flag_options" in params:
        params["flag_options"] = FlagOptions(params["flag_options"])

    if "astro_params" in params:
        params["astro_params"] = AstroParams(
            params["astro_params"], INHOMO_RECO=params["flag_options"].INHOMO_RECO
        )

    # Perform validation between different sets of inputs.
    validate_all_inputs(**{k: v for k, v in params.items() if k != "random_seed"})

    # Sort the params back into input order.
    params = [params[k] for k in input_params]

    out = params
    if redshift != -1:
        out.append(redshift)

    return out


def _call_c_simple(fnc, *args):
    """Call a simple C function that just returns an object.

    Any such function should be defined such that the last argument is an int pointer generating
    the status.
    """
    # Parse the function to get the type of the last argument
    cdata = str(ffi.addressof(lib, fnc.__name__))
    kind = cdata.split("(")[-1].split(")")[0].split(",")[-1]
    result = ffi.new(kind)
    status = fnc(*args, result)
    _process_exitcode(status, fnc, args)
    return result[0]


def _get_config_options(
    direc, regenerate, write, hooks
) -> tuple[str, bool, dict[Callable, dict[str, Any]]]:
    direc = str(os.path.expanduser(config["direc"] if direc is None else direc))

    if hooks is None or len(hooks) > 0:
        hooks = hooks or {}

        if callable(write) and write not in hooks:
            hooks[write] = {"direc": direc}

        if not hooks:
            if write is None:
                write = config["write"]

            if not callable(write) and write:
                hooks["write"] = {"direc": direc}

    return (
        direc,
        bool(config["regenerate"] if regenerate is None else regenerate),
        hooks,
    )


def get_all_fieldnames(
    arrays_only=True, lightcone_only=False, as_dict=False
) -> dict[str, str] | set[str]:
    """Return all possible fieldnames in output structs.

    Parameters
    ----------
    arrays_only : bool, optional
        Whether to only return fields that are arrays.
    lightcone_only : bool, optional
        Whether to only return fields from classes that evolve with redshift.
    as_dict : bool, optional
        Whether to return results as a dictionary of ``quantity: class_name``.
        Otherwise returns a set of quantities.
    """
    classes = [cls(redshift=0, dummy=True) for cls in _OutputStructZ._implementations()]

    if not lightcone_only:
        classes.append(InitialConditions())

    attr = "pointer_fields" if arrays_only else "fieldnames"

    if as_dict:
        return {
            name: cls.__class__.__name__
            for cls in classes
            for name in getattr(cls, attr)
        }
    else:
        return {name for cls in classes for name in getattr(cls, attr)}


# ======================================================================================
# WRAPPING FUNCTIONS
# ======================================================================================
def construct_fftw_wisdoms(*, user_params=None, cosmo_params=None):
    """Construct all necessary FFTW wisdoms.

    Parameters
    ----------
    user_params : :class:`~inputs.UserParams`
        Parameters defining the simulation run.

    """
    user_params = UserParams(user_params)
    cosmo_params = CosmoParams(cosmo_params)

    # Run the C code
    if user_params.USE_FFTW_WISDOM:
        return lib.CreateFFTWWisdoms(user_params(), cosmo_params())
    else:
        return 0


def compute_tau(*, redshifts, global_xHI, user_params=None, cosmo_params=None):
    """Compute the optical depth to reionization under the given model.

    Parameters
    ----------
    redshifts : array-like
        Redshifts defining an evolution of the neutral fraction.
    global_xHI : array-like
        The mean neutral fraction at `redshifts`.
    user_params : :class:`~inputs.UserParams`
        Parameters defining the simulation run.
    cosmo_params : :class:`~inputs.CosmoParams`
        Cosmological parameters.

    Returns
    -------
    tau : float
        The optional depth to reionization

    Raises
    ------
    ValueError :
        If `redshifts` and `global_xHI` have inconsistent length or if redshifts are not
        in ascending order.
    """
    user_params, cosmo_params = _setup_inputs(
        {"user_params": user_params, "cosmo_params": cosmo_params}
    )

    if len(redshifts) != len(global_xHI):
        raise ValueError("redshifts and global_xHI must have same length")

    if not np.all(np.diff(redshifts) > 0):
        raise ValueError("redshifts and global_xHI must be in ascending order")

    # Convert the data to the right type
    redshifts = np.array(redshifts, dtype="float32")
    global_xHI = np.array(global_xHI, dtype="float32")

    z = ffi.cast("float *", ffi.from_buffer(redshifts))
    xHI = ffi.cast("float *", ffi.from_buffer(global_xHI))

    # Run the C code
    return lib.ComputeTau(user_params(), cosmo_params(), len(redshifts), z, xHI)


def compute_luminosity_function(
    *,
    redshifts,
    user_params=None,
    cosmo_params=None,
    astro_params=None,
    flag_options=None,
    nbins=100,
    mturnovers=None,
    mturnovers_mini=None,
    component=0,
):
    """Compute a the luminosity function over a given number of bins and redshifts.

    Parameters
    ----------
    redshifts : array-like
        The redshifts at which to compute the luminosity function.
    user_params : :class:`~UserParams`, optional
        Defines the overall options and parameters of the run.
    cosmo_params : :class:`~CosmoParams`, optional
        Defines the cosmological parameters used to compute initial conditions.
    astro_params : :class:`~AstroParams`, optional
        The astrophysical parameters defining the course of reionization.
    flag_options : :class:`~FlagOptions`, optional
        Some options passed to the reionization routine.
    nbins : int, optional
        The number of luminosity bins to produce for the luminosity function.
    mturnovers : array-like, optional
        The turnover mass at each redshift for massive halos (ACGs).
        Only required when USE_MINI_HALOS is True.
    mturnovers_mini : array-like, optional
        The turnover mass at each redshift for minihalos (MCGs).
        Only required when USE_MINI_HALOS is True.
    component : int, optional
        The component of the LF to be calculated. 0, 1 an 2 are for the total,
        ACG and MCG LFs respectively, requiring inputs of both mturnovers and
        mturnovers_MINI (0), only mturnovers (1) or mturnovers_MINI (2).

    Returns
    -------
    Muvfunc : np.ndarray
        Magnitude array (i.e. brightness). Shape [nredshifts, nbins]
    Mhfunc : np.ndarray
        Halo mass array. Shape [nredshifts, nbins]
    lfunc : np.ndarray
        Number density of haloes corresponding to each bin defined by `Muvfunc`.
        Shape [nredshifts, nbins].
    """
    user_params, cosmo_params, astro_params, flag_options = _setup_inputs(
        {
            "user_params": user_params,
            "cosmo_params": cosmo_params,
            "astro_params": astro_params,
            "flag_options": flag_options,
        }
    )

    redshifts = np.array(redshifts, dtype="float32")
    if flag_options.USE_MINI_HALOS:
        if component in [0, 1]:
            if mturnovers is None:
                logger.warning(
                    "calculating ACG LFs with mini-halo feature requires users to "
                    "specify mturnovers!"
                )
                return None, None, None

            mturnovers = np.array(mturnovers, dtype="float32")
            if len(mturnovers) != len(redshifts):
                logger.warning(
                    "mturnovers(%d) does not match the length of redshifts (%d)"
                    % (len(mturnovers), len(redshifts))
                )
                return None, None, None
        if component in [0, 2]:
            if mturnovers_mini is None:
                logger.warning(
                    "calculating MCG LFs with mini-halo feature requires users to "
                    "specify mturnovers_MINI!"
                )
                return None, None, None

            mturnovers_mini = np.array(mturnovers_mini, dtype="float32")
            if len(mturnovers_mini) != len(redshifts):
                logger.warning(
                    "mturnovers_MINI(%d) does not match the length of redshifts (%d)"
                    % (len(mturnovers), len(redshifts))
                )
                return None, None, None

    else:
        mturnovers = np.zeros(len(redshifts), dtype="float32") + 10**astro_params.M_TURN
        component = 1

    if component == 0:
        lfunc = np.zeros(len(redshifts) * nbins)
        Muvfunc = np.zeros(len(redshifts) * nbins)
        Mhfunc = np.zeros(len(redshifts) * nbins)

        lfunc.shape = (len(redshifts), nbins)
        Muvfunc.shape = (len(redshifts), nbins)
        Mhfunc.shape = (len(redshifts), nbins)

        c_Muvfunc = ffi.cast("double *", ffi.from_buffer(Muvfunc))
        c_Mhfunc = ffi.cast("double *", ffi.from_buffer(Mhfunc))
        c_lfunc = ffi.cast("double *", ffi.from_buffer(lfunc))

        # Run the C code
        errcode = lib.ComputeLF(
            nbins,
            user_params(),
            cosmo_params(),
            astro_params(),
            flag_options(),
            1,
            len(redshifts),
            ffi.cast("float *", ffi.from_buffer(redshifts)),
            ffi.cast("float *", ffi.from_buffer(mturnovers)),
            c_Muvfunc,
            c_Mhfunc,
            c_lfunc,
        )

        _process_exitcode(
            errcode,
            lib.ComputeLF,
            (
                nbins,
                user_params,
                cosmo_params,
                astro_params,
                flag_options,
                1,
                len(redshifts),
            ),
        )

        lfunc_MINI = np.zeros(len(redshifts) * nbins)
        Muvfunc_MINI = np.zeros(len(redshifts) * nbins)
        Mhfunc_MINI = np.zeros(len(redshifts) * nbins)

        lfunc_MINI.shape = (len(redshifts), nbins)
        Muvfunc_MINI.shape = (len(redshifts), nbins)
        Mhfunc_MINI.shape = (len(redshifts), nbins)

        c_Muvfunc_MINI = ffi.cast("double *", ffi.from_buffer(Muvfunc_MINI))
        c_Mhfunc_MINI = ffi.cast("double *", ffi.from_buffer(Mhfunc_MINI))
        c_lfunc_MINI = ffi.cast("double *", ffi.from_buffer(lfunc_MINI))

        # Run the C code
        errcode = lib.ComputeLF(
            nbins,
            user_params(),
            cosmo_params(),
            astro_params(),
            flag_options(),
            2,
            len(redshifts),
            ffi.cast("float *", ffi.from_buffer(redshifts)),
            ffi.cast("float *", ffi.from_buffer(mturnovers_mini)),
            c_Muvfunc_MINI,
            c_Mhfunc_MINI,
            c_lfunc_MINI,
        )

        _process_exitcode(
            errcode,
            lib.ComputeLF,
            (
                nbins,
                user_params,
                cosmo_params,
                astro_params,
                flag_options,
                2,
                len(redshifts),
            ),
        )

        # redo the Muv range using the faintest (most likely MINI) and the brightest (most likely massive)
        lfunc_all = np.zeros(len(redshifts) * nbins)
        Muvfunc_all = np.zeros(len(redshifts) * nbins)
        Mhfunc_all = np.zeros(len(redshifts) * nbins * 2)

        lfunc_all.shape = (len(redshifts), nbins)
        Muvfunc_all.shape = (len(redshifts), nbins)
        Mhfunc_all.shape = (len(redshifts), nbins, 2)
        for iz in range(len(redshifts)):
            Muvfunc_all[iz] = np.linspace(
                np.min([Muvfunc.min(), Muvfunc_MINI.min()]),
                np.max([Muvfunc.max(), Muvfunc_MINI.max()]),
                nbins,
            )
            lfunc_all[iz] = np.log10(
                10
                ** (
                    interp1d(Muvfunc[iz], lfunc[iz], fill_value="extrapolate")(
                        Muvfunc_all[iz]
                    )
                )
                + 10
                ** (
                    interp1d(
                        Muvfunc_MINI[iz], lfunc_MINI[iz], fill_value="extrapolate"
                    )(Muvfunc_all[iz])
                )
            )
            Mhfunc_all[iz] = np.array(
                [
                    interp1d(Muvfunc[iz], Mhfunc[iz], fill_value="extrapolate")(
                        Muvfunc_all[iz]
                    ),
                    interp1d(
                        Muvfunc_MINI[iz], Mhfunc_MINI[iz], fill_value="extrapolate"
                    )(Muvfunc_all[iz]),
                ],
            ).T
        lfunc_all[lfunc_all <= -30] = np.nan
        return Muvfunc_all, Mhfunc_all, lfunc_all
    elif component == 1:
        lfunc = np.zeros(len(redshifts) * nbins)
        Muvfunc = np.zeros(len(redshifts) * nbins)
        Mhfunc = np.zeros(len(redshifts) * nbins)

        lfunc.shape = (len(redshifts), nbins)
        Muvfunc.shape = (len(redshifts), nbins)
        Mhfunc.shape = (len(redshifts), nbins)

        c_Muvfunc = ffi.cast("double *", ffi.from_buffer(Muvfunc))
        c_Mhfunc = ffi.cast("double *", ffi.from_buffer(Mhfunc))
        c_lfunc = ffi.cast("double *", ffi.from_buffer(lfunc))

        # Run the C code
        errcode = lib.ComputeLF(
            nbins,
            user_params(),
            cosmo_params(),
            astro_params(),
            flag_options(),
            1,
            len(redshifts),
            ffi.cast("float *", ffi.from_buffer(redshifts)),
            ffi.cast("float *", ffi.from_buffer(mturnovers)),
            c_Muvfunc,
            c_Mhfunc,
            c_lfunc,
        )

        _process_exitcode(
            errcode,
            lib.ComputeLF,
            (
                nbins,
                user_params,
                cosmo_params,
                astro_params,
                flag_options,
                1,
                len(redshifts),
            ),
        )

        lfunc[lfunc <= -30] = np.nan
        return Muvfunc, Mhfunc, lfunc
    elif component == 2:
        lfunc_MINI = np.zeros(len(redshifts) * nbins)
        Muvfunc_MINI = np.zeros(len(redshifts) * nbins)
        Mhfunc_MINI = np.zeros(len(redshifts) * nbins)

        lfunc_MINI.shape = (len(redshifts), nbins)
        Muvfunc_MINI.shape = (len(redshifts), nbins)
        Mhfunc_MINI.shape = (len(redshifts), nbins)

        c_Muvfunc_MINI = ffi.cast("double *", ffi.from_buffer(Muvfunc_MINI))
        c_Mhfunc_MINI = ffi.cast("double *", ffi.from_buffer(Mhfunc_MINI))
        c_lfunc_MINI = ffi.cast("double *", ffi.from_buffer(lfunc_MINI))

        # Run the C code
        errcode = lib.ComputeLF(
            nbins,
            user_params(),
            cosmo_params(),
            astro_params(),
            flag_options(),
            2,
            len(redshifts),
            ffi.cast("float *", ffi.from_buffer(redshifts)),
            ffi.cast("float *", ffi.from_buffer(mturnovers_mini)),
            c_Muvfunc_MINI,
            c_Mhfunc_MINI,
            c_lfunc_MINI,
        )

        _process_exitcode(
            errcode,
            lib.ComputeLF,
            (
                nbins,
                user_params,
                cosmo_params,
                astro_params,
                flag_options,
                2,
                len(redshifts),
            ),
        )

        lfunc_MINI[lfunc_MINI <= -30] = np.nan
        return Muvfunc_MINI, Mhfunc_MINI, lfunc_MINI
    else:
        logger.warning("What is component %d ?" % component)
        return None, None, None


def initial_conditions(
    *,
    user_params=None,
    cosmo_params=None,
    random_seed=None,
    regenerate=None,
    write=None,
    direc=None,
    hooks: dict[Callable, dict[str, Any]] | None = None,
    **global_kwargs,
) -> InitialConditions:
    r"""
    Compute initial conditions.

    Parameters
    ----------
    user_params : :class:`~UserParams` instance, optional
        Defines the overall options and parameters of the run.
    cosmo_params : :class:`~CosmoParams` instance, optional
        Defines the cosmological parameters used to compute initial conditions.
    regenerate : bool, optional
        Whether to force regeneration of data, even if matching cached data is found.
        This is applied recursively to any potential sub-calculations. It is ignored in
        the case of dependent data only if that data is explicitly passed to the function.
    write : bool, optional
        Whether to write results to file (i.e. cache). This is recursively applied to
        any potential sub-calculations.
    hooks
        Any extra functions to apply to the output object. This should be a dictionary
        where the keys are the functions, and the values are themselves dictionaries of
        parameters to pass to the function. The function signature should be
        ``(output, **params)``, where the ``output`` is the output object.
    direc : str, optional
        The directory in which to search for the boxes and write them. By default, this
        is the directory given by ``boxdir`` in the configuration file,
        ``~/.21cmfast/config.yml``. This is recursively applied to any potential
        sub-calculations.
    \*\*global_kwargs :
        Any attributes for :class:`~py21cmfast.inputs.GlobalParams`. This will
        *temporarily* set global attributes for the duration of the function. Note that
        arguments will be treated as case-insensitive.

    Returns
    -------
    :class:`~InitialConditions`
    """
    direc, regenerate, hooks = _get_config_options(direc, regenerate, write, hooks)

    with global_params.use(**global_kwargs):
        user_params, cosmo_params = _setup_inputs(
            {"user_params": user_params, "cosmo_params": cosmo_params}
        )

        # Initialize memory for the boxes that will be returned.
        boxes = InitialConditions(
            user_params=user_params, cosmo_params=cosmo_params, random_seed=random_seed
        )

        # Construct FFTW wisdoms. Only if required
        construct_fftw_wisdoms(user_params=user_params, cosmo_params=cosmo_params)

        # First check whether the boxes already exist.
        if not regenerate:
            try:
                boxes.read(direc)
                logger.info(
                    f"Existing init_boxes found and read in (seed={boxes.random_seed})."
                )
                return boxes
            except OSError:
                pass

        return boxes.compute(hooks=hooks)


def perturb_field(
    *,
    redshift,
    init_boxes=None,
    user_params=None,
    cosmo_params=None,
    random_seed=None,
    regenerate=None,
    write=None,
    direc=None,
    hooks: dict[Callable, dict[str, Any]] | None = None,
    **global_kwargs,
) -> PerturbedField:
    r"""
    Compute a perturbed field at a given redshift.

    Parameters
    ----------
    redshift : float
        The redshift at which to compute the perturbed field.
    init_boxes : :class:`~InitialConditions`, optional
        If given, these initial conditions boxes will be used, otherwise initial conditions will
        be generated. If given,
        the user and cosmo params will be set from this object.
    user_params : :class:`~UserParams`, optional
        Defines the overall options and parameters of the run.
    cosmo_params : :class:`~CosmoParams`, optional
        Defines the cosmological parameters used to compute initial conditions.
    \*\*global_kwargs :
        Any attributes for :class:`~py21cmfast.inputs.GlobalParams`. This will
        *temporarily* set global attributes for the duration of the function. Note that
        arguments will be treated as case-insensitive.

    Returns
    -------
    :class:`~PerturbedField`

    Other Parameters
    ----------------
    regenerate, write, direc, random_seed:
        See docs of :func:`initial_conditions` for more information.

    Examples
    --------
    The simplest method is just to give a redshift::

    >>> field = perturb_field(7.0)
    >>> print(field.density)

    Doing so will internally call the :func:`~initial_conditions` function. If initial conditions
    have already been
    calculated, this can be avoided by passing them:

    >>> init_boxes = initial_conditions()
    >>> field7 = perturb_field(7.0, init_boxes)
    >>> field8 = perturb_field(8.0, init_boxes)

    The user and cosmo parameter structures are by default inferred from the ``init_boxes``,
    so that the following is
    consistent::

    >>> init_boxes = initial_conditions(user_params= UserParams(HII_DIM=1000))
    >>> field7 = perturb_field(7.0, init_boxes)

    If ``init_boxes`` is not passed, then these parameters can be directly passed::

    >>> field7 = perturb_field(7.0, user_params=UserParams(HII_DIM=1000))

    """
    direc, regenerate, hooks = _get_config_options(direc, regenerate, write, hooks)

    with global_params.use(**global_kwargs):
        random_seed, user_params, cosmo_params, redshift = _setup_inputs(
            {
                "random_seed": random_seed,
                "user_params": user_params,
                "cosmo_params": cosmo_params,
            },
            input_boxes={"init_boxes": init_boxes},
            redshift=redshift,
        )

        # Initialize perturbed boxes.
        fields = PerturbedField(
            redshift=redshift,
            user_params=user_params,
            cosmo_params=cosmo_params,
            random_seed=random_seed,
        )

        # Check whether the boxes already exist
        if not regenerate:
            try:
                fields.read(direc)
                logger.info(
                    f"Existing z={redshift} perturb_field boxes found and read in "
                    f"(seed={fields.random_seed})."
                )
                return fields
            except OSError:
                pass

        # Construct FFTW wisdoms. Only if required
        construct_fftw_wisdoms(user_params=user_params, cosmo_params=cosmo_params)

        # Make sure we've got computed init boxes.
        if init_boxes is None or not init_boxes.is_computed:
            init_boxes = initial_conditions(
                user_params=user_params,
                cosmo_params=cosmo_params,
                regenerate=regenerate,
                hooks=hooks,
                direc=direc,
                random_seed=random_seed,
            )

            # Need to update fields to have the same seed as init_boxes
            fields._random_seed = init_boxes.random_seed

        # Run the C Code
        return fields.compute(ics=init_boxes, hooks=hooks)


def determine_halo_list(
    *,
    redshift,
    halos_desc=None,
    desc_z=None,
    init_boxes=None,
    user_params=None,
    cosmo_params=None,
    astro_params=None,
    flag_options=None,
    random_seed=None,
    regenerate=None,
    write=None,
    direc=None,
    hooks=None,
    **global_kwargs,
):
    r"""
    Find a halo list, given a redshift.

    Parameters
    ----------
    redshift : float
        The redshift at which to determine the halo list.
    init_boxes : :class:`~InitialConditions`, optional
        If given, these initial conditions boxes will be used, otherwise initial conditions will
        be generated. If given,
        the user and cosmo params will be set from this object.
    user_params : :class:`~UserParams`, optional
        Defines the overall options and parameters of the run.
    cosmo_params : :class:`~CosmoParams`, optional
        Defines the cosmological parameters used to compute initial conditions.
    astro_params: :class:`~AstroParams` instance, optional
        The astrophysical parameters defining the course of reionization.
    \*\*global_kwargs :
        Any attributes for :class:`~py21cmfast.inputs.GlobalParams`. This will
        *temporarily* set global attributes for the duration of the function. Note that
        arguments will be treated as case-insensitive.

    Returns
    -------
    :class:`~HaloField`

    Other Parameters
    ----------------
    regenerate, write, direc, random_seed:
        See docs of :func:`initial_conditions` for more information.

    Examples
    --------
    Fill this in once finalised

    """
    direc, regenerate, hooks = _get_config_options(direc, regenerate, write, hooks)

    with global_params.use(**global_kwargs):
        # Configure and check input/output parameters/structs
        (
            random_seed,
            user_params,
            cosmo_params,
            astro_params,
            flag_options,
            redshift,
        ) = _setup_inputs(
            {
                "random_seed": random_seed,
                "user_params": user_params,
                "cosmo_params": cosmo_params,
                "astro_params": astro_params,
                "flag_options": flag_options,
            },
            {"init_boxes": init_boxes, "prev_halo_field": halos_desc},
            redshift=redshift,
        )

        if user_params.HMF != 1:
            warnings.warn(
                "DexM Halofinder Uses a fit to the Sheth-Tormen mass function."
                "With HMF!=1 the Halos from DexM will not be from the same mass function",
            )

        min_z = (
            user_params.STOC_MINIMUM_Z if user_params.STOC_MINIMUM_Z is not None else -1
        )

        need_desc_box = False
        first_box = False
        # Three cases: descendant given, recursion to min_z, and first box
        # precedence is as follows: If the descendant field is given, use that no matter what.
        #   otherwise, if STOC_MINIMUM_Z == -1 *OR* we would jump over STOC_MINIMUM_Z, calculate this as the first box
        #   if neither of the above are true, step back ZPRIME_STEP_FACTOR and calculate that box
        if not isinstance(halos_desc, HaloField) or not halos_desc.is_computed:
            # If a descendant field is not provided, we step back toward the minimum z
            if desc_z is None:
                desc_z = (1 + redshift) / global_params.ZPRIME_STEP_FACTOR - 1

            # We are at the minimum redshift and no descendant z is given so this is the first box
            if desc_z < min_z or min_z == -1:
                first_box = True
                desc_z = -1  # This is -1 since None isn't compared to the files

            # we aren't at the minimum redshift and no previous field has been given
            else:
                # we don't calculate the descendant yet in case we have a current box on file
                need_desc_box = True
                # use ZPRIME_STEP_FACTOR if a descendant redshift not given

        # providing the previous halos ignores the minimum z and doesn't recurse
        else:
            desc_z = halos_desc.redshift

        # find the buffer size from expected halos in the box
        hbuffer_size = lib.expected_nhalo(
            redshift, user_params(), cosmo_params(), astro_params(), flag_options()
        )
        hbuffer_size = int((hbuffer_size + 1) * global_params.MAXHALO_FACTOR)
        # set a minimum in case of fluctuation at high z
        hbuffer_size = int(max(hbuffer_size, 1e6))

        # Initialize halo list boxes.
        fields = HaloField(
            desc_redshift=desc_z,
            buffer_size=hbuffer_size,
            redshift=redshift,
            user_params=user_params,
            cosmo_params=cosmo_params,
            astro_params=astro_params,
            flag_options=flag_options,
            random_seed=random_seed,
        )
        # Construct FFTW wisdoms. Only if required
        construct_fftw_wisdoms(user_params=user_params, cosmo_params=cosmo_params)

        # Make sure we've got computed init boxes.
        # TODO: we only need the init boxes if its the first sample
        if init_boxes is None or not init_boxes.is_computed:
            init_boxes = initial_conditions(
                user_params=user_params,
                cosmo_params=cosmo_params,
                regenerate=regenerate,
                hooks=hooks,
                direc=direc,
                random_seed=random_seed,
            )

            # Need to update fields to have the same seed as init_boxes
            fields._random_seed = init_boxes.random_seed

        # Check whether the boxes already exist
        logger.debug(f"z:{redshift:.2f} regen {regenerate}")
        if not regenerate:
            try:
                fields.read(direc)
                logger.info(
                    f"Existing z={redshift} determine_halo_list boxes found and read in "
                    f"(seed={fields.random_seed})."
                )
                return fields
            except OSError:
                pass

        if need_desc_box:
            halos_desc = determine_halo_list(
                redshift=desc_z,
                init_boxes=init_boxes,
                user_params=user_params,
                cosmo_params=cosmo_params,
                astro_params=astro_params,
                flag_options=flag_options,
                regenerate=regenerate,
            )
        elif first_box:
            halos_desc = HaloField(
                redshift=0,
                user_params=user_params,
                cosmo_params=cosmo_params,
                astro_params=astro_params,
                flag_options=flag_options,
                dummy=True,
            )

        # Run the C Code
        return fields.compute(
            ics=init_boxes, hooks=hooks, halos_desc=halos_desc, random_seed=random_seed
        )


def perturb_halo_list(
    *,
    redshift,
    init_boxes=None,
    halo_field=None,
    user_params=None,
    cosmo_params=None,
    astro_params=None,
    flag_options=None,
    random_seed=None,
    regenerate=None,
    write=None,
    direc=None,
    hooks=None,
    **global_kwargs,
):
    r"""
    Given a halo list, perturb the halos for a given redshift.

    Parameters
    ----------
    redshift : float
        The redshift at which to determine the halo list.
    init_boxes : :class:`~InitialConditions`, optional
        If given, these initial conditions boxes will be used, otherwise initial conditions will
        be generated. If given,
        the user and cosmo params will be set from this object.
    user_params : :class:`~UserParams`, optional
        Defines the overall options and parameters of the run.
    cosmo_params : :class:`~CosmoParams`, optional
        Defines the cosmological parameters used to compute initial conditions.
    astro_params: :class:`~AstroParams` instance, optional
        The astrophysical parameters defining the course of reionization.
    \*\*global_kwargs :
        Any attributes for :class:`~py21cmfast.inputs.GlobalParams`. This will
        *temporarily* set global attributes for the duration of the function. Note that
        arguments will be treated as case-insensitive.

    Returns
    -------
    :class:`~XraySourceBox`

    Other Parameters
    ----------------
    regenerate, write, direc, random_seed:
        See docs of :func:`initial_conditions` for more information.

    Examples
    --------
    Fill this in once finalised

    """
    direc, regenerate, hooks = _get_config_options(direc, regenerate, write, hooks)

    with global_params.use(**global_kwargs):
        # Configure and check input/output parameters/structs
        (
            random_seed,
            user_params,
            cosmo_params,
            astro_params,
            flag_options,
            redshift,
        ) = _setup_inputs(
            {
                "random_seed": random_seed,
                "user_params": user_params,
                "cosmo_params": cosmo_params,
                "astro_params": astro_params,
                "flag_options": flag_options,
            },
            {"init_boxes": init_boxes, "halo_field": halo_field},
            redshift=redshift,
        )

        if halo_field is None or not halo_field.is_computed:
            # find the buffer size from expected halos in the box
            hbuffer_size = lib.expected_nhalo(
                redshift, user_params(), cosmo_params(), astro_params(), flag_options()
            )
            hbuffer_size = int((hbuffer_size + 1) * global_params.MAXHALO_FACTOR)
            # set a minimum in case of fluctuation at high z
            hbuffer_size = int(max(hbuffer_size, 1e3))
        else:
            hbuffer_size = halo_field.n_halos

        # Initialize halo list boxes.
        fields = PerturbHaloField(
            redshift=redshift,
            buffer_size=hbuffer_size,
            user_params=user_params,
            cosmo_params=cosmo_params,
            astro_params=astro_params,
            flag_options=flag_options,
            random_seed=random_seed,
        )

        # Check whether the boxes already exist
        if not regenerate:
            try:
                fields.read(direc)
                logger.info(
                    "Existing z=%s perturb_halo_list boxes found and read in (seed=%s)."
                    % (redshift, fields.random_seed)
                )
                return fields
            except OSError:
                pass

        # Make sure we've got computed init boxes.
        if not isinstance(init_boxes, InitialConditions) or not init_boxes.is_computed:
            init_boxes = initial_conditions(
                user_params=user_params,
                cosmo_params=cosmo_params,
                regenerate=regenerate,
                hooks=hooks,
                direc=direc,
                random_seed=random_seed,
            )

            # Need to update fields to have the same seed as init_boxes
            fields._random_seed = init_boxes.random_seed

        # Dynamically produce the halo list.
        if halo_field is None or not halo_field.is_computed:
            halo_field = determine_halo_list(
                init_boxes=init_boxes,
                # NOTE: this is required, rather than using cosmo_ and user_,
                # since init may have a set seed.
                astro_params=astro_params,
                flag_options=flag_options,
                redshift=redshift,
                regenerate=regenerate,
                hooks=hooks,
                direc=direc,
            )

        # Run the C Code
        return fields.compute(ics=init_boxes, halo_field=halo_field, hooks=hooks)


def halo_box(
    *,
    redshift,
    astro_params=None,
    flag_options=None,
    cosmo_params=None,
    user_params=None,
    init_boxes=None,
    pt_halos=None,
    perturbed_field=None,
    previous_spin_temp=None,
    previous_ionize_box=None,
    write=None,
    direc=None,
    regenerate=None,
    random_seed=None,
    hooks=None,
    **global_kwargs,
) -> HaloBox:
    r"""
    Compute grids of halo properties from a catalogue.

    At the moment this simply produces halo masses, stellar masses and SFR on a grid of HII_DIM,
    in the future this will compute properties such as emissivities which will be passed directly into ionize_box etc
    instead of the catalogue.

    If no halo field is passed one is calculated at the desired redshift as if it is the first box

    Parameters
    ----------
    astro_params: :class:`~AstroParams` instance, optional
        The astrophysical parameters defining the course of reionization.
    flag_options: :class:`~FlagOptions` instance, optional
        Some options passed to the reionization routine.
    user_params : :class:`~UserParams`, optional
        Defines the overall options and parameters of the run.
    cosmo_params : :class:`~CosmoParams`, optional
        Defines the cosmological parameters used to compute initial conditions.
    pt_halos: :class:`~PerturbHaloField` or None, optional
        If passed, this contains all the dark matter haloes obtained if using the USE_HALO_FIELD.
        This is a list of halo masses and coords for the dark matter haloes.
    \*\*global_kwargs :
        Any attributes for :class:`~py21cmfast.inputs.GlobalParams`. This will
        *temporarily* set global attributes for the duration of the function. Note that
        arguments will be treated as case-insensitive.

    Returns
    -------
    :class:`~HaloBox` :
        An object containing the halo box data.

    Other Parameters
    ----------------
    regenerate, write, direc :
        See docs of :func:`initial_conditions` for more information.

    """
    direc, regenerate, hooks = _get_config_options(direc, regenerate, write, hooks)
    with global_params.use(**global_kwargs):
        # Configure and check input/output parameters/structs
        (
            random_seed,
            user_params,
            cosmo_params,
            astro_params,
            flag_options,
            redshift,
        ) = _setup_inputs(
            {
                "random_seed": random_seed,
                "user_params": user_params,
                "cosmo_params": cosmo_params,
                "astro_params": astro_params,
                "flag_options": flag_options,
            },
            {
                "init_boxes": init_boxes,
                "pt_halos": pt_halos,
                "perturbed_field": perturbed_field,
                "previous_spin_temp": previous_spin_temp,
                "previous_ionize_box": previous_ionize_box,
            },
            redshift=redshift,
        )

        # Initialize halo list boxes.
        box = HaloBox(
            redshift=redshift,
            random_seed=random_seed,
            user_params=user_params,
            cosmo_params=cosmo_params,
            astro_params=astro_params,
            flag_options=flag_options,
        )

        # Check whether the boxes already exist
        if not regenerate:
            try:
                box.read(direc)
                logger.info(
                    "Existing z=%s halo_box boxes found and read in (seed=%s)."
                    % (redshift, box.random_seed)
                )
                return box
            except OSError:
                pass

        # Make sure we've got computed init boxes.
        if init_boxes is None or not init_boxes.is_computed:
            init_boxes = initial_conditions(
                user_params=user_params,
                cosmo_params=cosmo_params,
                regenerate=regenerate,
                hooks=hooks,
                direc=direc,
                random_seed=random_seed,
            )
            box._random_seed = init_boxes.random_seed

        # Dynamically produce the halo list.
        # This uses default descendant behaviour, stepping from STOC_MINIMUM_Z to redshift
        if (
            not isinstance(perturbed_field, PerturbedField)
            or not perturbed_field.is_computed
        ):
            if flag_options.FIXED_HALO_GRIDS or global_params.AVG_BELOW_SAMPLER:
                perturbed_field = perturb_field(
                    user_params=user_params,
                    cosmo_params=cosmo_params,
                    init_boxes=init_boxes,
                    redshift=redshift,
                    regenerate=regenerate,
                    hooks=hooks,
                    direc=direc,
                )
            else:
                perturbed_field = PerturbedField(
                    redshift=0,
                    user_params=user_params,
                    cosmo_params=cosmo_params,
                    dummy=True,
                )
        if not isinstance(pt_halos, PerturbHaloField) or not pt_halos.is_computed:
            if flag_options.FIXED_HALO_GRIDS:
                pt_halos = perturb_halo_list(
                    redshift=redshift,
                    init_boxes=init_boxes,
                    cosmo_params=cosmo_params,
                    user_params=user_params,
                    astro_params=astro_params,
                    flag_options=flag_options,
                    regenerate=regenerate,
                    random_seed=random_seed,
                    hooks=hooks,
                    direc=direc,
                )
            else:
                pt_halos = PerturbHaloField(
                    redshift=0,
                    user_params=user_params,
                    cosmo_params=cosmo_params,
                    astro_params=astro_params,
                    flag_options=flag_options,
                    dummy=True,
                )

        # NOTE: due to the order, we use the previous spintemp here, like spin_temperature,
        #       but UNLIKE ionize_box, which uses the current box
        # TODO: think about the inconsistency here
        # NOTE: if USE_MINI_HALOS is TRUE, so is USE_TS_FLUCT and INHOMO_RECO
        if (
            not isinstance(previous_spin_temp, TsBox)
            or not previous_spin_temp.is_computed
        ):
            prev_z = (1 + redshift) * global_params.ZPRIME_STEP_FACTOR - 1
            if (not flag_options.USE_MINI_HALOS) or prev_z > global_params.Z_HEAT_MAX:
                previous_spin_temp = TsBox(
                    redshift=0,
                    user_params=user_params,
                    cosmo_params=cosmo_params,
                    astro_params=astro_params,
                    flag_options=flag_options,
                    dummy=True,
                )
            else:
                # Otherwise recursively create new previous box.
                previous_spin_temp = spin_temperature(
                    init_boxes=init_boxes,
                    astro_params=astro_params,
                    flag_options=flag_options,
                    redshift=prev_z,
                    regenerate=regenerate,
                    hooks=hooks,
                    direc=direc,
                    cleanup=False,  # we know we'll need the memory again
                )
        if (
            not isinstance(previous_ionize_box, IonizedBox)
            or not previous_ionize_box.is_computed
        ):
            prev_z = (1 + redshift) * global_params.ZPRIME_STEP_FACTOR - 1
            if (not flag_options.USE_MINI_HALOS) or prev_z > global_params.Z_HEAT_MAX:
                previous_ionize_box = IonizedBox(
                    redshift=0,
                    user_params=user_params,
                    cosmo_params=cosmo_params,
                    astro_params=astro_params,
                    flag_options=flag_options,
                    dummy=True,
                )
            else:
                previous_ionize_box = ionize_box(
                    init_boxes=init_boxes,
                    astro_params=astro_params,
                    flag_options=flag_options,
                    redshift=prev_z,
                    regenerate=regenerate,
                    hooks=hooks,
                    direc=direc,
                )

        result = box.compute(
            init_boxes=init_boxes,
            pt_halos=pt_halos,
            perturbed_field=perturbed_field,
            previous_ionize_box=previous_ionize_box,
            previous_spin_temp=previous_spin_temp,
            hooks=hooks,
        )

    return result


# TODO: make this more general and probably combine with the lightcone interp function
def interp_haloboxes(hbox_arr, fields, z_halos, z_target) -> HaloBox:
    """
    Interpolate HaloBox history to the desired redshift.

    Photon conservation & Xray sources require halo boxes at redshifts
    that are not equal to the current redshift, and may be between redshift steps.
    So we need a function to interpolate between two halo boxes.
    We assume here that z_arr is strictly INCERASING

    Parameters
    ----------
    hbox_arr: List of HaloBox instances
        The halobox history to be interpolated
    fields: List of Strings
        The properties of the haloboxes to be interpolated
    z_halos : array_like
        The redshifts of each HaloBox.
    pt_halos: :class:`~PerturbHaloField` or None, optional
        If passed, this contains all the dark matter haloes obtained if using the USE_HALO_FIELD.
        This is a list of halo masses and coords for the dark matter haloes.
    z_target : Float
        The desired redshift of interpolation

    Returns
    -------
    :class:`~HaloBox` :
        An object containing the halo box data.
    """
    if z_target > z_halos[-1] or z_target < z_halos[0]:
        raise ValueError(f"Invalid z_target {z_target} for redshift array {z_halos}")

    idx_prog = np.searchsorted(z_halos, z_target, side="left")

    if idx_prog == 0 or idx_prog == len(z_halos):
        logger.debug(f"z_target {z_target} beyond limits, {z_halos[0],z_halos[-1]}")
        raise ValueError

    z_prog = z_halos[idx_prog]
    idx_desc = idx_prog - 1

    z_desc = z_halos[idx_desc]
    interp_param = (z_target - z_desc) / (z_prog - z_desc)

    # I set the box redshift to be the stored one so it is read properly into the ionize box
    # for the xray source it doesn't matter, also since it is not _compute()'d, it won't be cached
    hbox_out = HaloBox(
        redshift=z_target,
        random_seed=hbox_arr[0].random_seed,
        user_params=hbox_arr[0].user_params,
        cosmo_params=hbox_arr[0].cosmo_params,
        astro_params=hbox_arr[0].astro_params,
        flag_options=hbox_arr[0].flag_options,
    )
    # initialise hbox memory
    hbox_out()

    # interpolate halo boxes in gridded SFR
    hbox_prog = hbox_arr[idx_prog]
    hbox_desc = hbox_arr[idx_desc]

    for field in fields:
        interp_field = (1 - interp_param) * getattr(
            hbox_desc, field
        ) + interp_param * getattr(hbox_prog, field)
        setattr(hbox_out, field, interp_field)

    logger.debug(
        f"interpolated to z={z_target} between [{z_desc},{z_prog}] ({interp_param})"
    )
    logger.debug(
        f"SFR averages desc {idx_desc}: {hbox_desc.halo_sfr.mean()} interp {hbox_out.halo_sfr.mean()} prog ({idx_prog}) {hbox_prog.halo_sfr.mean()}"
    )
    logger.debug(
        f"top-level struct min-max-mean {hbox_out.halo_sfr.min()}, {hbox_out.halo_sfr.max()}, {hbox_out.halo_sfr.mean()}"
    )
    # pass the arrays to the c struct by calling again
    hbox_out()
    # HACK: Since we don't compute, we have to mark the struct as computed
    for k, state in hbox_out._array_state.items():
        if state.initialized and k in fields:
            state.computed_in_mem = True
    # logger.info(f'C-level struct (first 3 halo mass cells) {asarray(getattr(hbox_out._cstruct, "halo_mass"), 3)}')

    return hbox_out, idx_desc, interp_param


# NOTE: the current implementation of this box is very hacky, since I have trouble figuring out a way to _compute()
#   over multiple redshifts in a nice way using this wrapper.
# TODO: if we move some code to jax or similar I think this would be one of the first candidates (just filling out some filtered grids)
def xray_source(
    *,
    redshift,
    astro_params=None,
    flag_options=None,
    cosmo_params=None,
    user_params=None,
    init_boxes=None,
    z_halos=None,
    hboxes=None,
    write=None,
    direc=None,
    regenerate=None,
    random_seed=None,
    hooks=None,
    **global_kwargs,
) -> XraySourceBox:
    r"""
    Compute filtered grid of SFR for use in spin temperature calculation.

    This will filter over the halo history in annuli, computing the contribution to the sfr density

    If no halo field is passed one is calculated at the desired redshift as if it is the first box

    Parameters
    ----------
    astro_params: :class:`~AstroParams` instance, optional
        The astrophysical parameters defining the course of reionization.
    flag_options: :class:`~FlagOptions` instance, optional
        Some options passed to the reionization routine.
    user_params : :class:`~UserParams`, optional
        Defines the overall options and parameters of the run.
    cosmo_params : :class:`~CosmoParams`, optional
        Defines the cosmological parameters used to compute initial conditions.
    hbox_file: list of str or None, optional
        If passed, this contains the list of files which contain saved
        :class:`~HaloBox` boxes which are used to create this source field
    \*\*global_kwargs :
        Any attributes for :class:`~py21cmfast.inputs.GlobalParams`. This will
        *temporarily* set global attributes for the duration of the function. Note that
        arguments will be treated as case-insensitive.

    Returns
    -------
    :class:`~XraySourceBox` :
        An object containing x ray heating, ionisation, and lyman alpha rates.

    Other Parameters
    ----------------
    regenerate, write, direc :
        See docs of :func:`initial_conditions` for more information.

    """
    direc, regenerate, hooks = _get_config_options(direc, regenerate, write, hooks)
    with global_params.use(**global_kwargs):
        # Configure and check input/output parameters/structs
        # NOTE: Since we pass a list of halo boxes at various redshifts,
        #       We only require that the final box is at the same redshift
        #       The init boxes are unused in the computation but are part of
        #       the convenient keywords and are used for verifying parameters
        (
            random_seed,
            user_params,
            cosmo_params,
            astro_params,
            flag_options,
            redshift,
        ) = _setup_inputs(
            {
                "random_seed": random_seed,
                "user_params": user_params,
                "cosmo_params": cosmo_params,
                "astro_params": astro_params,
                "flag_options": flag_options,
            },
            {"init_boxes": init_boxes, "halobox": hboxes[-1]},
            redshift=redshift,
        )

        # Initialize halo list boxes.
        box = XraySourceBox(
            redshift=redshift,
            random_seed=random_seed,
            user_params=user_params,
            cosmo_params=cosmo_params,
            astro_params=astro_params,
            flag_options=flag_options,
        )

        # Construct FFTW wisdoms. Only if required
        construct_fftw_wisdoms(user_params=user_params, cosmo_params=cosmo_params)

        # Check whether the boxes already exist
        if not regenerate:
            try:
                box.read(direc)
                logger.info(
                    "Existing z=%s xray_source boxes found and read in (seed=%s)."
                    % (redshift, box.random_seed)
                )
                return box
            except OSError:
                pass

        # set minimum R at cell size
        l_factor = (4 * np.pi / 3.0) ** (-1 / 3)
        R_min = user_params.BOX_LEN / user_params.HII_DIM * l_factor
        z_max = min(max(z_halos), global_params.Z_HEAT_MAX)

        # now we need to find the closest halo box to the redshift of the shell
        cosmo_ap = cosmo_params.cosmo
        cmd_zp = cosmo_ap.comoving_distance(redshift)
        R_steps = np.arange(0, global_params.NUM_FILTER_STEPS_FOR_Ts)
        R_factor = (global_params.R_XLy_MAX / R_min) ** (
            R_steps / global_params.NUM_FILTER_STEPS_FOR_Ts
        )
        R_range = un.Mpc * R_min * R_factor
        cmd_edges = cmd_zp + R_range  # comoving distance edges
        # NOTE(jdavies) added the 'bounded' method since it seems there are some compatibility issues with astropy and scipy
        # where astropy gives default bounds to a function with default unbounded minimization
        zpp_edges = [
            z_at_value(cosmo_ap.comoving_distance, d, method="bounded")
            for d in cmd_edges
        ]
        # the `average` redshift of the shell is the average of the
        # inner and outer redshifts (following the C code)
        zpp_avg = zpp_edges - np.diff(np.insert(zpp_edges, 0, redshift)) / 2

        # call the box the initialize the memory, since I give some values before computing
        box()
        final_box_computed = False
        for i in range(global_params.NUM_FILTER_STEPS_FOR_Ts):
            R_inner = R_range[i - 1].to("Mpc").value if i > 0 else 0
            R_outer = R_range[i].to("Mpc").value

            if zpp_avg[i] >= z_max:
                logger.debug(f"ignoring Radius {i} which is above Z_HEAT_MAX")
                box.filtered_sfr[i, ...] = 0
                continue

            hbox_interp, idx_desc, interp_param = interp_haloboxes(
                hboxes[::-1],
                ["halo_sfr", "halo_sfr_mini", "log10_Mcrit_LW_ave"],
                z_halos[::-1],
                zpp_avg[i],
            )

            # if we have no halos we ignore the whole shell
            if np.all(hbox_interp.halo_sfr + hbox_interp.halo_sfr_mini == 0):
                box.filtered_sfr[i, ...] = 0
                logger.debug(f"ignoring Radius {i} due to no stars")
                continue

            # HACK: so that I can compute in the loop multiple times
            # since the array state is initialized already it shouldn't re-initialise
            for k, state in box._array_state.items():
                if state.initialized:
                    state.computed_in_mem = False

            # we only want to call hooks at the end so we set a dummy hook here
            hooks_in = hooks if i == global_params.NUM_FILTER_STEPS_FOR_Ts - 1 else {}

            box = box.compute(
                halobox=hbox_interp,
                R_inner=R_inner,
                R_outer=R_outer,
                R_ct=i,
                hooks=hooks_in,
            )
            if i == global_params.NUM_FILTER_STEPS_FOR_Ts - 1:
                final_box_computed = True

        # HACK: sometimes we don't compute on the last step
        # (if the first zpp > z_max or there are no halos at max R)
        # in which case the array is not marked as computed
        if not final_box_computed:
            # we need to pass the memory to C, mark it as computed and call the hooks
            box()

            for k, state in box._array_state.items():
                if state.initialized:
                    state.computed_in_mem = True

            box._call_hooks(hooks)

        return box


def ionize_box(
    *,
    astro_params=None,
    flag_options=None,
    redshift=None,
    perturbed_field=None,
    previous_perturbed_field=None,
    previous_ionize_box=None,
    spin_temp=None,
    halobox=None,
    init_boxes=None,
    cosmo_params=None,
    user_params=None,
    regenerate=None,
    write=None,
    direc=None,
    random_seed=None,
    cleanup=True,
    hooks=None,
    **global_kwargs,
) -> IonizedBox:
    r"""
    Compute an ionized box at a given redshift.

    This function has various options for how the evolution of the ionization is computed (if at
    all). See the Notes below for details.

    Parameters
    ----------
    astro_params: :class:`~AstroParams` instance, optional
        The astrophysical parameters defining the course of reionization.
    flag_options: :class:`~FlagOptions` instance, optional
        Some options passed to the reionization routine.
    redshift : float, optional
        The redshift at which to compute the ionized box. If `perturbed_field` is given,
        its inherent redshift
        will take precedence over this argument. If not, this argument is mandatory.
    perturbed_field : :class:`~PerturbField`, optional
        If given, this field will be used, otherwise it will be generated. To be generated,
        either `init_boxes` and
        `redshift` must be given, or `user_params`, `cosmo_params` and `redshift`.
    previous_perturbed_field : :class:`~PerturbField`, optional
        An perturbed field at higher redshift. This is only used if mini_halo is included.
    init_boxes : :class:`~InitialConditions` , optional
        If given, and `perturbed_field` *not* given, these initial conditions boxes will be used
        to generate the perturbed field, otherwise initial conditions will be generated on the fly.
        If given, the user and cosmo params will be set from this object.
    previous_ionize_box: :class:`IonizedBox` or None
        An ionized box at higher redshift. This is only used if `INHOMO_RECO` and/or `do_spin_temp`
        are true. If either of these are true, and this is not given, then it will be assumed that
        this is the "first box", i.e. that it can be populated accurately without knowing source
        statistics.
    spin_temp: :class:`TsBox` or None, optional
        A spin-temperature box, only required if `do_spin_temp` is True. If None, will try to read
        in a spin temp box at the current redshift, and failing that will try to automatically
        create one, using the previous ionized box redshift as the previous spin temperature
        redshift.
    halobox: :class:`~HaloBox` or None, optional
        If passed, this contains all the dark matter haloes obtained if using the USE_HALO_FIELD.
        These are grids of halo masses and F_ESC-weighted Stellar Mass and Star Formation Rates
        If not passed, it will try and automatically create them using the available initial conditions.
    user_params : :class:`~UserParams`, optional
        Defines the overall options and parameters of the run.
    cosmo_params : :class:`~CosmoParams`, optional
        Defines the cosmological parameters used to compute initial conditions.
    cleanup : bool, optional
        A flag to specify whether the C routine cleans up its memory before returning. Typically,
        if `spin_temperature` is called directly, you will want this to be true, as if the next box
        to be calculate has different shape, errors will occur if memory is not cleaned. However,
        it can be useful to set it to False if scrolling through parameters for the same box shape.
    \*\*global_kwargs :
        Any attributes for :class:`~py21cmfast.inputs.GlobalParams`. This will
        *temporarily* set global attributes for the duration of the function. Note that
        arguments will be treated as case-insensitive.

    Returns
    -------
    :class:`~IonizedBox` :
        An object containing the ionized box data.

    Other Parameters
    ----------------
    regenerate, write, direc, random_seed :
        See docs of :func:`initial_conditions` for more information.

    Notes
    -----
    Typically, the ionization field at any redshift is dependent on the evolution of xHI up until
    that redshift, which necessitates providing a previous ionization field to define the current
    one. This function provides several options for doing so. First, if neither the spin
    temperature field, nor inhomogeneous recombinations (specified in flag options) are used, no
    evolution needs to be done. Otherwise, either (in order of precedence)

    1. a specific previous :class`~IonizedBox` object is provided, which will be used directly,
    2. a previous redshift is provided, for which a cached field on disk will be sought,
    3. a step factor is provided which recursively steps through redshift, calculating previous
       fields up until Z_HEAT_MAX, and returning just the final field at the current redshift, or
    4. the function is instructed to treat the current field as being an initial "high-redshift"
       field such that specific sources need not be found and evolved.

    .. note:: If a previous specific redshift is given, but no cached field is found at that
              redshift, the previous ionization field will be evaluated based on `z_step_factor`.

    Examples
    --------
    By default, no spin temperature is used, and neither are inhomogeneous recombinations,
    so that no evolution is required, thus the following will compute a coeval ionization box:

    >>> xHI = ionize_box(redshift=7.0)

    However, if either of those options are true, then a full evolution will be required:

    >>> xHI = ionize_box(redshift=7.0, flag_options=FlagOptions(INHOMO_RECO=True,USE_TS_FLUCT=True))

    This will by default evolve the field from a redshift of *at least* `Z_HEAT_MAX` (a global
    parameter), in logarithmic steps of `ZPRIME_STEP_FACTOR`. To change these:

    >>> xHI = ionize_box(redshift=7.0, zprime_step_factor=1.2, z_heat_max=15.0,
    >>>                  flag_options={"USE_TS_FLUCT":True})

    Alternatively, one can pass an exact previous redshift, which will be sought in the disk
    cache, or evaluated:

    >>> ts_box = ionize_box(redshift=7.0, previous_ionize_box=8.0, flag_options={
    >>>                     "USE_TS_FLUCT":True})

    Beware that doing this, if the previous box is not found on disk, will continue to evaluate
    prior boxes based on `ZPRIME_STEP_FACTOR`. Alternatively, one can pass a previous
    :class:`~IonizedBox`:

    >>> xHI_0 = ionize_box(redshift=8.0, flag_options={"USE_TS_FLUCT":True})
    >>> xHI = ionize_box(redshift=7.0, previous_ionize_box=xHI_0)

    Again, the first line here will implicitly use ``ZPRIME_STEP_FACTOR`` to evolve the field from
    ``Z_HEAT_MAX``. Note that in the second line, all of the input parameters are taken directly from
    `xHI_0` so that they are consistent, and we need not specify the ``flag_options``.

    As the function recursively evaluates previous redshift, the previous spin temperature fields
    will also be consistently recursively evaluated. Only the final ionized box will actually be
    returned and kept in memory, however intervening results will by default be cached on disk.
    One can also pass an explicit spin temperature object:

    >>> ts = spin_temperature(redshift=7.0)
    >>> xHI = ionize_box(redshift=7.0, spin_temp=ts)

    If automatic recursion is used, then it is done in such a way that no large boxes are kept
    around in memory for longer than they need to be (only two at a time are required).
    """
    direc, regenerate, hooks = _get_config_options(direc, regenerate, write, hooks)

    with global_params.use(**global_kwargs):
        # Configure and check input/output parameters/structs
        (
            random_seed,
            user_params,
            cosmo_params,
            astro_params,
            flag_options,
            redshift,
        ) = _setup_inputs(
            {
                "random_seed": random_seed,
                "user_params": user_params,
                "cosmo_params": cosmo_params,
                "astro_params": astro_params,
                "flag_options": flag_options,
            },
            {
                "init_boxes": init_boxes,
                "perturbed_field": perturbed_field,
                "previous_perturbed_field": previous_perturbed_field,
                "previous_ionize_box": previous_ionize_box,
                "spin_temp": spin_temp,
                "halobox": halobox,
            },
            redshift=redshift,
        )

        if spin_temp is not None and not flag_options.USE_TS_FLUCT:
            logger.warning(
                "Changing flag_options.USE_TS_FLUCT to True since spin_temp was passed."
            )
            flag_options.USE_TS_FLUCT = True

        # Get the previous redshift
        if previous_ionize_box is not None and previous_ionize_box.is_computed:
            prev_z = previous_ionize_box.redshift

            # Ensure the previous ionized box has a higher redshift than this one.
            if prev_z <= redshift:
                raise ValueError(
                    "Previous ionized box must have a higher redshift than that being evaluated."
                )
        elif flag_options.INHOMO_RECO or flag_options.USE_TS_FLUCT:
            prev_z = (1 + redshift) * global_params.ZPRIME_STEP_FACTOR - 1
            # if the previous box is before our starting point, we set it to zero,
            # which is what the C-code expects for an "initial" box
            if prev_z > global_params.Z_HEAT_MAX:
                prev_z = 0
        else:
            prev_z = 0

        box = IonizedBox(
            user_params=user_params,
            cosmo_params=cosmo_params,
            redshift=redshift,
            astro_params=astro_params,
            flag_options=flag_options,
            random_seed=random_seed,
            prev_ionize_redshift=prev_z,
        )

        # Construct FFTW wisdoms. Only if required
        construct_fftw_wisdoms(user_params=user_params, cosmo_params=cosmo_params)

        # Check whether the boxes already exist
        if not regenerate:
            try:
                box.read(direc)
                logger.info(
                    "Existing z=%s ionized boxes found and read in (seed=%s)."
                    % (redshift, box.random_seed)
                )
                return box
            except OSError:
                pass

        # EVERYTHING PAST THIS POINT ONLY HAPPENS IF THE BOX DOESN'T ALREADY EXIST
        # ------------------------------------------------------------------------

        # Get init_box required.
        if init_boxes is None or not init_boxes.is_computed:
            init_boxes = initial_conditions(
                user_params=user_params,
                cosmo_params=cosmo_params,
                regenerate=regenerate,
                hooks=hooks,
                direc=direc,
                random_seed=random_seed,
            )

            # Need to update random seed
            box._random_seed = init_boxes.random_seed

        # Get appropriate previous ionization box
        if previous_ionize_box is None or not previous_ionize_box.is_computed:
            # If we are beyond Z_HEAT_MAX, just make an empty box
            if prev_z == 0:
                previous_ionize_box = IonizedBox(
                    redshift=0, flag_options=flag_options, initial=True
                )

            # Otherwise recursively create new previous box.
            else:
                previous_ionize_box = ionize_box(
                    astro_params=astro_params,
                    flag_options=flag_options,
                    redshift=prev_z,
                    init_boxes=init_boxes,
                    regenerate=regenerate,
                    hooks=hooks,
                    direc=direc,
                    cleanup=False,  # We *know* we're going to need the memory again.
                )

        # Dynamically produce the perturbed field.
        if perturbed_field is None or not perturbed_field.is_computed:
            perturbed_field = perturb_field(
                init_boxes=init_boxes,
                # NOTE: this is required, rather than using cosmo_ and user_,
                # since init may have a set seed.
                redshift=redshift,
                regenerate=regenerate,
                hooks=hooks,
                direc=direc,
            )

        if previous_perturbed_field is None or not previous_perturbed_field.is_computed:
            # If we are beyond Z_HEAT_MAX, just make an empty box
            if not prev_z:
                previous_perturbed_field = PerturbedField(
                    redshift=0, user_params=user_params, initial=True
                )
            else:
                previous_perturbed_field = perturb_field(
                    init_boxes=init_boxes,
                    redshift=prev_z,
                    regenerate=regenerate,
                    hooks=hooks,
                    direc=direc,
                )

        # Dynamically produce the halo field.
        # NOTE: IF PHOTON CONSERVATION IS ON WE ASSUME YOU PASSED THE RIGHT HALOBOX
        if not flag_options.USE_HALO_FIELD:
            # Construct an empty halo field to pass in to the function.
            halobox = HaloBox(
                redshift=0,
                user_params=user_params,
                cosmo_params=cosmo_params,
                astro_params=astro_params,
                flag_options=flag_options,
                dummy=True,
            )
        elif halobox is None or not halobox.is_computed:
            # CURRENT ISSUES (ALSO FOR spin_temperature):
            # sometimes redshifts don't exactly match and we regenerate too much
            # we ONLY want to regenerate the halo field on the highest redshift
            regen_halos = regenerate and prev_z == 0
            logger.info(
                f"z: {redshift} | regen {regenerate} | regen_halos {regen_halos}"
            )
            raise NotImplementedError(
                "Automatic generation of halo boxes not yet implemented, \
                                            Use run_coeval, run_lightcone or explicitly generate the box"
            )

        # Set empty spin temp box if necessary.
        if not flag_options.USE_TS_FLUCT:
            spin_temp = TsBox(
                redshift=0,
                user_params=user_params,
                cosmo_params=cosmo_params,
                astro_params=astro_params,
                flag_options=flag_options,
                dummy=True,
            )
        elif spin_temp is None:
            spin_temp = spin_temperature(
                perturbed_field=perturbed_field,
                flag_options=flag_options,
                init_boxes=init_boxes,
                direc=direc,
                hooks=hooks,
                regenerate=regenerate,
                cleanup=cleanup,
            )

        # Run the C Code
        return box.compute(
            perturbed_field=perturbed_field,
            prev_perturbed_field=previous_perturbed_field,
            prev_ionize_box=previous_ionize_box,
            spin_temp=spin_temp,
            halobox=halobox,
            ics=init_boxes,
            hooks=hooks,
        )


def spin_temperature(
    *,
    astro_params=None,
    flag_options=None,
    redshift=None,
    perturbed_field=None,
    xray_source_box=None,
    previous_spin_temp=None,
    init_boxes=None,
    cosmo_params=None,
    user_params=None,
    regenerate=None,
    write=None,
    direc=None,
    random_seed=None,
    cleanup=True,
    hooks=None,
    **global_kwargs,
) -> TsBox:
    r"""
    Compute spin temperature boxes at a given redshift.

    See the notes below for how the spin temperature field is evolved through redshift.

    Parameters
    ----------
    astro_params : :class:`~AstroParams`, optional
        The astrophysical parameters defining the course of reionization.
    flag_options : :class:`~FlagOptions`, optional
        Some options passed to the reionization routine.
    redshift : float, optional
        The redshift at which to compute the ionized box. If not given, the redshift from
        `perturbed_field` will be used. Either `redshift`, `perturbed_field`, or
        `previous_spin_temp` must be given. See notes on `perturbed_field` for how it affects the
        given redshift if both are given.
    perturbed_field : :class:`~PerturbField`, optional
        If given, this field will be used, otherwise it will be generated. To be generated,
        either `init_boxes` and `redshift` must be given, or `user_params`, `cosmo_params` and
        `redshift`. By default, this will be generated at the same redshift as the spin temperature
        box. The redshift of perturb field is allowed to be different than `redshift`. If so, it
        will be interpolated to the correct redshift, which can provide a speedup compared to
        actually computing it at the desired redshift.
    previous_spin_temp : :class:`TsBox` or None
        The previous spin temperature box.
    init_boxes : :class:`~InitialConditions`, optional
        If given, and `perturbed_field` *not* given, these initial conditions boxes will be used
        to generate the perturbed field, otherwise initial conditions will be generated on the fly.
        If given, the user and cosmo params will be set from this object.
    user_params : :class:`~UserParams`, optional
        Defines the overall options and parameters of the run.
    cosmo_params : :class:`~CosmoParams`, optional
        Defines the cosmological parameters used to compute initial conditions.
    cleanup : bool, optional
        A flag to specify whether the C routine cleans up its memory before returning.
        Typically, if `spin_temperature` is called directly, you will want this to be
        true, as if the next box to be calculate has different shape, errors will occur
        if memory is not cleaned. However, it can be useful to set it to False if
        scrolling through parameters for the same box shape.
    \*\*global_kwargs :
        Any attributes for :class:`~py21cmfast.inputs.GlobalParams`. This will
        *temporarily* set global attributes for the duration of the function. Note that
        arguments will be treated as case-insensitive.

    Returns
    -------
    :class:`~TsBox`
        An object containing the spin temperature box data.

    Other Parameters
    ----------------
    regenerate, write, direc, random_seed :
        See docs of :func:`initial_conditions` for more information.

    Notes
    -----
    Typically, the spin temperature field at any redshift is dependent on the evolution of spin
    temperature up until that redshift, which necessitates providing a previous spin temperature
    field to define the current one. This function provides several options for doing so. Either
    (in order of precedence):

    1. a specific previous spin temperature object is provided, which will be used directly,
    2. a previous redshift is provided, for which a cached field on disk will be sought,
    3. a step factor is provided which recursively steps through redshift, calculating previous
       fields up until Z_HEAT_MAX, and returning just the final field at the current redshift, or
    4. the function is instructed to treat the current field as being an initial "high-redshift"
       field such that specific sources need not be found and evolved.

    .. note:: If a previous specific redshift is given, but no cached field is found at that
              redshift, the previous spin temperature field will be evaluated based on
              ``z_step_factor``.

    Examples
    --------
    To calculate and return a fully evolved spin temperature field at a given redshift (with
    default input parameters), simply use:

    >>> ts_box = spin_temperature(redshift=7.0)

    This will by default evolve the field from a redshift of *at least* `Z_HEAT_MAX` (a global
    parameter), in logarithmic steps of `z_step_factor`. Thus to change these:

    >>> ts_box = spin_temperature(redshift=7.0, zprime_step_factor=1.2, z_heat_max=15.0)

    Alternatively, one can pass an exact previous redshift, which will be sought in the disk
    cache, or evaluated:

    >>> ts_box = spin_temperature(redshift=7.0, previous_spin_temp=8.0)

    Beware that doing this, if the previous box is not found on disk, will continue to evaluate
    prior boxes based on the ``z_step_factor``. Alternatively, one can pass a previous spin
    temperature box:

    >>> ts_box1 = spin_temperature(redshift=8.0)
    >>> ts_box = spin_temperature(redshift=7.0, previous_spin_temp=ts_box1)

    Again, the first line here will implicitly use ``z_step_factor`` to evolve the field from
    around ``Z_HEAT_MAX``. Note that in the second line, all of the input parameters are taken
    directly from `ts_box1` so that they are consistent. Finally, one can force the function to
    evaluate the current redshift as if it was beyond ``Z_HEAT_MAX`` so that it depends only on
    itself:

    >>> ts_box = spin_temperature(redshift=7.0, zprime_step_factor=None)

    This is usually a bad idea, and will give a warning, but it is possible.
    """
    direc, regenerate, hooks = _get_config_options(direc, regenerate, write, hooks)

    with global_params.use(**global_kwargs):
        # Configure and check input/output parameters/structs
        (
            random_seed,
            user_params,
            cosmo_params,
            astro_params,
            flag_options,
        ) = _setup_inputs(
            {
                "random_seed": random_seed,
                "user_params": user_params,
                "cosmo_params": cosmo_params,
                "astro_params": astro_params,
                "flag_options": flag_options,
            },
            {
                "init_boxes": init_boxes,
                "perturbed_field": perturbed_field,
                "previous_spin_temp": previous_spin_temp,
                "xray_source_box": xray_source_box,
            },
        )

        # Try to determine redshift from other inputs, if required.
        # Note that perturb_field does not need to match redshift here.
        if redshift is None:
            if perturbed_field is not None:
                redshift = perturbed_field.redshift
            elif previous_spin_temp is not None:
                redshift = (
                    previous_spin_temp.redshift + 1
                ) / global_params.ZPRIME_STEP_FACTOR - 1
            else:
                raise ValueError(
                    "Either the redshift, perturbed_field or previous_spin_temp must be given."
                )

        # Explicitly set this flag to True, though it shouldn't be required!
        flag_options.update(USE_TS_FLUCT=True)

        # Get the previous redshift
        if previous_spin_temp is not None:
            prev_z = previous_spin_temp.redshift
        else:
            if redshift < global_params.Z_HEAT_MAX:
                # In general runs, we only compute the spin temperature *below* Z_HEAT_MAX.
                # Above this, we don't need a prev_z at all, because we can calculate
                # directly at whatever redshift it is.
                prev_z = min(
                    global_params.Z_HEAT_MAX,
                    (1 + redshift) * global_params.ZPRIME_STEP_FACTOR - 1,
                )
            else:
                # Set prev_z to anything, since we don't need it.
                prev_z = np.inf

        # Ensure the previous spin temperature has a higher redshift than this one.
        if prev_z <= redshift:
            raise ValueError(
                "Previous spin temperature box must have a higher redshift than "
                "that being evaluated."
            )

        # Set up the box without computing anything.
        box = TsBox(
            user_params=user_params,
            cosmo_params=cosmo_params,
            redshift=redshift,
            astro_params=astro_params,
            flag_options=flag_options,
            random_seed=random_seed,
            prev_spin_redshift=prev_z,
            perturbed_field_redshift=(
                perturbed_field.redshift
                if (perturbed_field is not None and perturbed_field.is_computed)
                else redshift
            ),
        )

        # Construct FFTW wisdoms. Only if required
        construct_fftw_wisdoms(user_params=user_params, cosmo_params=cosmo_params)

        # Check whether the boxes already exist on disk.
        if not regenerate:
            try:
                box.read(direc)
                logger.info(
                    f"Existing z={redshift} spin_temp boxes found and read in "
                    f"(seed={box.random_seed})."
                )
                return box
            except OSError:
                pass

        # EVERYTHING PAST THIS POINT ONLY HAPPENS IF THE BOX DOESN'T ALREADY EXIST
        # ------------------------------------------------------------------------
        # Dynamically produce the initial conditions.
        if init_boxes is None or not init_boxes.is_computed:
            init_boxes = initial_conditions(
                user_params=user_params,
                cosmo_params=cosmo_params,
                regenerate=regenerate,
                hooks=hooks,
                direc=direc,
                random_seed=random_seed,
            )

            # Need to update random seed
            box._random_seed = init_boxes.random_seed

        # Create appropriate previous_spin_temp
        if previous_spin_temp is None:
            if redshift >= global_params.Z_HEAT_MAX:
                # We end up never even using this box, just need to define it
                # unallocated to be able to send into the C code.
                previous_spin_temp = TsBox(
                    redshift=prev_z,  # redshift here is ignored
                    user_params=init_boxes.user_params,
                    cosmo_params=init_boxes.cosmo_params,
                    astro_params=astro_params,
                    flag_options=flag_options,
                    dummy=True,
                )
            else:
                previous_spin_temp = spin_temperature(
                    init_boxes=init_boxes,
                    astro_params=astro_params,
                    flag_options=flag_options,
                    redshift=prev_z,
                    regenerate=regenerate,
                    hooks=hooks,
                    direc=direc,
                    cleanup=False,  # we know we'll need the memory again
                )

        # Generate halos if needed
        if flag_options.USE_HALO_FIELD:
            # TODO: this doesn't work with the recursion at all, move to a list of needed snapshots like the lightcone
            if xray_source_box is None or not xray_source_box.is_computed:
                raise NotImplementedError(
                    "Automatic generation of Xray source box from halos not yet implemented, \
                                             Use run_coeval, run_lightcone or explicitly generate the source box"
                )
            # The entire halo history is generated via similar (but backwards) recursion to the spintemp
        else:
            xray_source_box = XraySourceBox(
                redshift=0,
                user_params=user_params,
                cosmo_params=cosmo_params,
                astro_params=astro_params,
                flag_options=flag_options,
                dummy=True,
            )

        # Dynamically produce the perturbed field.
        if perturbed_field is None or not perturbed_field.is_computed:
            perturbed_field = perturb_field(
                redshift=redshift,
                init_boxes=init_boxes,
                regenerate=regenerate,
                hooks=hooks,
                direc=direc,
            )

        # Run the C Code
        return box.compute(
            cleanup=cleanup,
            perturbed_field=perturbed_field,
            xray_source_box=xray_source_box,
            prev_spin_temp=previous_spin_temp,
            ics=init_boxes,
            hooks=hooks,
        )


def brightness_temperature(
    *,
    ionized_box,
    perturbed_field,
    spin_temp=None,
    write=None,
    regenerate=None,
    direc=None,
    hooks=None,
    **global_kwargs,
) -> BrightnessTemp:
    r"""
    Compute a coeval brightness temperature box.

    Parameters
    ----------
    ionized_box: :class:`IonizedBox`
        A pre-computed ionized box.
    perturbed_field: :class:`PerturbedField`
        A pre-computed perturbed field at the same redshift as `ionized_box`.
    spin_temp: :class:`TsBox`, optional
        A pre-computed spin temperature, at the same redshift as the other boxes.
    \*\*global_kwargs :
        Any attributes for :class:`~py21cmfast.inputs.GlobalParams`. This will
        *temporarily* set global attributes for the duration of the function. Note that
        arguments will be treated as case-insensitive.

    Returns
    -------
    :class:`BrightnessTemp` instance.
    """
    direc, regenerate, hooks = _get_config_options(direc, regenerate, write, hooks)

    with global_params.use(**global_kwargs):
        _verify_types(
            perturbed_field=perturbed_field,
            spin_temp=spin_temp,
            ionized_box=ionized_box,
        )

        # don't ignore redshift here
        _check_compatible_inputs(ionized_box, perturbed_field, spin_temp, ignore=[])

        # ensure ionized_box and perturbed_field aren't None, as we don't do
        # any dynamic calculations here.
        if ionized_box is None or perturbed_field is None:
            raise ValueError("both ionized_box and perturbed_field must be specified.")

        if spin_temp is None:
            if ionized_box.flag_options.USE_TS_FLUCT:
                raise ValueError(
                    "You have USE_TS_FLUCT=True, but have not provided a spin_temp!"
                )

            # Make an unused dummy box.
            spin_temp = TsBox(redshift=0, dummy=True)

        box = BrightnessTemp(
            user_params=ionized_box.user_params,
            cosmo_params=ionized_box.cosmo_params,
            astro_params=ionized_box.astro_params,
            flag_options=ionized_box.flag_options,
            redshift=ionized_box.redshift,
            random_seed=ionized_box.random_seed,
        )

        # Construct FFTW wisdoms. Only if required
        construct_fftw_wisdoms(
            user_params=ionized_box.user_params, cosmo_params=ionized_box.cosmo_params
        )

        # Check whether the boxes already exist on disk.
        if not regenerate:
            try:
                box.read(direc)
                logger.info(
                    f"Existing brightness_temp box found and read in (seed={box.random_seed})."
                )
                return box
            except OSError:
                pass

        return box.compute(
            spin_temp=spin_temp,
            ionized_box=ionized_box,
            perturbed_field=perturbed_field,
            hooks=hooks,
        )


def _logscroll_redshifts(min_redshift, z_step_factor, zmax):
    redshifts = [min_redshift]
    while redshifts[-1] < zmax:
        redshifts.append((redshifts[-1] + 1.0) * z_step_factor - 1.0)

    return redshifts[::-1]


def run_coeval(
    *,
    redshift=None,
    user_params=None,
    cosmo_params=None,
    astro_params=None,
    flag_options=None,
    regenerate=None,
    write=None,
    direc=None,
    init_box=None,
    perturb=None,
    use_interp_perturb_field=False,
    random_seed=None,
    cleanup=True,
    hooks=None,
    always_purge: bool = False,
    **global_kwargs,
):
    r"""
    Evaluate a coeval ionized box at a given redshift, or multiple redshifts.

    This is generally the easiest and most efficient way to generate a set of coeval cubes at a
    given set of redshift. It self-consistently deals with situations in which the field needs to be
    evolved, and does this with the highest memory-efficiency, only returning the desired redshift.
    All other calculations are by default stored in the on-disk cache so they can be re-used at a
    later time.

    .. note:: User-supplied redshift are *not* used as previous redshift in any scrolling,
              so that pristine log-sampling can be maintained.

    Parameters
    ----------
    redshift: array_like
        A single redshift, or multiple redshift, at which to return results. The minimum of these
        will define the log-scrolling behaviour (if necessary).
    user_params : :class:`~inputs.UserParams`, optional
        Defines the overall options and parameters of the run.
    cosmo_params : :class:`~inputs.CosmoParams` , optional
        Defines the cosmological parameters used to compute initial conditions.
    astro_params : :class:`~inputs.AstroParams` , optional
        The astrophysical parameters defining the course of reionization.
    flag_options : :class:`~inputs.FlagOptions` , optional
        Some options passed to the reionization routine.
    init_box : :class:`~InitialConditions`, optional
        If given, the user and cosmo params will be set from this object, and it will not
        be re-calculated.
    perturb : list of :class:`~PerturbedField`, optional
        If given, must be compatible with init_box. It will merely negate the necessity
        of re-calculating the perturb fields.
    use_interp_perturb_field : bool, optional
        Whether to use a single perturb field, at the lowest redshift of the lightcone,
        to determine all spin temperature fields. If so, this field is interpolated in
        the underlying C-code to the correct redshift. This is less accurate (and no more
        efficient), but provides compatibility with older versions of 21cmFAST.
    cleanup : bool, optional
        A flag to specify whether the C routine cleans up its memory before returning.
        Typically, if `spin_temperature` is called directly, you will want this to be
        true, as if the next box to be calculate has different shape, errors will occur
        if memory is not cleaned. Note that internally, this is set to False until the
        last iteration.
    \*\*global_kwargs :
        Any attributes for :class:`~py21cmfast.inputs.GlobalParams`. This will
        *temporarily* set global attributes for the duration of the function. Note that
        arguments will be treated as case-insensitive.

    Returns
    -------
    coevals : :class:`~py21cmfast.outputs.Coeval`
        The full data for the Coeval class, with init boxes, perturbed fields, ionized boxes,
        brightness temperature, and potential data from the conservation of photons. If a
        single redshift was specified, it will return such a class. If multiple redshifts
        were passed, it will return a list of such classes.

    Other Parameters
    ----------------
    regenerate, write, direc, random_seed :
        See docs of :func:`initial_conditions` for more information.
    """
    with global_params.use(**global_kwargs):
        if redshift is None and perturb is None:
            raise ValueError("Either redshift or perturb must be given")

        direc, regenerate, hooks = _get_config_options(direc, regenerate, write, hooks)

        singleton = False
        # Ensure perturb is a list of boxes, not just one.
        if perturb is None:
            perturb = []
        elif not hasattr(perturb, "__len__"):
            perturb = [perturb]
            singleton = True

        (
            random_seed,
            user_params,
            cosmo_params,
            astro_params,
            flag_options,
        ) = _setup_inputs(
            {
                "random_seed": random_seed,
                "user_params": user_params,
                "cosmo_params": cosmo_params,
                "astro_params": astro_params,
                "flag_options": flag_options,
            },
        )

        if use_interp_perturb_field and flag_options.USE_MINI_HALOS:
            raise ValueError("Cannot use an interpolated perturb field with minihalos!")

        iokw = {"regenerate": regenerate, "hooks": hooks, "direc": direc}

        if init_box is None:
            init_box = initial_conditions(
                user_params=user_params,
                cosmo_params=cosmo_params,
                random_seed=random_seed,
                **iokw,
            )

        # We can go ahead and purge some of the stuff in the init_box, but only if
        # it is cached -- otherwise we could be losing information.
        try:
            init_box.prepare_for_perturb(flag_options=flag_options, force=always_purge)
        except OSError:
            pass

        if perturb:
            if redshift is not None and any(
                p.redshift != z for p, z in zip(perturb, redshift)
            ):
                raise ValueError("Input redshifts do not match perturb field redshifts")
            else:
                redshift = [p.redshift for p in perturb]

        kw = {
            **{
                "astro_params": astro_params,
                "flag_options": flag_options,
                "init_boxes": init_box,
            },
            **iokw,
        }
        photon_nonconservation_data = None
        if flag_options.PHOTON_CONS_TYPE != 0:
            photon_nonconservation_data = setup_photon_cons(**kw)

        if not hasattr(redshift, "__len__"):
            singleton = True
            redshift = [redshift]

        if isinstance(redshift, np.ndarray):
            redshift = redshift.tolist()

        # Get the list of redshift we need to scroll through.
        redshifts = _get_required_redshifts_coeval(flag_options, redshift)

        # Get all the perturb boxes early. We need to get the perturb at every
        # redshift, even if we are interpolating the perturb field, because the
        # ionize box needs it.

        pz = [p.redshift for p in perturb]
        perturb_ = []
        for z in redshifts:
            p = (
                perturb_field(redshift=z, init_boxes=init_box, **iokw)
                if z not in pz
                else perturb[pz.index(z)]
            )

            if user_params.MINIMIZE_MEMORY:
                try:
                    p.purge(force=always_purge)
                except OSError:
                    pass

            perturb_.append(p)

        perturb = perturb_

        # Now we can purge init_box further.
        try:
            init_box.prepare_for_halos(flag_options=flag_options, force=always_purge)
        except OSError:
            pass

        # get the halos (reverse redshift order)
        if flag_options.USE_HALO_FIELD:
            halos_desc = None

            pt_halos = []
            for i, z in enumerate(redshifts[::-1]):
                if not flag_options.FIXED_HALO_GRIDS:
                    halos = determine_halo_list(redshift=z, halos_desc=halos_desc, **kw)
                    pt_halos += [perturb_halo_list(redshift=z, halo_field=halos, **kw)]

                    # we never want to store every halofield
                    try:
                        pt_halos[i].purge(force=always_purge)
                    except OSError:
                        pass
                    halos_desc = halos

            # reverse to get the right redshift order
            pt_halos = pt_halos[::-1]

        # Now we can purge init_box further.
        try:
            init_box.prepare_for_spin_temp(
                flag_options=flag_options, force=always_purge
            )
        except OSError:
            pass

        if (
            flag_options.PHOTON_CONS_TYPE == 1
            and np.amin(redshifts) < global_params.PhotonConsEndCalibz
        ):
            raise ValueError(
                f"You have passed a redshift (z = {np.amin(redshifts)}) that is lower than"
                "the endpoint of the photon non-conservation correction"
                f"(global_params.PhotonConsEndCalibz = {global_params.PhotonConsEndCalibz})."
                "If this behaviour is desired then set global_params.PhotonConsEndCalibz"
                f"to a value lower than z = {np.amin(redshifts)}."
            )

        ib_tracker = [0] * len(redshift)
        bt = [0] * len(redshift)
        # At first we don't have any "previous" st or ib.
        st, ib, pf = None, None, None
        # optional fields which remain None if their flags are off
        hb2, ph2 = None, None

        perturb_min = perturb[np.argmin(redshift)]

        hb_tracker = [None] * len(redshift)
        st_tracker = [None] * len(redshift)

        spin_temp_files = []
        hbox_files = []
        perturb_files = []
        ionize_files = []
        brightness_files = []
        pth_files = []

        # Iterate through redshift from top to bottom
        z_halos = []
        hbox_arr = []
        for iz, z in enumerate(redshifts):
            pf2 = perturb[iz]
            pf2.load_all()

            if flag_options.USE_HALO_FIELD:
                ph2 = pt_halos[iz]
                hb2 = halo_box(
                    redshift=z,
                    pt_halos=ph2,
                    perturbed_field=pf2,
                    previous_ionize_box=ib,
                    previous_spin_temp=st,
                    **kw,
                )
                # append the halo redshift array so we have all halo boxes [z,zmax]
                z_halos += [z]
                hbox_arr += [hb2]

            if flag_options.USE_TS_FLUCT:
                if flag_options.USE_HALO_FIELD:
                    xray_source_box = xray_source(
                        redshift=z,
                        z_halos=z_halos,
                        hboxes=hbox_arr,
                        **kw,
                    )

                st2 = spin_temperature(
                    redshift=z,
                    previous_spin_temp=st,
                    perturbed_field=perturb_min if use_interp_perturb_field else pf2,
                    xray_source_box=(
                        xray_source_box if flag_options.USE_HALO_FIELD else None
                    ),
                    **kw,
                    cleanup=(cleanup and z == redshifts[-1]),
                )

                if z not in redshift:
                    st = st2

            ib2 = ionize_box(
                redshift=z,
                previous_ionize_box=ib,
                perturbed_field=pf2,
                # perturb field *not* interpolated here.
                previous_perturbed_field=pf,
                halobox=hb2,
                spin_temp=st2 if flag_options.USE_TS_FLUCT else None,
                z_heat_max=global_params.Z_HEAT_MAX,
                # cleanup if its the last time through
                cleanup=cleanup and z == redshifts[-1],
                **kw,
            )

            if pf is not None:
                try:
                    pf.purge(force=always_purge)
                except OSError:
                    pass

            if ph2 is not None:
                try:
                    ph2.purge(force=always_purge)
                except OSError:
                    pass

            if z in redshift:
                logger.debug(f"PID={os.getpid()} doing brightness temp for z={z}")
                ib_tracker[redshift.index(z)] = ib2
                st_tracker[redshift.index(z)] = (
                    st2 if flag_options.USE_TS_FLUCT else None
                )

                hb_tracker[redshift.index(z)] = (
                    hb2 if flag_options.USE_HALO_FIELD else None
                )

                _bt = brightness_temperature(
                    ionized_box=ib2,
                    perturbed_field=pf2,
                    spin_temp=st2 if flag_options.USE_TS_FLUCT else None,
                    **iokw,
                )

                bt[redshift.index(z)] = _bt

            else:
                ib = ib2
                pf = pf2
                _bt = None

            perturb_files.append((z, os.path.join(direc, pf2.filename)))
            if flag_options.USE_HALO_FIELD:
                hbox_files.append((z, os.path.join(direc, hb2.filename)))
                pth_files.append((z, os.path.join(direc, ph2.filename)))
            if flag_options.USE_TS_FLUCT:
                spin_temp_files.append((z, os.path.join(direc, st2.filename)))
            ionize_files.append((z, os.path.join(direc, ib2.filename)))

            if _bt is not None:
                brightness_files.append((z, os.path.join(direc, _bt.filename)))

        if flag_options.PHOTON_CONS_TYPE == 1:
            photon_nonconservation_data = _get_photon_nonconservation_data()

        if lib.photon_cons_allocated:
            lib.FreePhotonConsMemory()

        coevals = [
            Coeval(
                redshift=z,
                initial_conditions=init_box,
                perturbed_field=perturb[redshifts.index(z)],
                ionized_box=ib,
                brightness_temp=_bt,
                ts_box=st,
                halobox=hb if flag_options.USE_HALO_FIELD else None,
                photon_nonconservation_data=photon_nonconservation_data,
                cache_files={
                    "init": [(0, os.path.join(direc, init_box.filename))],
                    "perturb_field": perturb_files,
                    "halobox": hbox_files,
                    "ionized_box": ionize_files,
                    "brightness_temp": brightness_files,
                    "spin_temp": spin_temp_files,
                    "pt_halos": pth_files,
                },
            )
            for z, ib, _bt, st, hb in zip(redshift, ib_tracker, bt, st_tracker, hb_tracker)
        ]

        # If a single redshift was passed, then pass back singletons.
        if singleton:
            coevals = coevals[0]

        logger.debug("Returning from Coeval")

        return coevals


def _get_required_redshifts_coeval(flag_options, redshift) -> list[float]:
    if min(redshift) < global_params.Z_HEAT_MAX and (
        flag_options.INHOMO_RECO or flag_options.USE_TS_FLUCT
    ):
        redshifts = _logscroll_redshifts(
            min(redshift),
            global_params.ZPRIME_STEP_FACTOR,
            global_params.Z_HEAT_MAX,
        )
        # Set the highest redshift to exactly Z_HEAT_MAX. This makes the coeval run
        # at exactly the same redshift as the spin temperature box. There's literally
        # no point going higher for a coeval, since the user is only interested in
        # the final "redshift" (if they've specified a z in redshift that is higher
        # that Z_HEAT_MAX, we add it back in below, and so they'll still get it).
        redshifts = np.clip(redshifts, None, global_params.Z_HEAT_MAX)

    else:
        redshifts = [min(redshift)]
    # Add in the redshift defined by the user, and sort in order
    # Turn into a set so that exact matching user-set redshift
    # don't double-up with scrolling ones.
    redshifts = np.concatenate((redshifts, redshift))
    redshifts = np.sort(np.unique(redshifts))[::-1]
    return redshifts.tolist()


def run_lightcone(
    *,
    redshift: float = None,
    max_redshift: float = None,
    lightcone_quantities=("brightness_temp",),
    lightconer: Lightconer | None = None,
    user_params=None,
    cosmo_params=None,
    astro_params=None,
    flag_options=None,
    regenerate=None,
    write=None,
    global_quantities=("brightness_temp", "xH_box"),
    direc=None,
    init_box=None,
    perturb=None,
    random_seed=None,
    coeval_callback=None,
    coeval_callback_redshifts=1,
    use_interp_perturb_field=False,
    cleanup=True,
    hooks=None,
    always_purge: bool = False,
    lightcone_filename: str | Path = None,
    return_at_z: float = 0.0,
    **global_kwargs,
):
    r"""
    Evaluate a full lightcone ending at a given redshift.

    This is generally the easiest and most efficient way to generate a lightcone, though it can
    be done manually by using the lower-level functions which are called by this function.

    Parameters
    ----------
    redshift : float
        The minimum redshift of the lightcone.
    max_redshift : float, optional
        The maximum redshift at which to keep lightcone information. By default, this is equal to
        `z_heat_max`. Note that this is not *exact*, but will be typically slightly exceeded.
    user_params : `~UserParams`, optional
        Defines the overall options and parameters of the run.
    astro_params : :class:`~AstroParams`, optional
        Defines the astrophysical parameters of the run.
    cosmo_params : :class:`~CosmoParams`, optional
        Defines the cosmological parameters used to compute initial conditions.
    flag_options : :class:`~FlagOptions`, optional
        Options concerning how the reionization process is run, eg. if spin temperature
        fluctuations are required.
    lightcone_quantities : tuple of str, optional
        The quantities to form into a lightcone. By default, just the brightness
        temperature. Note that these quantities must exist in one of the output
        structures:

        * :class:`~InitialConditions`
        * :class:`~PerturbField`
        * :class:`~TsBox`
        * :class:`~IonizedBox`
        * :class:`BrightnessTemp`

        To get a full list of possible quantities, run :func:`get_all_fieldnames`.
    global_quantities : tuple of str, optional
        The quantities to save as globally-averaged redshift-dependent functions.
        These may be any of the quantities that can be used in ``lightcone_quantities``.
        The mean is taken over the full 3D cube at each redshift, rather than a 2D
        slice.
    init_box : :class:`~InitialConditions`, optional
        If given, the user and cosmo params will be set from this object, and it will not be
        re-calculated.
    perturb : list of :class:`~PerturbedField`, optional
        If given, must be compatible with init_box. It will merely negate the necessity of
        re-calculating the
        perturb fields. It will also be used to set the redshift if given.
    coeval_callback : callable, optional
        User-defined arbitrary function computed on :class:`~Coeval`, at redshifts defined in
        `coeval_callback_redshifts`.
        If given, the function returns :class:`~LightCone` and the list of `coeval_callback` outputs.
    coeval_callback_redshifts : list or int, optional
        Redshifts for `coeval_callback` computation.
        If list, computes the function on `node_redshifts` closest to the specified ones.
        If positive integer, computes the function on every n-th redshift in `node_redshifts`.
        Ignored in the case `coeval_callback is None`.
    use_interp_perturb_field : bool, optional
        Whether to use a single perturb field, at the lowest redshift of the lightcone,
        to determine all spin temperature fields. If so, this field is interpolated in the
        underlying C-code to the correct redshift. This is less accurate (and no more efficient),
        but provides compatibility with older versions of 21cmFAST.
    cleanup : bool, optional
        A flag to specify whether the C routine cleans up its memory before returning.
        Typically, if `spin_temperature` is called directly, you will want this to be
        true, as if the next box to be calculate has different shape, errors will occur
        if memory is not cleaned. Note that internally, this is set to False until the
        last iteration.
    minimize_memory_usage
        If switched on, the routine will do all it can to minimize peak memory usage.
        This will be at the cost of disk I/O and CPU time. Recommended to only set this
        if you are running particularly large boxes, or have low RAM.
    lightcone_filename
        The filename to which to save the lightcone. The lightcone is returned in
        memory, and can be saved manually later, but including this filename will
        save the lightcone on each iteration, which can be helpful for checkpointing.
    return_at_z
        If given, evaluation of the lightcone will be stopped at the given redshift,
        and the partial lightcone object will be returned. Lightcone evaluation can
        continue if the returned lightcone is saved to file, and this file is passed
        as `lightcone_filename`.
    \*\*global_kwargs :
        Any attributes for :class:`~py21cmfast.inputs.GlobalParams`. This will
        *temporarily* set global attributes for the duration of the function. Note that
        arguments will be treated as case-insensitive.

    Returns
    -------
    lightcone : :class:`~py21cmfast.LightCone`
        The lightcone object.
    coeval_callback_output : list
        Only if coeval_callback in not None.

    Other Parameters
    ----------------
    regenerate, write, direc, random_seed
        See docs of :func:`initial_conditions` for more information.
    """
    direc, regenerate, hooks = _get_config_options(direc, regenerate, write, hooks)

    if cosmo_params is None and lightconer is not None:
        cosmo_params = CosmoParams.from_astropy(lightconer.cosmo)

    with global_params.use(**global_kwargs):
        # First, get the parameters OTHER than redshift...

        (
            random_seed,
            user_params,
            cosmo_params,
            flag_options,
            astro_params,
        ) = _setup_inputs(
            {
                "random_seed": random_seed,
                "user_params": user_params,
                "cosmo_params": cosmo_params,
                "flag_options": flag_options,
                "astro_params": astro_params,
            },
            {"init_box": init_box, "perturb": perturb},
        )

        if redshift is None and perturb is None:
            if lightconer is None:
                raise ValueError(
                    "You must provide either redshift, perturb or lightconer"
                )
            else:
                redshift = lightconer.lc_redshifts.min()

        elif redshift is None:
            redshift = perturb.redshift
        elif redshift is not None:
            warnings.warn(
                "passing redshift directly is deprecated, please use the Lightconer interface instead",
                category=DeprecationWarning,
            )

        if user_params.MINIMIZE_MEMORY and not write:
            raise ValueError(
                "If trying to minimize memory usage, you must be caching. Set write=True!"
            )

        max_redshift = (
            global_params.Z_HEAT_MAX
            if (
                flag_options.INHOMO_RECO
                or flag_options.USE_TS_FLUCT
                or (max_redshift is None and lightconer is None)
            )
            else (
                max_redshift
                if max_redshift is not None
                else lightconer.lc_redshifts.max()
            )
        )

        if lightconer is None:
            lightconer = RectilinearLightconer.with_equal_cdist_slices(
                min_redshift=redshift,
                max_redshift=max_redshift,
                resolution=user_params.cell_size,
                cosmo=cosmo_params.cosmo,
                quantities=lightcone_quantities,
                get_los_velocity=not flag_options.APPLY_RSDS,
            )
        lightconer.validate_options(user_params, flag_options)

        # Get the redshift through which we scroll and evaluate the ionization field.
        scrollz = np.array(
            _logscroll_redshifts(
                redshift, global_params.ZPRIME_STEP_FACTOR, max_redshift
            )
        )

        lcz = lightconer.lc_redshifts
        if not np.all(min(scrollz) * 0.99 < lcz) and np.all(lcz < max(scrollz) * 1.01):
            # We have a 1% tolerance on the redshifts, because the lightcone redshifts are
            # computed via inverse fitting the comoving_distance.
            raise ValueError(
                "The lightcone redshifts are not compatible with the given redshift."
                f"The range of computed redshifts is {min(scrollz)} to {max(scrollz)}, "
                f"while the lightcone redshift range is {lcz.min()} to {lcz.max()}."
            )

        if (
            flag_options.PHOTON_CONS_TYPE == 1
            and np.amin(scrollz) < global_params.PhotonConsEndCalibz
        ):
            raise ValueError(
                f"""
                You have passed a redshift (z = {np.amin(scrollz)}) that is lower than the endpoint
                of the photon non-conservation correction
                (global_params.PhotonConsEndCalibz = {global_params.PhotonConsEndCalibz}).
                If this behaviour is desired then set global_params.PhotonConsEndCalibz to a value lower than
                z = {np.amin(scrollz)}.
                """
            )

        coeval_callback_output = []
        compute_coeval_callback = _get_coeval_callbacks(
            scrollz, coeval_callback, coeval_callback_redshifts
        )

        iokw = {"hooks": hooks, "regenerate": regenerate, "direc": direc}

        if init_box is None:  # no need to get cosmo, user params out of it.
            init_box = initial_conditions(
                user_params=user_params,
                cosmo_params=cosmo_params,
                random_seed=random_seed,
                **iokw,
            )

        # We can go ahead and purge some of the stuff in the init_box, but only if
        # it is cached -- otherwise we could be losing information.
        try:
            # TODO: should really check that the file at path actually contains a fully
            # working copy of the init_box.
            init_box.prepare_for_perturb(flag_options=flag_options, force=always_purge)
        except OSError:
            pass

        if lightcone_filename and Path(lightcone_filename).exists():
            lightcone = LightCone.read(lightcone_filename)
            scrollz = scrollz[scrollz < lightcone._current_redshift]
            if len(scrollz) == 0:
                # The entire lightcone is already full!
                logger.info(
                    f"Lightcone already full at z={lightcone._current_redshift}. Returning."
                )
                return lightcone
            lc = lightcone.lightcones
        else:
            lcn_cls = (
                LightCone
                if isinstance(lightconer, RectilinearLightconer)
                else AngularLightcone
            )
            lc = {
                quantity: np.zeros(
                    lightconer.get_shape(user_params),
                    dtype=np.float32,
                )
                for quantity in lightconer.quantities
            }

            # Special case: AngularLightconer can also save los_velocity
            if getattr(lightconer, "get_los_velocity", False):
                lc["los_velocity"] = np.zeros(
                    lightconer.get_shape(user_params), dtype=np.float32
                )

            lightcone = lcn_cls(
                redshift,
                lightconer.lc_distances,
                user_params,
                cosmo_params,
                astro_params,
                flag_options,
                init_box.random_seed,
                lc,
                node_redshifts=scrollz,
                log10_mturnovers=np.zeros_like(scrollz),
                log10_mturnovers_mini=np.zeros_like(scrollz),
                global_quantities={
                    quantity: np.zeros(len(scrollz)) for quantity in global_quantities
                },
                _globals=dict(global_params.items()),
            )

        if perturb is None:
            zz = scrollz
        else:
            zz = scrollz[:-1]

        perturb_ = []
        for z in zz:
            p = perturb_field(redshift=z, init_boxes=init_box, **iokw)
            if user_params.MINIMIZE_MEMORY:
                try:
                    p.purge(force=always_purge)
                except OSError:
                    pass

            perturb_.append(p)

        if perturb is not None:
            perturb_.append(perturb)
        perturb = perturb_
        perturb_min = perturb[np.argmin(scrollz)]

        # Now that we've got all the perturb fields, we can purge init more.
        try:
            init_box.prepare_for_spin_temp(
                flag_options=flag_options, force=always_purge
            )
        except OSError:
            pass

        kw = {
            **{
                "init_boxes": init_box,
                "astro_params": astro_params,
                "flag_options": flag_options,
            },
            **iokw,
        }

        photon_nonconservation_data = None
        if flag_options.PHOTON_CONS_TYPE != 0:
            setup_photon_cons(**kw)

        if return_at_z > lightcone.redshift and not write:
            raise ValueError(
                "Returning before the final redshift requires caching in order to "
                "continue the simulation later. Set write=True!"
            )

        # Iterate through redshift from top to bottom
        if lightcone.redshift != lightcone._current_redshift:
            logger.info(
                f"Finding boxes at z={lightcone._current_redshift} with seed {lightcone.random_seed} and direc={direc}"
            )
            cached_boxes = get_boxes_at_redshift(
                redshift=lightcone._current_redshift,
                seed=lightcone.random_seed,
                direc=direc,
                user_params=user_params,
                cosmo_params=cosmo_params,
                flag_options=flag_options,
                astro_params=astro_params,
            )
            try:
                st = cached_boxes["TsBox"][0] if flag_options.USE_TS_FLUCT else None
                prev_perturb = cached_boxes["PerturbedField"][0]
                ib = cached_boxes["IonizedBox"][0]
            except (KeyError, IndexError):
                raise OSError(
                    f"No component boxes found at z={lightcone._current_redshift} with "
                    f"seed {lightcone.random_seed} and direc={direc}. You need to have "
                    "run with write=True to continue from a checkpoint."
                )
            pf = prev_perturb
        else:
            st, ib, prev_perturb = None, None, None
            pf = None

        pf = None

        # Now we can purge init_box further.
        try:
            init_box.prepare_for_halos(flag_options=flag_options, force=always_purge)
        except OSError:
            pass

        # we explicitly pass the descendant halos here since we have a redshift list prior
        #   this will generate the extra fields if STOC_MINIMUM_Z is given
        pt_halos = []
        if flag_options.USE_HALO_FIELD:
            halos_desc = None
            for iz, z in enumerate(scrollz[::-1]):
                if not flag_options.FIXED_HALO_GRIDS:
                    halo_field = determine_halo_list(
                        redshift=z,
                        halos_desc=halos_desc,
                        **kw,
                    )
                    halos_desc = halo_field
                    pt_halos += [
                        perturb_halo_list(redshift=z, halo_field=halo_field, **kw)
                    ]

                    # we never want to store every halofield
                    try:
                        pt_halos[iz].purge(force=always_purge)
                    except OSError:
                        pass

            # reverse the halo lists to be in line with the redshift lists
            pt_halos = pt_halos[::-1]

        # Now that we've got all the perturb fields, we can purge init more.
        try:
            init_box.prepare_for_spin_temp(
                flag_options=flag_options, force=always_purge
            )
        except OSError:
            pass

        ph = None

        perturb_files = []
        spin_temp_files = []
        ionize_files = []
        brightness_files = []
        hbox_files = []
        pth_files = []
        log10_mturnovers = np.zeros(len(scrollz))
        log10_mturnovers_mini = np.zeros(len(scrollz))
        hboxes = []
        z_halos = []
        coeval = None
        prev_coeval = None
        st2 = None
        hbox2 = None

        if lightcone_filename and not Path(lightcone_filename).exists():
            lightcone.save(lightcone_filename)

        for iz, z in enumerate(scrollz):
            logger.info(f"Computing Redshift {z} ({iz+1}/{len(scrollz)}) iterations.")

            # Best to get a perturb for this redshift, to pass to brightness_temperature
            pf2 = perturb[iz]
            # This ensures that all the arrays that are required for spin_temp are there,
            # in case we dumped them from memory into file.
            pf2.load_all()
            if flag_options.USE_HALO_FIELD:
                ph = pt_halos[iz]
                ph.load_all()

                hbox2 = halo_box(
                    redshift=z,
                    pt_halos=ph,
                    previous_ionize_box=ib,
                    previous_spin_temp=st,
                    perturbed_field=pf2,
                    **kw,
                )
                z_halos.append(z)
                hboxes.append(hbox2)

                if flag_options.USE_TS_FLUCT:
                    xray_source_box = xray_source(
                        redshift=z,
                        z_halos=z_halos,
                        hboxes=hboxes,
                        **kw,
                    )

            if flag_options.USE_TS_FLUCT:
                st2 = spin_temperature(
                    redshift=z,
                    previous_spin_temp=st,
                    perturbed_field=perturb_min if use_interp_perturb_field else pf2,
                    xray_source_box=(
                        xray_source_box if flag_options.USE_HALO_FIELD else None
                    ),
                    cleanup=(cleanup and iz == (len(scrollz) - 1)),
                    **kw,
                )

            ib2 = ionize_box(
                redshift=z,
                previous_ionize_box=ib,
                perturbed_field=pf2,
                previous_perturbed_field=prev_perturb,
                spin_temp=st2,
                halobox=hbox2,
                cleanup=(cleanup and iz == (len(scrollz) - 1)),
                **kw,
            )
            log10_mturnovers[iz] = ib2.log10_Mturnover_ave
            log10_mturnovers_mini[iz] = ib2.log10_Mturnover_MINI_ave

            bt2 = brightness_temperature(
                ionized_box=ib2,
                perturbed_field=pf2,
                spin_temp=st2,
                **iokw,
            )

            coeval = Coeval(
                redshift=z,
                initial_conditions=init_box,
                perturbed_field=pf2,
                ionized_box=ib2,
                brightness_temp=bt2,
                ts_box=st2,
                halobox=hbox2,
                photon_nonconservation_data=photon_nonconservation_data,
                _globals=None,
            )

            if coeval_callback is not None and compute_coeval_callback[iz]:
                try:
                    coeval_callback_output.append(coeval_callback(coeval))
                except Exception as e:
                    if sum(compute_coeval_callback[: iz + 1]) == 1:
                        raise RuntimeError(
                            f"coeval_callback computation failed on first trial, z={z}."
                        )
                    else:
                        logger.warning(
                            f"coeval_callback computation failed on z={z}, skipping. {type(e).__name__}: {e}"
                        )

            perturb_files.append((z, os.path.join(direc, pf2.filename)))
            if flag_options.USE_HALO_FIELD:
                hbox_files.append((z, os.path.join(direc, hbox2.filename)))
                pth_files.append((z, os.path.join(direc, ph.filename)))
            if flag_options.USE_TS_FLUCT:
                spin_temp_files.append((z, os.path.join(direc, st2.filename)))
            ionize_files.append((z, os.path.join(direc, ib2.filename)))
            brightness_files.append((z, os.path.join(direc, bt2.filename)))

            # Save mean/global quantities
            for quantity in global_quantities:
                lightcone.global_quantities[quantity][iz] = np.mean(
                    getattr(coeval, quantity)
                )

            # Get lightcone slices
            if prev_coeval is not None:
                for quantity, idx, this_lc in lightconer.make_lightcone_slices(
                    coeval, prev_coeval
                ):
                    if this_lc is not None:
                        lightcone.lightcones[quantity][..., idx] = this_lc
                        lc_index = idx

                if lightcone_filename:
                    lightcone.make_checkpoint(
                        lightcone_filename, redshift=z, index=lc_index
                    )

            # Save current ones as old ones.
            if flag_options.USE_TS_FLUCT:
                st = st2
            ib = ib2
            if flag_options.USE_MINI_HALOS:
                prev_perturb = pf2
            prev_coeval = coeval

            if pf is not None:
                try:
                    pf.purge(force=always_purge)
                except OSError:
                    pass

            if ph is not None:
                try:
                    ph.purge(force=always_purge)
                except OSError:
                    pass

            pf = pf2

            if z <= return_at_z:
                # Optionally return when the lightcone is only partially filled
                break

        if flag_options.PHOTON_CONS_TYPE == 1:
            photon_nonconservation_data = _get_photon_nonconservation_data()

        if lib.photon_cons_allocated:
            lib.FreePhotonConsMemory()

        if isinstance(lightcone, AngularLightcone) and lightconer.get_los_velocity:
            lightcone.compute_rsds(
                fname=lightcone_filename, n_subcells=astro_params.N_RSD_STEPS
            )

        # Append some info to the lightcone before we return
        lightcone.photon_nonconservation_data = photon_nonconservation_data
        lightcone.cache_files = {
            "init": [(0, os.path.join(direc, init_box.filename))],
            "perturb_field": perturb_files,
            "ionized_box": ionize_files,
            "brightness_temp": brightness_files,
            "spin_temp": spin_temp_files,
            "halobox": hbox_files,
            "pt_halos": pth_files,
        }

        lightcone.log10_mturnovers = log10_mturnovers
        lightcone.log10_mturnovers_mini = log10_mturnovers_mini

        if coeval_callback is None:
            return lightcone
        else:
            return lightcone, coeval_callback_output


def _get_coeval_callbacks(
    scrollz: list[float], coeval_callback, coeval_callback_redshifts
) -> list[bool]:
    compute_coeval_callback = [False] * len(scrollz)

    if coeval_callback is not None:
        if isinstance(coeval_callback_redshifts, (list, np.ndarray)):
            for coeval_z in coeval_callback_redshifts:
                assert isinstance(coeval_z, (int, float, np.number))
                compute_coeval_callback[
                    np.argmin(np.abs(np.array(scrollz) - coeval_z))
                ] = True
            if sum(compute_coeval_callback) != len(coeval_callback_redshifts):
                logger.warning(
                    "some of the coeval_callback_redshifts refer to the same node_redshift"
                )
        elif (
            isinstance(coeval_callback_redshifts, int) and coeval_callback_redshifts > 0
        ):
            compute_coeval_callback = [
                not i % coeval_callback_redshifts for i in range(len(scrollz))
            ]
        else:
            raise ValueError("coeval_callback_redshifts has to be list or integer > 0.")

    return compute_coeval_callback
