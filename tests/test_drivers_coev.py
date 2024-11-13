"""
These are designed to be unit-tests of the lightcone drivers. They do not test for
correctness of simulations,
but whether different parameter options work/don't work as intended.
"""

import pytest

import py21cmfast as p21c
from py21cmfast import run_coeval


def test_coeval_st(ic, default_flag_options_ts, perturbed_field):
    coeval = run_coeval(
        initial_conditions=ic,
        perturbed_field=perturbed_field,
        flag_options=default_flag_options_ts,
    )

    assert isinstance(coeval.spin_temp_struct, p21c.TsBox)


def test_run_coeval_bad_inputs(ic, default_astro_params, default_flag_options):
    with pytest.raises(
        ValueError, match="Either out_redshifts or perturb must be given"
    ):
        run_coeval(
            initial_conditions=ic,
            astro_params=default_astro_params,
            flag_options=default_flag_options,
        )


def test_coeval_lowerz_than_photon_cons(ic, default_flag_options):
    with pytest.raises(ValueError, match="You have passed a redshift"):
        run_coeval(
            initial_conditions=ic,
            out_redshifts=2.0,
            flag_options=default_flag_options.clone(
                PHOTON_CONS_TYPE="z-photoncons",
            ),
        )