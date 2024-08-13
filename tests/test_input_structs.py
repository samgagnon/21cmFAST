"""
Unit tests for input structures
"""

import pytest

import pickle
import warnings

from py21cmfast import AstroParams  # An example of a struct with defaults
from py21cmfast import CosmoParams, FlagOptions, UserParams, __version__, global_params
from py21cmfast.wrapper.inputs import validate_all_inputs


@pytest.fixture(scope="module")
def c():
    return CosmoParams(SIGMA_8=0.8)


def test_diff(c):
    """Ensure that the python dict has all fields"""
    d = CosmoParams(SIGMA_8=0.9)

    assert c is not d
    assert c != d
    assert hash(c) != hash(d)


def test_constructed_the_same(c):
    c2 = CosmoParams(SIGMA_8=0.8)

    assert c is not c2
    assert c == c2
    assert hash(c) == hash(c2)


def test_bad_construction(c):
    with pytest.raises(TypeError):
        CosmoParams(c, c)

    with pytest.raises(TypeError):
        CosmoParams(UserParams())

    with pytest.raises(TypeError):
        CosmoParams(1)


def test_warning_bad_params():
    with pytest.warns(
        UserWarning, match="The following parameters to CosmoParams are not supported"
    ):
        CosmoParams(SIGMA_8=0.8, bad_param=1)


def test_constructed_from_itself(c):
    c3 = CosmoParams(c)

    assert c == c3
    assert c is not c3


def test_dynamic_variables():
    u = UserParams()
    assert u.DIM == 3 * u.HII_DIM

    u.update(DIM=200)

    assert u.DIM == 200


def test_clone():
    u = UserParams()
    v = u.clone()
    assert u == v


def test_repr(c):
    assert "SIGMA_8:0.8" in repr(c)


def test_pickle(c):
    # Make sure we can pickle/unpickle it.
    c4 = pickle.dumps(c)
    c4 = pickle.loads(c4)
    assert c == c4

    # Make sure the c data gets loaded fine.
    assert c4._cstruct.SIGMA_8 == c._cstruct.SIGMA_8


def test_self(c):
    c5 = CosmoParams(c.self)
    assert c5 == c
    assert c5.pystruct == c.pystruct
    assert c5.defining_dict == c.defining_dict
    assert (
        c5.defining_dict != c5.pystruct
    )  # not the same because the former doesn't include dynamic parameters.
    assert c5.self == c.self


def test_update():
    c = CosmoParams()
    c_pystruct = c.pystruct

    c.update(
        SIGMA_8=0.9
    )  # update c parameters. since pystruct as dynamically created, it is a new object each call.
    assert c_pystruct != c.pystruct


def test_c_structures(c):
    # See if the C structures are behaving correctly
    c2 = CosmoParams(SIGMA_8=0.8)

    assert c() != c2()
    assert (
        c() is c()
    )  # Re-calling should not re-make the object (object should have persistence)


def test_c_struct_update():
    c = CosmoParams()
    _c = c()
    c.update(SIGMA_8=0.8)
    assert _c != c()


def test_update_inhomo_reco():
    ap = AstroParams(R_BUBBLE_MAX=25)
    # regex
    msg = r"This is non\-standard \(but allowed\), and usually occurs upon manual update of INHOMO_RECO"
    with pytest.warns(UserWarning, match=msg):
        ap.update(INHOMO_RECO=True)
        ap.R_BUBBLE_MAX


def test_mmin():
    fo = FlagOptions(USE_MASS_DEPENDENT_ZETA=True)
    assert fo.M_MIN_in_Mass


def test_globals():
    orig = global_params.Z_HEAT_MAX

    with global_params.use(Z_HEAT_MAX=10.0):
        assert global_params.Z_HEAT_MAX == 10.0
        assert global_params._cobj.Z_HEAT_MAX == 10.0

    assert global_params.Z_HEAT_MAX == orig


@pytest.mark.xfail(
    __version__ >= "4.0.0", reason="the warning can be removed in v4", strict=True
)
def test_interpolation_table_warning():
    with pytest.warns(UserWarning, match="setting has changed in v3.1.2"):
        UserParams().USE_INTERPOLATION_TABLES

    with warnings.catch_warnings():
        warnings.simplefilter("error")
        UserParams(USE_INTERPOLATION_TABLES=True).USE_INTERPOLATION_TABLES


def test_validation():
    c = CosmoParams()
    a = AstroParams(R_BUBBLE_MAX=100)
    f = FlagOptions(USE_EXP_FILTER=False)  # needed for HII_FILTER checks
    u = UserParams(BOX_LEN=50)

    with global_params.use(HII_FILTER=2):
        with pytest.warns(UserWarning, match="Setting R_BUBBLE_MAX to BOX_LEN"):
            validate_all_inputs(
                cosmo_params=c, astro_params=a, flag_options=f, user_params=u
            )

        assert a.R_BUBBLE_MAX == u.BOX_LEN

    a.update(R_BUBBLE_MAX=20)

    with global_params.use(HII_FILTER=1):
        with pytest.raises(ValueError, match="Your R_BUBBLE_MAX is > BOX_LEN/3"):
            validate_all_inputs(
                cosmo_params=c, astro_params=a, flag_options=f, user_params=u
            )


def test_user_params():
    up = UserParams()
    up_non_cubic = UserParams(NON_CUBIC_FACTOR=1.5)

    assert up_non_cubic.tot_fft_num_pixels == 1.5 * up.tot_fft_num_pixels
    assert up_non_cubic.HII_tot_num_pixels == up.HII_tot_num_pixels * 1.5

    with pytest.raises(
        ValueError,
        match="NON_CUBIC_FACTOR \\* DIM and NON_CUBIC_FACTOR \\* HII_DIM must be integers",
    ):
        up = UserParams(NON_CUBIC_FACTOR=1.1047642)
        up.NON_CUBIC_FACTOR

    assert up.cell_size / up.cell_size_hires == up.DIM / up.HII_DIM


# Testing all the FlagOptions dependencies, including emitted warnings
def test_flag_options():
    with pytest.raises(
        ValueError, match="You have set SUBCELL_RSD to True but APPLY_RSDS is False"
    ):
        flg = FlagOptions(SUBCELL_RSD=True, APPLY_RSDS=False)
        flg.SUBCELL_RSD

    with pytest.raises(
        ValueError, match="USE_HALO_FIELD requires USE_MASS_DEPENDENT_ZETA"
    ):
        flg = FlagOptions(USE_MASS_DEPENDENT_ZETA=False, USE_HALO_FIELD=True)
        flg.USE_MASS_DEPENDENT_ZETA
    with pytest.raises(
        ValueError, match="USE_MINI_HALOS requires USE_MASS_DEPENDENT_ZETA"
    ):
        flg = FlagOptions(
            USE_MASS_DEPENDENT_ZETA=False,
            USE_HALO_FIELD=False,
            USE_MINI_HALOS=True,
            INHOMO_RECO=True,
            USE_TS_FLUCT=True,
        )
        flg.USE_MASS_DEPENDENT_ZETA
    with pytest.raises(
        ValueError,
        match="You have set USE_MASS_DEPENDENT_ZETA to True but M_Min_in_Mass is False!",
    ):
        flg = FlagOptions(USE_MASS_DEPENDENT_ZETA=True, M_MIN_in_Mass=False)
        flg.USE_MASS_DEPENDENT_ZETA

    with pytest.raises(ValueError, match="USE_MINI_HALOS requires INHOMO_RECO"):
        flg = FlagOptions(USE_MINI_HALOS=True, USE_TS_FLUCT=True, INHOMO_RECO=False)
        flg.USE_MINI_HALOS
    with pytest.raises(ValueError, match="USE_MINI_HALOS requires USE_TS_FLUCT"):
        flg = FlagOptions(USE_MINI_HALOS=True, INHOMO_RECO=True, USE_TS_FLUCT=False)
        flg.USE_MINI_HALOS

    with pytest.raises(
        ValueError, match="USE_MINI_HALOS and USE_HALO_FIELD are not compatible"
    ):
        flg = FlagOptions(
            PHOTON_CONS_TYPE=1, USE_MINI_HALOS=True, INHOMO_RECO=True, USE_TS_FLUCT=True
        )
        flg.PHOTON_CONS_TYPE
    with pytest.raises(
        ValueError, match="USE_MINI_HALOS and USE_HALO_FIELD are not compatible"
    ):
        flg = FlagOptions(PHOTON_CONS_TYPE=1, USE_HALO_FIELD=True)
        flg.PHOTON_CONS_TYPE

    with pytest.raises(
        ValueError, match="HALO_STOCHASTICITY must be used with USE_HALO_FIELD"
    ):
        flg = FlagOptions(USE_HALO_FIELD=False, HALO_STOCHASTICITY=True)
        flg.HALO_STOCHASTICITY

    with pytest.raises(
        ValueError, match="USE_EXP_FILTER can only be used with CELL_RECOMB"
    ):
        flg = FlagOptions(USE_EXP_FILTER=True, CELL_RECOMB=False)
        flg.USE_EXP_FILTER

    with global_params.use(HII_FILTER=1):
        with pytest.raises(
            ValueError,
            match="USE_EXP_FILTER can only be used with a real-space tophat HII_FILTER==0",
        ):
            flg = FlagOptions(USE_EXP_FILTER=True)
            flg.USE_EXP_FILTER
