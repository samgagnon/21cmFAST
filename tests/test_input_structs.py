"""
Unit tests for input structures
"""

import pytest

import pickle
import tomllib
import warnings
from pathlib import Path

from py21cmfast import (
    AstroParams,
    CosmoParams,
    FlagOptions,
    InputParameters,
    IonizedBox,
    UserParams,
    __version__,
    global_params,
)


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

    with pytest.raises(TypeError):
        CosmoParams.new(None, SIGMA_8=0.8, bad_param=1)


def test_constructed_from_itself(c):
    c3 = CosmoParams.new(c)

    assert c == c3
    assert c is not c3


def test_altered_construction(c):
    c3 = CosmoParams.new(c, SIGMA_8=0.7)

    assert c != c3
    assert c3.SIGMA_8 == 0.7


def test_dynamic_variables():
    u = UserParams()
    assert u.DIM == 3 * u.HII_DIM

    u = u.clone(DIM=200)

    assert u.DIM == 200


def test_clone():
    u = UserParams()
    v = u.clone()
    assert u == v
    assert u is not v


def test_repr(c):
    assert "SIGMA_8=0.8" in repr(c)


def test_pickle(c):
    # Make sure we can pickle/unpickle it.
    c4 = pickle.dumps(c)
    c4 = pickle.loads(c4)
    assert c == c4

    # Make sure the c data gets loaded fine.
    assert c4.cstruct.SIGMA_8 == c.cstruct.SIGMA_8


def test_self(c):
    c5 = CosmoParams.new(c)
    assert c5 == c
    assert c5.asdict() == c.asdict()
    assert c5.cdict == c.cdict
    assert (
        c5.cdict != c5.asdict()
    )  # not the same because the former doesn't include dynamic parameters.
    assert c5 == c


def test_c_structures(c):
    # See if the C structures are behaving correctly
    c2 = CosmoParams(SIGMA_8=0.8)

    assert c is not c2


def test_mmin():
    fo = FlagOptions(USE_MASS_DEPENDENT_ZETA=True)
    assert fo.M_MIN_in_Mass


def test_globals():
    orig = global_params.Pop2_ion

    with global_params.use(Pop2_ion=1000.0):
        assert global_params.Pop2_ion == 1000.0
        assert global_params._cobj.Pop2_ion == 1000.0

    assert global_params.Pop2_ion == orig


def test_validation():
    c = CosmoParams()
    f = FlagOptions(USE_EXP_FILTER=False)  # needed for HII_FILTER checks
    a = AstroParams(R_BUBBLE_MAX=100)
    u = UserParams(BOX_LEN=50)

    with global_params.use(HII_FILTER=2):
        with pytest.raises(ValueError, match="R_BUBBLE_MAX is larger than BOX_LEN"):
            InputParameters(
                cosmo_params=c,
                astro_params=a,
                user_params=u,
                flag_options=f,
                random_seed=1,
            )

    a = a.clone(R_BUBBLE_MAX=20)
    with global_params.use(HII_FILTER=1):
        with pytest.raises(ValueError, match="Your R_BUBBLE_MAX is > BOX_LEN/3"):
            InputParameters(
                cosmo_params=c,
                astro_params=a,
                user_params=u,
                flag_options=f,
                random_seed=1,
            )

    f = f.clone(INHOMO_RECO=True)
    msg = r"This is non\-standard \(but allowed\), and usually occurs upon manual update of INHOMO_RECO"
    with pytest.warns(UserWarning, match=msg):
        InputParameters(
            cosmo_params=c,
            astro_params=a,
            user_params=u,
            flag_options=f,
            random_seed=1,
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

    assert up.cell_size / up.cell_size_hires == up.DIM / up.HII_DIM


# Testing all the FlagOptions dependencies, including emitted warnings
def test_flag_options():
    with pytest.raises(
        ValueError,
        match="The SUBCELL_RSD flag is only effective if APPLY_RSDS is True.",
    ):
        FlagOptions(SUBCELL_RSD=True, APPLY_RSDS=False)

    with pytest.raises(
        ValueError,
        match="You have set USE_MASS_DEPENDENT_ZETA to False but USE_HALO_FIELD is True!",
    ):
        FlagOptions(USE_MASS_DEPENDENT_ZETA=False, USE_HALO_FIELD=True)
    with pytest.raises(
        ValueError,
        match="You have set USE_MINI_HALOS to True but USE_MASS_DEPENDENT_ZETA is False!",
    ):
        FlagOptions(
            USE_MASS_DEPENDENT_ZETA=False,
            USE_HALO_FIELD=False,
            USE_MINI_HALOS=True,
            INHOMO_RECO=True,
            USE_TS_FLUCT=True,
        )
    with pytest.raises(
        ValueError,
        match="M_MIN_in_Mass must be true if USE_MASS_DEPENDENT_ZETA is true.",
    ):
        FlagOptions(USE_MASS_DEPENDENT_ZETA=True, M_MIN_in_Mass=False)

    with pytest.raises(
        ValueError,
        match="You have set USE_MINI_HALOS to True but INHOMO_RECO is False!",
    ):
        FlagOptions(USE_MINI_HALOS=True, USE_TS_FLUCT=True, INHOMO_RECO=False)
    with pytest.raises(
        ValueError,
        match="You have set USE_MINI_HALOS to True but USE_TS_FLUCT is False!",
    ):
        FlagOptions(USE_MINI_HALOS=True, INHOMO_RECO=True, USE_TS_FLUCT=False)

    with pytest.raises(
        ValueError, match="USE_MINI_HALOS and USE_HALO_FIELD are not compatible"
    ):
        FlagOptions(
            PHOTON_CONS_TYPE="z-photoncons",
            USE_MINI_HALOS=True,
            INHOMO_RECO=True,
            USE_TS_FLUCT=True,
        )
    with pytest.raises(
        ValueError, match="USE_MINI_HALOS and USE_HALO_FIELD are not compatible"
    ):
        FlagOptions(PHOTON_CONS_TYPE="z-photoncons", USE_HALO_FIELD=True)

    with pytest.raises(
        ValueError, match="HALO_STOCHASTICITY is True but USE_HALO_FIELD is False"
    ):
        FlagOptions(USE_HALO_FIELD=False, HALO_STOCHASTICITY=True)

    with pytest.raises(
        ValueError, match="USE_EXP_FILTER is True but CELL_RECOMB is False"
    ):
        FlagOptions(USE_EXP_FILTER=True, CELL_RECOMB=False)

    with global_params.use(HII_FILTER=1):
        with pytest.raises(
            ValueError,
            match="USE_EXP_FILTER can only be used with a real-space tophat HII_FILTER==0",
        ):
            FlagOptions(USE_EXP_FILTER=True)


def test_inputstruct_init(default_seed):
    default_struct = InputParameters(random_seed=default_seed)
    altered_struct = default_struct.evolve_input_structs(BOX_LEN=30)

    assert default_struct.cosmo_params == CosmoParams.new()
    assert default_struct.user_params == UserParams.new()
    assert default_struct.astro_params == AstroParams.new()
    assert default_struct.flag_options == FlagOptions.new()
    assert altered_struct.user_params.BOX_LEN == 30


def test_inputstruct_outputs(
    default_input_struct, default_input_struct_ts, perturbed_field
):
    # NOTE: node_redshifts are not yet saved in inputstruct, so two OutputStruct
    # can still be compatible with different node_redshifts
    example_ib = IonizedBox(inputs=default_input_struct_ts)  # doesn't compute
    with pytest.raises(ValueError, match="InputParameters not compatible with"):
        default_input_struct.check_output_compatibility([example_ib])

    default_input_struct.check_output_compatibility([perturbed_field])


def test_native_template_loading(default_seed):
    template_path = Path(__file__).parent.parent / "src/py21cmfast/templates/"
    with open(template_path / "manifest.toml", "rb") as f:
        manifest = tomllib.load(f)

    # check all files and all aliases work
    for manf_entry in manifest["templates"]:
        for alias in manf_entry["aliases"]:
            assert isinstance(
                InputParameters.from_template(alias, random_seed=default_seed),
                InputParameters,
            )
