"""
These are designed to be unit-tests of the wrapper functionality. They do not test for
correctness of simulations,
but whether different parameter options work/don't work as intended.
"""

import pytest

import h5py
import numpy as np
from astropy import units as un

import py21cmfast as p21c
from py21cmfast.drivers.coeval import get_logspaced_redshifts


@pytest.fixture(scope="module")
def ic_newseed(default_input_struct, tmpdirec):
    return p21c.compute_initial_conditions(
        inputs=default_input_struct.clone(random_seed=33),
        write=True,
        direc=tmpdirec,
    )


@pytest.fixture(scope="module")
def perturb_field_lowz(ic, default_input_struct, low_redshift):
    """A default perturb_field"""
    return p21c.perturb_field(
        redshift=low_redshift,
        inputs=default_input_struct,
        initial_conditions=ic,
        write=True,
    )


@pytest.fixture(scope="module")
def ionize_box(ic, perturbed_field, default_input_struct):
    """A default ionize_box"""
    return p21c.compute_ionization_field(
        initial_conditions=ic,
        inputs=default_input_struct,
        perturbed_field=perturbed_field,
        write=True,
    )


@pytest.fixture(scope="module")
def ionize_box_lowz(ic, perturb_field_lowz, default_input_struct):
    """A default ionize_box at lower redshift."""
    return p21c.compute_ionization_field(
        initial_conditions=ic,
        inputs=default_input_struct,
        perturbed_field=perturb_field_lowz,
        write=True,
    )


@pytest.fixture(scope="module")
def spin_temp_evolution(ic, redshift, default_input_struct_ts):
    """An example spin temperature evolution"""
    scrollz = default_input_struct_ts.node_redshifts
    st_prev = None
    outputs = []
    for iz, z in enumerate(scrollz):
        pt = p21c.perturb_field(
            redshift=z,
            inputs=default_input_struct_ts,
            initial_conditions=ic,
        )
        st = p21c.spin_temperature(
            redshift=z,
            initial_conditions=ic,
            perturbed_field=pt,
            previous_spin_temp=st_prev,
            inputs=default_input_struct_ts,
        )
        outputs.append(
            {
                "redshift": z,
                "perturbed_field": pt,
                "spin_temp": st,
            }
        )
        st_prev = st

    return outputs


def test_perturb_field_no_ic(default_input_struct, redshift, perturbed_field):
    """Run a perturb field without passing an init box"""
    with pytest.raises(TypeError):
        p21c.perturb_field(redshift=redshift, user_params=default_input_struct)


def test_ib_no_z(ic, default_input_struct):
    with pytest.raises(TypeError):
        p21c.compute_ionization_field(
            initial_conditions=ic, inputs=default_input_struct
        )


def test_pf_unnamed_param():
    """Try using an un-named parameter."""
    with pytest.raises(TypeError):
        p21c.perturb_field(7)


def test_perturb_field_ic(perturbed_field, default_input_struct, ic):
    # this will run perturb_field again, since by default regenerate=True for tests.
    # BUT it should produce exactly the same as the default perturb_field since it has
    # the same seed.
    pf = p21c.perturb_field(
        redshift=perturbed_field.redshift,
        initial_conditions=ic,
        inputs=default_input_struct,
    )

    assert len(pf.density) == len(ic.lowres_density)
    assert pf.cosmo_params == ic.cosmo_params
    assert pf.user_params == ic.user_params
    assert not np.all(pf.density == 0)

    assert pf.user_params == perturbed_field.user_params
    assert pf.cosmo_params == perturbed_field.cosmo_params

    assert pf == perturbed_field


def test_cache_exists(default_input_struct, perturbed_field, tmpdirec):
    pf = p21c.PerturbedField(
        redshift=perturbed_field.redshift,
        inputs=default_input_struct,
    )

    assert pf.exists(tmpdirec)

    pf.read(tmpdirec)
    np.testing.assert_allclose(pf.density, perturbed_field.density)
    assert pf == perturbed_field


def test_new_seeds(
    ic_newseed,
    perturb_field_lowz,
    ionize_box_lowz,
    default_input_struct,
    tmpdirec,
):
    inputs_newseed = default_input_struct.clone(random_seed=ic_newseed.random_seed)
    # Perturbed Field
    pf = p21c.perturb_field(
        redshift=perturb_field_lowz.redshift,
        initial_conditions=ic_newseed,
        inputs=inputs_newseed,
    )

    # we didn't write it, and this has a different seed
    assert not pf.exists(direc=tmpdirec)
    assert pf.random_seed != perturb_field_lowz.random_seed
    assert not np.all(pf.density == perturb_field_lowz.density)

    # Ionization Box
    with pytest.raises(ValueError):
        p21c.compute_ionization_field(
            initial_conditions=ic_newseed,
            perturbed_field=perturb_field_lowz,
            inputs=default_input_struct,
        )

    ib = p21c.compute_ionization_field(
        initial_conditions=ic_newseed,
        perturbed_field=pf,
        inputs=inputs_newseed,
    )

    # we didn't write it, and this has a different seed
    assert not ib.exists(direc=tmpdirec)
    assert ib.random_seed != ionize_box_lowz.random_seed
    assert not np.all(ib.xH_box == ionize_box_lowz.xH_box)


def test_ib_from_pf(perturbed_field, ic, default_input_struct):
    ib = p21c.compute_ionization_field(
        initial_conditions=ic,
        perturbed_field=perturbed_field,
        inputs=default_input_struct,
    )
    assert ib.redshift == perturbed_field.redshift
    assert ib.user_params == perturbed_field.user_params
    assert ib.cosmo_params == perturbed_field.cosmo_params


def test_ib_override_global(ic, perturbed_field, default_input_struct):
    # save previous z_heat_max
    saved_val = p21c.global_params.Pop2_ion

    p21c.compute_ionization_field(
        initial_conditions=ic,
        perturbed_field=perturbed_field,
        inputs=default_input_struct,
        pop2_ion=3500,
    )

    assert p21c.global_params.Pop2_ion == saved_val


def test_ib_bad_st(ic, default_input_struct, perturbed_field, redshift):
    with pytest.raises((ValueError, AttributeError)):
        p21c.compute_ionization_field(
            inputs=default_input_struct,
            initial_conditions=ic,
            perturbed_field=perturbed_field,
            spin_temp=ic,
        )


def test_bt(ionize_box, default_input_struct, spin_temp_evolution, perturbed_field):
    curr_st = spin_temp_evolution[-1]["spin_temp"]
    with pytest.raises(TypeError):  # have to specify param names
        p21c.brightness_temperature(
            default_input_struct, ionize_box, curr_st, perturbed_field
        )

    # this will fail because ionized_box was not created with spin temperature.
    with pytest.raises(ValueError):
        p21c.brightness_temperature(
            inputs=default_input_struct,
            ionized_box=ionize_box,
            perturbed_field=perturbed_field,
            spin_temp=curr_st,
        )

    bt = p21c.brightness_temperature(
        ionized_box=ionize_box,
        perturbed_field=perturbed_field,
        inputs=default_input_struct,
    )

    assert bt.cosmo_params == perturbed_field.cosmo_params
    assert bt.user_params == perturbed_field.user_params
    assert bt.flag_options == ionize_box.flag_options
    assert bt.astro_params == ionize_box.astro_params


def test_coeval_against_direct(ic, perturbed_field, ionize_box, default_input_struct):
    coeval = p21c.run_coeval(
        perturbed_field=perturbed_field,
        initial_conditions=ic,
        inputs=default_input_struct,
    )

    assert coeval.init_struct == ic
    assert coeval.perturb_struct == perturbed_field
    assert coeval.ionization_struct == ionize_box


def test_using_cached_halo_field(ic, test_direc, default_input_struct):
    """Test whether the C-based memory in halo fields is cached correctly.

    Prior to v3.1 this was segfaulting, so this test ensure that this behaviour does
    not regress.
    """
    inputs = default_input_struct.evolve_input_structs(USE_HALO_FIELD=True)
    halo_field = p21c.determine_halo_list(
        redshift=10.0,
        initial_conditions=ic,
        inputs=inputs,
        write=True,
        direc=test_direc,
    )

    pt_halos = p21c.perturb_halo_list(
        initial_conditions=ic,
        inputs=inputs,
        halo_field=halo_field,
        write=True,
        direc=test_direc,
    )

    print("DONE WITH FIRST BOXES!")
    # Now get the halo field again at the same redshift -- should be cached
    new_halo_field = p21c.determine_halo_list(
        redshift=10.0,
        initial_conditions=ic,
        inputs=inputs,
        write=False,
        regenerate=False,
    )

    new_pt_halos = p21c.perturb_halo_list(
        redshift=10.0,
        initial_conditions=ic,
        inputs=inputs,
        halo_field=new_halo_field,
        write=False,
        regenerate=False,
    )

    np.testing.assert_allclose(new_halo_field.halo_masses, halo_field.halo_masses)
    np.testing.assert_allclose(pt_halos.halo_coords, new_pt_halos.halo_coords)


def test_first_box(default_input_struct_ts):
    """Tests whether the first_box idea works for spin_temp.
    This test was breaking before we set the z_heat_max box to actually get
    the correct dimensions (before it was treated as a dummy).
    """
    inputs = default_input_struct_ts.evolve_input_structs(
        HII_DIM=default_input_struct_ts.user_params.HII_DIM + 1
    )
    initial_conditions = p21c.compute_initial_conditions(
        inputs=inputs,
        random_seed=1,
    )

    prevst = None
    for z in [default_input_struct_ts.user_params.Z_HEAT_MAX + 1e-2, 29.0]:
        print(f"z={z}")
        perturbed_field = p21c.perturb_field(
            inputs=inputs,
            redshift=z,
            initial_conditions=initial_conditions,
        )

        spin_temp = p21c.spin_temperature(
            initial_conditions=initial_conditions,
            perturbed_field=perturbed_field,
            inputs=inputs,
            previous_spin_temp=prevst,
        )
        prevst = spin_temp

    assert spin_temp.redshift == 29.0
