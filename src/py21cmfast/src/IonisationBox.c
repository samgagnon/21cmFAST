// Re-write of find_HII_bubbles.c for being accessible within the MCMC
#include <stdlib.h>
#include <stdio.h>
#include <stdbool.h>
#include <math.h>
#include <omp.h>
#include <complex.h>
#include <fftw3.h>
#include <gsl/gsl_rng.h>
#include <gsl/gsl_randist.h>
#include "cexcept.h"
#include "exceptions.h"
#include "logger.h"

#include "Constants.h"
#include "InputParameters.h"
#include "OutputStructs.h"
#include "cosmology.h"
#include "hmf.h"
#include "indexing.h"
#include "dft.h"
#include "recombinations.h"
#include "debugging.h"
#include "heating_helper_progs.h"
#include "photoncons.h"
#include "thermochem.h"
#include "interp_tables.h"
#include "filtering.h"
#include "bubble_helper_progs.h"
#include "InitialConditions.h"

#include "IonisationBox.h"

#define LOG10_MTURN_MAX ((double)(10)) //maximum mturn limit enforced on grids

int INIT_RECOMBINATIONS = 1;

//Parameters for the ionisation box calculations
struct IonBoxConstants{
    //redshift constants
    double redshift;
    double stored_redshift;
    double prev_redshift;
    double growth_factor;
    double prev_growth_factor;
    double photoncons_adjustment_factor;
    double dz;
    double fabs_dtdz;

    //compound/alternate flags
    bool fix_mean;
    bool filter_recombinations;

    //astro parameters
    double fstar_10;
    double alpha_star;
    double fstar_7;
    double alpha_star_mini;
    double t_h;
    double t_star_sec;
    double fesc_10;
    double alpha_esc;
    double fesc_7;

    //astro calculated values
    double vcb_norel;
    double mturn_a_nofb;
    double mturn_m_nofb;
    double ion_eff_factor;
    double ion_eff_factor_mini;
    double ion_eff_factor_gl;
    double ion_eff_factor_mini_gl;
    double mfp_meandens;
    double gamma_prefactor;
    double gamma_prefactor_mini;

    double TK_nofluct;
    double adia_TK_term;

    //power-law limits
    double Mlim_Fstar;
    double Mlim_Fesc;
    double Mlim_Fstar_mini;
    double Mlim_Fesc_mini;

    //HMF limits
    double M_min;
    double lnMmin;
    double M_max_gl;
    double lnMmax_gl;
    double sigma_minmass;

    //dimensions
    double pixel_length;
    double pixel_mass;
};

//TODO: Something like these structs should also replace the globals in SpinTemperature.c

//TODO: consider the case of having Radiusspec as array of structs (current)
//  vs struct of arrays. The former allows you to pass a single struct into each function
//  without an index so they don't need to know about other radii.
//  The second is simpler to understand/work with in terms of allocation/scoping
struct RadiusSpec{
    //calculated and stored at the beginning
    double R;
    double M_max_R;
    double ln_M_max_R;
    double sigma_maxmass;
    int R_index;

    //calculated within the loop
    double f_coll_grid_mean;
    double f_coll_grid_mean_MINI;
};

//this struct should hold all the pointers to grids which need to be filtered
struct FilteredGrids{
    //Always Used
    fftwf_complex *deltax_unfiltered, *deltax_filtered;

    //Used when TS_FLUCT==True
    fftwf_complex *xe_unfiltered, *xe_filtered;

    //Used when INHOMO_RECO==True and CELL_RECOMB=False
    fftwf_complex *N_rec_unfiltered, *N_rec_filtered;

    //Used when USE_MINI_HALOS==True and USE_HALO_FIELD=False
    fftwf_complex *prev_deltax_unfiltered, *prev_deltax_filtered;
    fftwf_complex *log10_Mturnover_unfiltered, *log10_Mturnover_filtered;
    fftwf_complex *log10_Mturnover_MINI_unfiltered, *log10_Mturnover_MINI_filtered;

    //Used when USE_HALO_FIELD=True
    fftwf_complex *stars_unfiltered, *stars_filtered;
    fftwf_complex *sfr_unfiltered, *sfr_filtered;
};

void set_ionbox_constants(double redshift, double prev_redshift, CosmoParams *cosmo_params, AstroParams *astro_params,
                         FlagOptions *flag_options, struct IonBoxConstants *consts){
    consts->redshift = redshift;
    //defaults for no photoncons
    consts->stored_redshift = redshift;
    consts->photoncons_adjustment_factor = 1.;

    //dz is only used if inhomo_reco
    if (prev_redshift < 1)
        consts->dz = (1. + redshift) * (global_params.ZPRIME_STEP_FACTOR - 1.);
    else
        consts->dz = redshift - prev_redshift;

    //TODO: Figure out why we have the 1e15 here
    consts->fabs_dtdz = fabs(dtdz(redshift))/1e15; //reduce to have good precision

    consts->growth_factor = dicke(redshift);
    consts->prev_growth_factor = dicke(prev_redshift);
    //whether to fix *integrated* (not sampled) galaxy properties to the expected mean
    //  constant for now, to be a flag later
    consts->fix_mean = !flag_options->USE_HALO_FIELD;
    consts->filter_recombinations = flag_options->INHOMO_RECO && !flag_options->CELL_RECOMB;

    consts->fstar_10 = astro_params->F_STAR10;
    consts->alpha_star = astro_params->ALPHA_STAR;

    consts->fstar_7 = astro_params->F_STAR7_MINI;
    consts->alpha_star_mini = astro_params->ALPHA_STAR_MINI;

    consts->t_h = t_hubble(redshift);
    consts->t_star_sec = astro_params->t_STAR * consts->t_h;

    consts->alpha_esc = astro_params->ALPHA_ESC;
    consts->fesc_10= astro_params->F_ESC10;
    consts->fesc_7 = astro_params->F_ESC7_MINI;

    if(flag_options->PHOTON_CONS_TYPE == 2)
        consts->alpha_esc = get_fesc_fit(redshift);
    else if(flag_options->PHOTON_CONS_TYPE == 3)
        consts->fesc_10 = get_fesc_fit(redshift);

    consts->mturn_a_nofb = flag_options->USE_MINI_HALOS ? atomic_cooling_threshold(redshift) : astro_params->M_TURN;

    consts->mturn_m_nofb = 0.;
    if(flag_options->USE_MINI_HALOS){
        consts->vcb_norel = flag_options->FIX_VCB_AVG ? global_params.VAVG : 0;
        consts->mturn_m_nofb = lyman_werner_threshold(redshift, 0., consts->vcb_norel, astro_params);
    }

    if(consts->mturn_m_nofb < astro_params->M_TURN)consts->mturn_m_nofb = astro_params->M_TURN;
    if(consts->mturn_a_nofb < astro_params->M_TURN)consts->mturn_a_nofb = astro_params->M_TURN;

    if(flag_options->FIXED_HALO_GRIDS || user_params_global->AVG_BELOW_SAMPLER){
        consts->Mlim_Fstar = Mass_limit_bisection(global_params.M_MIN_INTEGRAL, global_params.M_MAX_INTEGRAL, consts->alpha_star, consts->fstar_10);
        consts->Mlim_Fesc = Mass_limit_bisection(global_params.M_MIN_INTEGRAL, global_params.M_MAX_INTEGRAL, consts->alpha_esc, consts->fesc_10);

        if(flag_options->USE_MINI_HALOS){
            consts->Mlim_Fstar_mini = Mass_limit_bisection(global_params.M_MIN_INTEGRAL, global_params.M_MAX_INTEGRAL, consts->alpha_star_mini,
                                                            consts->fstar_7 * pow(1e3,consts->alpha_star_mini));
            consts->Mlim_Fesc_mini = Mass_limit_bisection(global_params.M_MIN_INTEGRAL, global_params.M_MAX_INTEGRAL, consts->alpha_esc,
                                                            consts->fesc_7 * pow(1e3,consts->alpha_esc));
        }
    }

    if(flag_options->USE_MASS_DEPENDENT_ZETA) {
        consts->ion_eff_factor_gl = global_params.Pop2_ion * astro_params->F_STAR10 * consts->fesc_10;
        consts->ion_eff_factor_mini_gl = global_params.Pop3_ion * astro_params->F_STAR7_MINI * astro_params->F_ESC7_MINI;
    }
    else {
        consts->ion_eff_factor_gl = astro_params->HII_EFF_FACTOR;
        consts->ion_eff_factor_mini_gl = 0.;
    }

    //The halo fields already have Fstar,Fesc,nion taken into account, so their global factor differs from the local one
    if(flag_options->USE_HALO_FIELD) {
        consts->ion_eff_factor = 1.;
        consts->ion_eff_factor_mini = 1.;
    }
    else{
        consts->ion_eff_factor = consts->ion_eff_factor_gl;
        consts->ion_eff_factor_mini = consts->ion_eff_factor_mini_gl;
    }

    //Yuxiang's evolving Rmax for MFP in ionised regions
    if(flag_options->USE_EXP_FILTER){
        if (redshift > 6)
            consts->mfp_meandens = 25.483241248322766 / cosmo_params->hlittle;
        else
            consts->mfp_meandens = 112 / cosmo_params->hlittle * pow( (1.+redshift) / 5. , -4.4);
    }

    //set the minimum source mass
    consts->M_min = minimum_source_mass(redshift,false,astro_params,flag_options);
    consts->lnMmin = log(consts->M_min);
    consts->lnMmax_gl = log(global_params.M_MAX_INTEGRAL);
    consts->sigma_minmass = sigma_z0(consts->M_min);

    //global TK and adiabatic terms for temperature without the Ts Calculation
    //final temperature = TK * (1+cT_ad*delta)
    if(!flag_options->USE_TS_FLUCT){
        consts->TK_nofluct = T_RECFAST(redshift,0);
        //finding the adiabatic index at the initial redshift from 2302.08506 to fix adiabatic fluctuations.
        consts->adia_TK_term = cT_approx(redshift);
    }
    consts->pixel_length = user_params_global->BOX_LEN/(double)user_params_global->HII_DIM;
    consts->pixel_mass = cosmo_params_global->OMm * RHOcrit * pow(consts->pixel_length,3);

    consts->gamma_prefactor = pow(1+redshift,2) * CMperMPC * SIGMA_HI * global_params.ALPHA_UVB \
                                 / (global_params.ALPHA_UVB+2.75) * N_b0 * consts->ion_eff_factor / 1.0e-12;
    if(flag_options->USE_HALO_FIELD)
        consts->gamma_prefactor /= RHOcrit * cosmo_params->OMb; //TODO: double-check these unit differences, HaloBox.halo_wsfr vs Nion_General units
    else
        consts->gamma_prefactor /= consts->t_star_sec;
    consts->gamma_prefactor_mini = consts->gamma_prefactor * consts->ion_eff_factor_mini / consts->ion_eff_factor;


}


void allocate_fftw_grids(struct FilteredGrids **fg_struct){
    //NOTE FOR REFACTOR: These don't need to be allocated/filtered if (USE_HALO FIELD && CELL_RECOMB)
    // Also, I don't think deltax_unfiltered_original is useful at all since we have the PerturbedField
    *fg_struct = malloc(sizeof(**fg_struct));

    (*fg_struct)->deltax_unfiltered = (fftwf_complex *) fftwf_malloc(sizeof(fftwf_complex)*HII_KSPACE_NUM_PIXELS);
    (*fg_struct)->deltax_filtered = (fftwf_complex *) fftwf_malloc(sizeof(fftwf_complex)*HII_KSPACE_NUM_PIXELS);

    if(flag_options_global->USE_MINI_HALOS && !flag_options_global->USE_HALO_FIELD){
        (*fg_struct)->prev_deltax_unfiltered = (fftwf_complex *) fftwf_malloc(sizeof(fftwf_complex)*HII_KSPACE_NUM_PIXELS);
        (*fg_struct)->prev_deltax_filtered = (fftwf_complex *) fftwf_malloc(sizeof(fftwf_complex)*HII_KSPACE_NUM_PIXELS);

        (*fg_struct)->log10_Mturnover_unfiltered      = (fftwf_complex *) fftwf_malloc(sizeof(fftwf_complex)*HII_KSPACE_NUM_PIXELS);
        (*fg_struct)->log10_Mturnover_filtered        = (fftwf_complex *) fftwf_malloc(sizeof(fftwf_complex)*HII_KSPACE_NUM_PIXELS);
        (*fg_struct)->log10_Mturnover_MINI_unfiltered = (fftwf_complex *) fftwf_malloc(sizeof(fftwf_complex)*HII_KSPACE_NUM_PIXELS);
        (*fg_struct)->log10_Mturnover_MINI_filtered   = (fftwf_complex *) fftwf_malloc(sizeof(fftwf_complex)*HII_KSPACE_NUM_PIXELS);
    }

    if(flag_options_global->USE_TS_FLUCT){
        (*fg_struct)->xe_unfiltered = (fftwf_complex *) fftwf_malloc(sizeof(fftwf_complex)*HII_KSPACE_NUM_PIXELS);
        (*fg_struct)->xe_filtered = (fftwf_complex *) fftwf_malloc(sizeof(fftwf_complex)*HII_KSPACE_NUM_PIXELS);
    }

    if(flag_options_global->INHOMO_RECO && !flag_options_global->CELL_RECOMB){
        (*fg_struct)->N_rec_unfiltered = (fftwf_complex *) fftwf_malloc(sizeof(fftwf_complex)*HII_KSPACE_NUM_PIXELS); // cumulative number of recombinations
        (*fg_struct)->N_rec_filtered = (fftwf_complex *) fftwf_malloc(sizeof(fftwf_complex)*HII_KSPACE_NUM_PIXELS);
    }

    if(flag_options_global->USE_HALO_FIELD){
        (*fg_struct)->stars_unfiltered = (fftwf_complex *) fftwf_malloc(sizeof(fftwf_complex)*HII_KSPACE_NUM_PIXELS);
        (*fg_struct)->stars_filtered = (fftwf_complex *) fftwf_malloc(sizeof(fftwf_complex)*HII_KSPACE_NUM_PIXELS);

        (*fg_struct)->sfr_unfiltered = (fftwf_complex *) fftwf_malloc(sizeof(fftwf_complex)*HII_KSPACE_NUM_PIXELS);
        (*fg_struct)->sfr_filtered = (fftwf_complex *) fftwf_malloc(sizeof(fftwf_complex)*HII_KSPACE_NUM_PIXELS);
    }
    //TODO: check for null pointers and throw errors
}

void free_fftw_grids(struct FilteredGrids *fg_struct){
    fftwf_free(fg_struct->deltax_unfiltered);
    fftwf_free(fg_struct->deltax_filtered);

    if(flag_options_global->USE_MINI_HALOS && !flag_options_global->USE_HALO_FIELD){
        fftwf_free(fg_struct->prev_deltax_unfiltered);
        fftwf_free(fg_struct->prev_deltax_filtered);

        fftwf_free(fg_struct->log10_Mturnover_unfiltered);
        fftwf_free(fg_struct->log10_Mturnover_filtered);
        fftwf_free(fg_struct->log10_Mturnover_MINI_unfiltered);
        fftwf_free(fg_struct->log10_Mturnover_MINI_filtered);
    }
    if(flag_options_global->USE_TS_FLUCT) {
        fftwf_free(fg_struct->xe_unfiltered);
        fftwf_free(fg_struct->xe_filtered);
    }
    if (flag_options_global->INHOMO_RECO && !flag_options_global->CELL_RECOMB){
        fftwf_free(fg_struct->N_rec_unfiltered);
        fftwf_free(fg_struct->N_rec_filtered);
    }

    if(flag_options_global->USE_HALO_FIELD) {
        fftwf_free(fg_struct->stars_unfiltered);
        fftwf_free(fg_struct->stars_filtered);
        fftwf_free(fg_struct->sfr_unfiltered);
        fftwf_free(fg_struct->sfr_filtered);
    }

    free(fg_struct);
}

//fill fftwf boxes, do the r2c transform and normalise
//TODO: check for some invalid limit values to skip the clipping step
void prepare_box_for_filtering(float *input_box, fftwf_complex *output_c_box, double const_factor, double limit_min, double limit_max){
    int i,j,k;
    unsigned long long int ct;
    double curr_cell;

    LOG_ULTRA_DEBUG("Starting one grid...");
    //NOTE: Meraxes just applies a pointer cast box = (fftwf_complex *) input. Figure out why this works,
    //      They pad the input by a factor of 2 to cover the complex part, but from the type I thought it would be stored [(r,c),(r,c)...]
    //      Not [(r,r,r,r....),(c,c,c....)] so the alignment should be wrong, right?
    #pragma omp parallel for private(i,j,k,curr_cell) num_threads(user_params_global->N_THREADS) collapse(3)
    for(i=0;i<user_params_global->HII_DIM;i++){
        for(j=0;j<user_params_global->HII_DIM;j++){
            for(k=0;k<HII_D_PARA;k++){
                curr_cell = input_box[HII_R_INDEX(i,j,k)] * const_factor;
                //clipping
                *((float *)output_c_box + HII_R_FFT_INDEX(i,j,k)) = fmax(fmin(curr_cell,limit_max),limit_min);
            }
        }
    }
    ////////////////// Transform unfiltered box to k-space to prepare for filtering /////////////////
    dft_r2c_cube(user_params_global->USE_FFTW_WISDOM, user_params_global->HII_DIM, HII_D_PARA, user_params_global->N_THREADS, output_c_box);

    //divide by pixel number in preparation for later inverse transform
    #pragma omp parallel for private(ct) num_threads(user_params_global->N_THREADS)
    for (ct=0; ct<HII_KSPACE_NUM_PIXELS; ct++){
        output_c_box[ct] /= (float)HII_TOT_NUM_PIXELS;
    }
}

//Making a dummy previous box which has the required fields for the first snapshot:
void setup_first_z_prevbox(IonizedBox *previous_ionize_box, PerturbedField *previous_perturb, int n_radii){
    LOG_DEBUG("first redshift, do some initialization");
    int i,j,k;
    unsigned long long int ct;

    //z_re_box is used in all cases
    previous_ionize_box->z_re_box    = (float *) calloc(HII_TOT_NUM_PIXELS, sizeof(float));
    #pragma omp parallel shared(previous_ionize_box) private(i,j,k) num_threads(user_params_global->N_THREADS)
    {
        #pragma omp for
        for (i=0; i<user_params_global->HII_DIM; i++){
            for (j=0; j<user_params_global->HII_DIM; j++){
                for (k=0; k<HII_D_PARA; k++){
                    previous_ionize_box->z_re_box[HII_R_INDEX(i, j, k)] = -1.0;
                }
            }
        }
    }

    //dNrec is used for INHOMO_RECO
    if(flag_options_global->INHOMO_RECO)
        previous_ionize_box->dNrec_box = (float *) calloc(HII_TOT_NUM_PIXELS, sizeof(float));

    //previous Gamma12 is used for reionisation feedback when USE_MINI_HALOS
    //previous delta and Fcoll are used for the trapezoidal integral when USE_MINI_HALOS
    if(flag_options_global->USE_MINI_HALOS){
        previous_ionize_box->Gamma12_box = (float *) calloc(HII_TOT_NUM_PIXELS, sizeof(float));
        previous_ionize_box->Fcoll       = (float *) calloc(HII_TOT_NUM_PIXELS*n_radii, sizeof(float));
        previous_ionize_box->Fcoll_MINI  = (float *) calloc(HII_TOT_NUM_PIXELS*n_radii, sizeof(float));
        previous_ionize_box->mean_f_coll = 0.0;
        previous_ionize_box->mean_f_coll_MINI = 0.0;

        previous_perturb->density = (float *) calloc(HII_TOT_NUM_PIXELS*n_radii, sizeof(float));
        #pragma omp parallel private(ct) num_threads(user_params_global->N_THREADS)
        {
            #pragma omp for
            for(ct=0;ct<HII_TOT_NUM_PIXELS;ct++){
                previous_perturb->density[ct] = -1.5;
            }
        }
    }
}

void calculate_mcrit_boxes(IonizedBox *prev_ionbox, TsBox *spin_temp, InitialConditions *ini_boxes, struct IonBoxConstants *consts,
                            fftwf_complex *log10_mcrit_acg, fftwf_complex *log10_mcrit_mcg, double *avg_mturn_acg, double *avg_mturn_mcg){
    double ave_log10_Mturnover = 0.;
    double ave_log10_Mturnover_MINI = 0.;

    #pragma omp parallel num_threads(user_params_global->N_THREADS)
    {
        int x,y,z;
        double Mcrit_RE, Mcrit_LW;
        double curr_Mt, curr_Mt_MINI;
        double curr_vcb = consts->vcb_norel;
    #pragma omp for reduction(+:ave_log10_Mturnover,ave_log10_Mturnover_MINI)
        for (x=0; x<user_params_global->HII_DIM; x++){
            for (y=0; y<user_params_global->HII_DIM; y++){
                for (z=0; z<HII_D_PARA; z++){
                    Mcrit_RE = reionization_feedback(consts->redshift, prev_ionbox->Gamma12_box[HII_R_INDEX(x, y, z)], prev_ionbox->z_re_box[HII_R_INDEX(x, y, z)]);

                    if(user_params_global->USE_RELATIVE_VELOCITIES && !flag_options_global->FIX_VCB_AVG)
                        curr_vcb = ini_boxes->lowres_vcb[HII_R_INDEX(x,y,z)];

                    Mcrit_LW = lyman_werner_threshold(consts->redshift, spin_temp->J_21_LW_box[HII_R_INDEX(x, y, z)], curr_vcb, astro_params_global);
                    if(Mcrit_LW != Mcrit_LW || Mcrit_LW == 0){
                        LOG_ERROR("Mcrit error %d %d %d: M %.2e z %.2f J %.2e v %.2e",x,y,z,Mcrit_LW,consts->redshift,
                                        spin_temp->J_21_LW_box[HII_R_INDEX(x, y, z)],curr_vcb);
                        Throw(ValueError);
                    }

                    //JBM: this only accounts for effect 3 (largest on minihaloes). Effects 1 and 2 affect both minihaloes (MCGs) and regular ACGs, but they're smaller ~10%. See Sec 2 of Muñoz+21 (2110.13919)
                    curr_Mt              = log10(fmax(Mcrit_RE,consts->mturn_a_nofb));
                    curr_Mt_MINI         = log10(fmax(Mcrit_RE,fmax(Mcrit_LW,consts->mturn_m_nofb)));

                    //To avoid allocating another box we directly assign turnover masses to the fftw grid
                    *((float *)log10_mcrit_acg      + HII_R_FFT_INDEX(x,y,z)) = curr_Mt;
                    *((float *)log10_mcrit_mcg + HII_R_FFT_INDEX(x,y,z)) = curr_Mt_MINI;

                    ave_log10_Mturnover      += curr_Mt;
                    ave_log10_Mturnover_MINI += curr_Mt_MINI;
                }
            }
        }
    }
    *avg_mturn_acg = ave_log10_Mturnover/HII_TOT_NUM_PIXELS;
    *avg_mturn_mcg = ave_log10_Mturnover_MINI/HII_TOT_NUM_PIXELS;
}

// Determine the normalisation for the excursion set algorithm
// When USE_MINI_HALOS==True, we do a trapezoidal integration, where we take
// F_coll = f(z_current,Mturn_current) - f(z_previous,Mturn_current) + f(z_previous,Mturn_previous)
// all mturns are average log10 over the
// the `limit` outputs are set to the total value at the maximum redshift and current turnover, these form a
// lower limit on any grid cell
void set_mean_fcoll(struct IonBoxConstants *c, IonizedBox *prev_box, IonizedBox *curr_box, double mturn_acg, double mturn_mcg, double *f_limit_acg, double *f_limit_mcg){
    double f_coll_curr, f_coll_prev, f_coll_curr_mini, f_coll_prev_mini;
    if(flag_options_global->USE_MASS_DEPENDENT_ZETA){
        f_coll_curr = Nion_General(c->redshift,c->lnMmin,c->lnMmax_gl,mturn_acg,c->alpha_star,c->alpha_esc,
                                    c->fstar_10,c->fesc_10,c->Mlim_Fstar,c->Mlim_Fesc);
        *f_limit_acg = Nion_General(global_params.Z_HEAT_MAX,c->lnMmin,c->lnMmax_gl,mturn_acg,c->alpha_star,c->alpha_esc,
                                    c->fstar_10,c->fesc_10,c->Mlim_Fstar,c->Mlim_Fesc);

        if (flag_options_global->USE_MINI_HALOS){
            if (prev_box->mean_f_coll * c->ion_eff_factor_gl < 1e-4){
                //we don't have enough ionising radiation in the previous snapshot, just take the current value
                curr_box->mean_f_coll = f_coll_curr;
            }
            else{
                f_coll_prev = Nion_General(c->prev_redshift,c->lnMmin,c->lnMmax_gl,mturn_acg,c->alpha_star,c->alpha_esc,
                                                c->fstar_10,c->fesc_10,c->Mlim_Fstar,c->Mlim_Fesc);
                curr_box->mean_f_coll = prev_box->mean_f_coll + f_coll_curr - f_coll_prev;
            }
            f_coll_curr_mini = Nion_General_MINI(c->redshift,c->lnMmin,c->lnMmax_gl,mturn_mcg,mturn_acg,c->alpha_star_mini,
                                                          c->alpha_esc,c->fstar_7,c->fesc_7,c->Mlim_Fstar_mini,c->Mlim_Fesc_mini);
            if (prev_box->mean_f_coll_MINI * c->ion_eff_factor_gl < 1e-4){
                curr_box->mean_f_coll_MINI = f_coll_curr_mini;
            }
            else{
                f_coll_prev_mini = Nion_General_MINI(c->prev_redshift,c->lnMmin,c->lnMmax_gl,mturn_mcg,mturn_acg,c->alpha_star_mini,
                                                          c->alpha_esc,c->fstar_7,c->fesc_7,c->Mlim_Fstar_mini,c->Mlim_Fesc_mini);
                curr_box->mean_f_coll_MINI = prev_box->mean_f_coll_MINI + f_coll_curr_mini - f_coll_prev_mini;
            }
            *f_limit_mcg = Nion_General_MINI(global_params.Z_HEAT_MAX,c->lnMmin,c->lnMmax_gl,mturn_mcg,mturn_acg,c->alpha_star_mini,
                                                          c->alpha_esc,c->fstar_7,c->fesc_7,
                                                          c->Mlim_Fstar_mini,c->Mlim_Fesc_mini);
        }
        else{
            curr_box->mean_f_coll = f_coll_curr;
            curr_box->mean_f_coll_MINI = 0.;
        }
    }
    else {
        curr_box->mean_f_coll = Fcoll_General(c->redshift, c->lnMmin, c->lnMmax_gl);
        *f_limit_acg = Fcoll_General(global_params.Z_HEAT_MAX, c->lnMmin, c->lnMmax_gl); //JD: the old parametrisation didn't have this limit before
    }

    if(isfinite(curr_box->mean_f_coll)==0){
        LOG_ERROR("Mean collapse fraction is either infinite or NaN!");
        Throw(InfinityorNaNError);
    }
    LOG_SUPER_DEBUG("excursion set normalisation, mean_f_coll: %e", curr_box->mean_f_coll);

    if (flag_options_global->USE_MINI_HALOS){
        if(isfinite(curr_box->mean_f_coll_MINI)==0){
            LOG_ERROR("Mean collapse fraction of MINI is either infinite or NaN!");
            Throw(InfinityorNaNError);
        }
        LOG_SUPER_DEBUG("excursion set normalisation, mean_f_coll_MINI: %e", curr_box->mean_f_coll_MINI);
    }
}

double set_fully_neutral_box(IonizedBox *box, TsBox *spin_temp, PerturbedField *perturbed_field, struct IonBoxConstants *consts){
    double global_xH;
    unsigned long long int ct;
    if(flag_options_global->USE_TS_FLUCT) {
        #pragma omp parallel private(ct) num_threads(user_params_global->N_THREADS)
        {
            #pragma omp for reduction(+:global_xH)
            for (ct=0; ct<HII_TOT_NUM_PIXELS; ct++){
                box->xH_box[ct] = 1.-spin_temp->x_e_box[ct]; // convert from x_e to xH
                global_xH += box->xH_box[ct];
                box->temp_kinetic_all_gas[ct] = spin_temp->Tk_box[ct];
            }
        }
        global_xH /= (double)HII_TOT_NUM_PIXELS;
    }
    else {
        global_xH = 1. - xion_RECFAST(consts->redshift, 0);
        #pragma omp parallel private(ct) num_threads(user_params_global->N_THREADS)
        {
            #pragma omp for
            for (ct=0; ct<HII_TOT_NUM_PIXELS; ct++){
                box->xH_box[ct] = global_xH;
                box->temp_kinetic_all_gas[ct] = consts->TK_nofluct * (1.0 + consts->adia_TK_term * perturbed_field->density[ct]); // Is perturbed_field defined already here? we need it for cT. I'm also assuming we don't need to multiply by other z here.
            }
        }
    }
    return global_xH;
}

//TODO: SPEED TEST THE FOLLOWING ORDERS:
// (copy,copy,copy....) (filter,filter,filter,...) (transform,transform,...)
// (copy,filter,transform), (copy,filter,transform), (copy,filter,transform)...
//  if the first is faster, consider reordering prepare_filter_grids() in the same way
//  if the second is faster, make a function to do this for one grid
void copy_filter_transform(struct FilteredGrids *fg_struct, struct IonBoxConstants *consts, struct RadiusSpec rspec){
    memcpy(fg_struct->deltax_filtered, fg_struct->deltax_unfiltered, sizeof(fftwf_complex)*HII_KSPACE_NUM_PIXELS);
    if(flag_options_global->USE_TS_FLUCT){
        memcpy(fg_struct->xe_filtered, fg_struct->xe_unfiltered, sizeof(fftwf_complex) * HII_KSPACE_NUM_PIXELS);
    }
    if(consts->filter_recombinations){
        memcpy(fg_struct->N_rec_filtered, fg_struct->N_rec_unfiltered, sizeof(fftwf_complex) * HII_KSPACE_NUM_PIXELS);
    }
    if(flag_options_global->USE_HALO_FIELD){
        memcpy(fg_struct->stars_filtered, fg_struct->stars_unfiltered, sizeof(fftwf_complex) * HII_KSPACE_NUM_PIXELS);
        memcpy(fg_struct->sfr_filtered, fg_struct->sfr_unfiltered, sizeof(fftwf_complex) * HII_KSPACE_NUM_PIXELS);
    }
    else{
        if(flag_options_global->USE_MINI_HALOS){
            memcpy(fg_struct->prev_deltax_filtered, fg_struct->prev_deltax_unfiltered, sizeof(fftwf_complex)*HII_KSPACE_NUM_PIXELS);
            memcpy(fg_struct->log10_Mturnover_MINI_filtered, fg_struct->log10_Mturnover_MINI_unfiltered, sizeof(fftwf_complex)*HII_KSPACE_NUM_PIXELS);
            memcpy(fg_struct->log10_Mturnover_filtered, fg_struct->log10_Mturnover_unfiltered, sizeof(fftwf_complex)*HII_KSPACE_NUM_PIXELS);
        }
    }

    if(rspec.R_index > 0){
        double R = rspec.R;
        filter_box(fg_struct->deltax_filtered, 1, global_params.HII_FILTER, R, 0.);
        if (flag_options_global->USE_TS_FLUCT) {
            filter_box(fg_struct->xe_filtered, 1, global_params.HII_FILTER, R, 0.);
        }
        if (consts->filter_recombinations) {
            filter_box(fg_struct->N_rec_filtered, 1, global_params.HII_FILTER, R, 0.);
        }
        if (flag_options_global->USE_HALO_FIELD) {
                int filter_hf = flag_options_global->USE_EXP_FILTER ? 3 : global_params.HII_FILTER;
                filter_box(fg_struct->stars_filtered, 1, filter_hf, R, consts->mfp_meandens);
                filter_box(fg_struct->sfr_filtered, 1, filter_hf, R, consts->mfp_meandens);
        }
        else{
            if(flag_options_global->USE_MINI_HALOS){
                filter_box(fg_struct->prev_deltax_filtered, 1, global_params.HII_FILTER, R, 0.);
                filter_box(fg_struct->log10_Mturnover_MINI_filtered, 1, global_params.HII_FILTER, R, 0.);
                filter_box(fg_struct->log10_Mturnover_filtered, 1, global_params.HII_FILTER, R, 0.);
            }
        }
    }

    // Perform FFTs
    dft_c2r_cube(user_params_global->USE_FFTW_WISDOM, user_params_global->HII_DIM, HII_D_PARA, user_params_global->N_THREADS, fg_struct->deltax_filtered);
    if (flag_options_global->USE_HALO_FIELD) {
        dft_c2r_cube(user_params_global->USE_FFTW_WISDOM, user_params_global->HII_DIM, HII_D_PARA, user_params_global->N_THREADS, fg_struct->stars_filtered);
        dft_c2r_cube(user_params_global->USE_FFTW_WISDOM, user_params_global->HII_DIM, HII_D_PARA, user_params_global->N_THREADS, fg_struct->sfr_filtered);
    }
    else{
        if(flag_options_global->USE_MINI_HALOS){
            dft_c2r_cube(user_params_global->USE_FFTW_WISDOM, user_params_global->HII_DIM, HII_D_PARA, user_params_global->N_THREADS, fg_struct->prev_deltax_filtered);
            dft_c2r_cube(user_params_global->USE_FFTW_WISDOM, user_params_global->HII_DIM, HII_D_PARA, user_params_global->N_THREADS, fg_struct->log10_Mturnover_MINI_filtered);
            dft_c2r_cube(user_params_global->USE_FFTW_WISDOM, user_params_global->HII_DIM, HII_D_PARA, user_params_global->N_THREADS, fg_struct->log10_Mturnover_filtered);
        }
    }
    if (flag_options_global->USE_TS_FLUCT) {
        dft_c2r_cube(user_params_global->USE_FFTW_WISDOM, user_params_global->HII_DIM, HII_D_PARA, user_params_global->N_THREADS, fg_struct->xe_filtered);
    }
    if (consts->filter_recombinations) {
        dft_c2r_cube(user_params_global->USE_FFTW_WISDOM, user_params_global->HII_DIM, HII_D_PARA, user_params_global->N_THREADS, fg_struct->N_rec_filtered);
    }
}

//After filtering the grids, we need to clip them to physical values and take the extrema for some interpolation tables
void clip_and_get_extrema(fftwf_complex * grid, double lower_limit, double upper_limit, double *grid_min, double *grid_max){
    double min_buf,max_buf;
    //setting the extrema to the first cell to guarantee it's in the range
    min_buf = *((float *) grid + HII_R_FFT_INDEX(0, 0, 0));
    max_buf = *((float *) grid + HII_R_FFT_INDEX(0, 0, 0));
    #pragma omp parallel num_threads(user_params_global->N_THREADS)
    {
        int x,y,z;
        float curr;
        #pragma omp for reduction(max:max_buf) reduction(min:min_buf)
        for (x = 0; x < user_params_global->HII_DIM; x++) {
            for (y = 0; y < user_params_global->HII_DIM; y++) {
                for (z = 0; z < HII_D_PARA; z++) {
                    // delta cannot be less than -1
                    curr = *((float *) grid + HII_R_FFT_INDEX(x, y, z));
                    *((float *) grid + HII_R_FFT_INDEX(x, y, z)) = fmaxf(fmin(curr,upper_limit),lower_limit);

                    if (curr < min_buf)
                        min_buf = curr;
                    if (curr > max_buf)
                        max_buf = curr;
                }
            }
        }
    }
    *grid_min = min_buf;
    *grid_max = max_buf;
}

//TODO: maybe put the grid clipping outside this function
void setup_integration_tables(struct FilteredGrids *fg_struct, struct IonBoxConstants *consts, struct RadiusSpec rspec, bool need_prev){
    double min_density, max_density, prev_min_density, prev_max_density;
    double log10Mturn_min, log10Mturn_max, log10Mturn_min_MINI, log10Mturn_max_MINI;
    if (flag_options_global->USE_MASS_DEPENDENT_ZETA){
        //TODO: instead of putting a random upper limit, put a proper flag for switching of one/both sides of the clipping
        clip_and_get_extrema(fg_struct->deltax_filtered,-1,1e6,&min_density,&max_density);
        if (flag_options_global->USE_MINI_HALOS){
            // do the same for prev
            clip_and_get_extrema(fg_struct->prev_deltax_filtered,-1,1e6,&prev_min_density,&prev_max_density);
            clip_and_get_extrema(fg_struct->log10_Mturnover_filtered,0.,LOG10_MTURN_MAX,&log10Mturn_min,&log10Mturn_max);
            clip_and_get_extrema(fg_struct->log10_Mturnover_MINI_filtered,0.,LOG10_MTURN_MAX,&log10Mturn_min_MINI,&log10Mturn_max_MINI);
        }

        LOG_ULTRA_DEBUG("Tb limits d (%.2e,%.2e), m (%.2e,%.2e) t (%.2e,%.2e) tm (%.2e,%.2e)",
                        min_density,max_density,consts->M_min,rspec.M_max_R,log10Mturn_min,log10Mturn_max,
                        log10Mturn_min_MINI,log10Mturn_max_MINI);
        if(user_params_global->INTEGRATION_METHOD_ATOMIC == 1 || (flag_options_global->USE_MINI_HALOS && user_params_global->INTEGRATION_METHOD_MINI == 1))
            initialise_GL(consts->lnMmin, rspec.ln_M_max_R);
        if(user_params_global->USE_INTERPOLATION_TABLES){
            //Buffers to avoid both zero bin widths and max cell segfault in 2D interptables
            min_density -= 0.001;
            max_density += 0.001;
            prev_min_density -= 0.001;
            prev_max_density += 0.001;
            log10Mturn_min = log10Mturn_min * 0.99;
            log10Mturn_max = log10Mturn_max * 1.01;
            log10Mturn_min_MINI = log10Mturn_min_MINI * 0.99;
            log10Mturn_max_MINI = log10Mturn_max_MINI * 1.01;

            //current redshift tables (automatically handles minihalo case)
            initialise_Nion_Conditional_spline(consts->redshift,consts->mturn_a_nofb,min_density,max_density,consts->M_min,rspec.M_max_R,rspec.M_max_R,
                                    log10Mturn_min,log10Mturn_max,log10Mturn_min_MINI,log10Mturn_max_MINI,
                                    consts->alpha_star, consts->alpha_star_mini,
                                    consts->alpha_esc,consts->fstar_10,
                                    consts->fesc_10,consts->Mlim_Fstar,consts->Mlim_Fesc,consts->fstar_7,
                                    consts->fesc_7,consts->Mlim_Fstar_mini,consts->Mlim_Fesc_mini,
                                    user_params_global->INTEGRATION_METHOD_ATOMIC, user_params_global->INTEGRATION_METHOD_MINI,
                                    flag_options_global->USE_MINI_HALOS,false);

            //previous redshift tables if needed
            if(need_prev){
                initialise_Nion_Conditional_spline(consts->prev_redshift,consts->mturn_a_nofb,prev_min_density,prev_max_density,consts->M_min,rspec.M_max_R,rspec.M_max_R,
                                        log10Mturn_min,log10Mturn_max,log10Mturn_min_MINI,log10Mturn_max_MINI,
                                        consts->alpha_star, consts->alpha_star_mini,
                                        consts->alpha_esc,consts->fstar_10,
                                        consts->fesc_10,consts->Mlim_Fstar,consts->Mlim_Fesc,consts->fstar_7,
                                        consts->fstar_7,consts->Mlim_Fstar_mini,consts->Mlim_Fesc_mini,
                                        user_params_global->INTEGRATION_METHOD_ATOMIC, user_params_global->INTEGRATION_METHOD_MINI,
                                        flag_options_global->USE_MINI_HALOS,true);
            }
        }
    }
    else {
        //This was previously one table for all R, which can be done with the EPS mass function (and some others)
        //TODO: I don't expect this to be a bottleneck, but we can look into re-making the ERFC table if needed
        initialise_FgtrM_delta_table(min_density, max_density, consts->redshift, consts->growth_factor, consts->sigma_minmass, rspec.sigma_maxmass);
    }
}

//TODO: We should speed test different configurations, separating grids, parallel sections etc.
//  See the note above copy_filter_transform() for the general idea
//  If we separate by grid we can reuse the clipping function above
void calculate_fcoll_grid(IonizedBox *box, IonizedBox *previous_ionize_box, struct FilteredGrids *fg_struct, struct IonBoxConstants *consts,
                             struct RadiusSpec *rspec){
    double f_coll_total,f_coll_MINI_total;
    //TODO: make proper error tracking through the parallel region
    bool error_flag;

    int fc_r_idx;
    fc_r_idx = flag_options_global->USE_MINI_HALOS ? rspec->R_index : 0;
    #pragma omp parallel num_threads(user_params_global->N_THREADS)
    {
        int x,y,z;
        double curr_dens;
        double Splined_Fcoll,Splined_Fcoll_MINI;
        double log10_Mturnover,log10_Mturnover_MINI;
        double prev_dens,prev_Splined_Fcoll,prev_Splined_Fcoll_MINI;
        log10_Mturnover = log10(consts->mturn_a_nofb); //is only overwritten with minihalos
        #pragma omp for reduction(+:f_coll_total,f_coll_MINI_total)
        for (x = 0; x < user_params_global->HII_DIM; x++) {
            for (y = 0; y < user_params_global->HII_DIM; y++) {
                for (z = 0; z < HII_D_PARA; z++) {
                    //clip the filtered grids to physical values
                    // delta cannot be less than -1
                    *((float *) fg_struct->deltax_filtered + HII_R_FFT_INDEX(x, y, z)) = fmaxf(
                                        *((float *) fg_struct->deltax_filtered + HII_R_FFT_INDEX(x, y, z)), -1. + FRACT_FLOAT_ERR);

                    // <N_rec> cannot be less than zero
                    if (consts->filter_recombinations) {
                        *((float *) fg_struct->N_rec_filtered + HII_R_FFT_INDEX(x, y, z)) = \
                            fmaxf(*((float *) fg_struct->N_rec_filtered + HII_R_FFT_INDEX(x, y, z)), 0.0);
                    }

                    // x_e has to be between zero and unity
                    if (flag_options_global->USE_TS_FLUCT) {
                        *((float *) fg_struct->xe_filtered + HII_R_FFT_INDEX(x, y, z)) = \
                            fmaxf(*((float *) fg_struct->xe_filtered + HII_R_FFT_INDEX(x, y, z)), 0.);
                        *((float *) fg_struct->xe_filtered + HII_R_FFT_INDEX(x, y, z)) = \
                            fminf(*((float *) fg_struct->xe_filtered + HII_R_FFT_INDEX(x, y, z)), 0.999);
                    }

                    // stellar mass & sfr cannot be less than zero
                    if(flag_options_global->USE_HALO_FIELD) {
                        *((float *)fg_struct->stars_filtered + HII_R_FFT_INDEX(x,y,z)) = fmaxf(
                                *((float *)fg_struct->stars_filtered + HII_R_FFT_INDEX(x,y,z)) , 0.0);
                        *((float *)fg_struct->sfr_filtered + HII_R_FFT_INDEX(x,y,z)) = fmaxf(
                                *((float *)fg_struct->sfr_filtered + HII_R_FFT_INDEX(x,y,z)) , 0.0);

                        //Ionising photon output
                        Splined_Fcoll = *((float *)fg_struct->stars_filtered + HII_R_FFT_INDEX(x,y,z));
                        //Minihalos are taken care of already
                        Splined_Fcoll_MINI = 0.;
                        //The smoothing done with minihalos corrects for sudden changes in M_crit
                        //Nion_smoothed(z,Mcrit) = Nion(z,Mcrit) + (Nion(z_prev,Mcrit_prev) - Nion(z_prev,Mcrit))
                        prev_Splined_Fcoll = 0.;
                        prev_Splined_Fcoll_MINI = 0.;
                    }
                    else {
                        curr_dens = *((float *) fg_struct->deltax_filtered + HII_R_FFT_INDEX(x, y, z));
                        if (flag_options_global->USE_MASS_DEPENDENT_ZETA){
                            if (flag_options_global->USE_MINI_HALOS){
                                log10_Mturnover = *((float *)fg_struct->log10_Mturnover_filtered + HII_R_FFT_INDEX(x,y,z));
                                log10_Mturnover_MINI = *((float *)fg_struct->log10_Mturnover_MINI_filtered + HII_R_FFT_INDEX(x,y,z));

                                Splined_Fcoll_MINI = EvaluateNion_Conditional_MINI(curr_dens,log10_Mturnover_MINI,consts->growth_factor,consts->M_min,
                                                                                    rspec->M_max_R,rspec->M_max_R,rspec->sigma_maxmass,consts->mturn_a_nofb,
                                                                                    consts->Mlim_Fstar_mini,consts->Mlim_Fesc,false);


                                if (previous_ionize_box->mean_f_coll_MINI * consts->ion_eff_factor_mini_gl +
                                        previous_ionize_box->mean_f_coll * consts->ion_eff_factor_gl > 1e-4){
                                    prev_dens = *((float *)fg_struct->prev_deltax_filtered + HII_R_FFT_INDEX(x,y,z));
                                    prev_Splined_Fcoll = EvaluateNion_Conditional(prev_dens,log10_Mturnover,consts->prev_growth_factor,
                                                                                        consts->M_min,rspec->M_max_R,rspec->M_max_R,
                                                                                        rspec->sigma_maxmass,consts->Mlim_Fstar,consts->Mlim_Fesc,true);
                                    prev_Splined_Fcoll_MINI = EvaluateNion_Conditional_MINI(prev_dens,log10_Mturnover_MINI,consts->prev_growth_factor,consts->M_min,
                                                                                    rspec->M_max_R,rspec->M_max_R,rspec->sigma_maxmass,consts->mturn_a_nofb,
                                                                                    consts->Mlim_Fstar_mini,consts->Mlim_Fesc_mini,true);
                                }
                                else{
                                    prev_Splined_Fcoll = 0.;
                                    prev_Splined_Fcoll_MINI = 0.;
                                }
                            }
                            Splined_Fcoll = EvaluateNion_Conditional(curr_dens,log10_Mturnover,consts->growth_factor,
                                                                    consts->M_min,rspec->M_max_R,rspec->M_max_R,
                                                                    rspec->sigma_maxmass,consts->Mlim_Fstar,consts->Mlim_Fesc,true);
                        }
                        else{
                            Splined_Fcoll = EvaluateFcoll_delta(curr_dens,consts->growth_factor,consts->sigma_minmass,rspec->sigma_maxmass);
                        }
                    }
                    // save the value of the collasped fraction into the Fcoll array
                    //TODO: each of these grids are clipped before filtering, after filtering,
                    //  after the Fcoll integration, and after trapezoidal integration here
                    //  we should figure which of these clips are necessary/useful
                    if (flag_options_global->USE_MINI_HALOS && !flag_options_global->USE_HALO_FIELD){
                        if (Splined_Fcoll > 1.) Splined_Fcoll = 1.;
                        if (Splined_Fcoll < 0.) Splined_Fcoll = 1e-40;
                        if (prev_Splined_Fcoll > 1.) prev_Splined_Fcoll = 1.;
                        if (prev_Splined_Fcoll < 0.) prev_Splined_Fcoll = 1e-40;
                        box->Fcoll[fc_r_idx * HII_TOT_NUM_PIXELS + HII_R_INDEX(x,y,z)] = \
                                previous_ionize_box->Fcoll[fc_r_idx * HII_TOT_NUM_PIXELS + HII_R_INDEX(x,y,z)] + Splined_Fcoll - prev_Splined_Fcoll;

                        if (box->Fcoll[fc_r_idx * HII_TOT_NUM_PIXELS + HII_R_INDEX(x,y,z)] > 1.) box->Fcoll[fc_r_idx * HII_TOT_NUM_PIXELS + HII_R_INDEX(x,y,z)] = 1.;
                        //if (box->Fcoll[counter * HII_TOT_NUM_PIXELS + HII_R_INDEX(x,y,z)] <0.) box->Fcoll[counter * HII_TOT_NUM_PIXELS + HII_R_INDEX(x,y,z)] = 1e-40;
                        //if (box->Fcoll[counter * HII_TOT_NUM_PIXELS + HII_R_INDEX(x,y,z)] < previous_ionize_box->Fcoll[counter * HII_TOT_NUM_PIXELS + HII_R_INDEX(x,y,z)])
                        //    box->Fcoll[counter * HII_TOT_NUM_PIXELS + HII_R_INDEX(x,y,z)] = previous_ionize_box->Fcoll[counter * HII_TOT_NUM_PIXELS + HII_R_INDEX(x,y,z)];
                        f_coll_total += box->Fcoll[fc_r_idx * HII_TOT_NUM_PIXELS + HII_R_INDEX(x,y,z)];
                        if(isfinite(f_coll_total)==0) {
                            LOG_ERROR("f_coll is either infinite or NaN!(%d,%d,%d)%g,%g,%g,%g,%g,%g,%g,%g,%g",\
                                    x,y,z,curr_dens,prev_dens,previous_ionize_box->Fcoll[fc_r_idx * HII_TOT_NUM_PIXELS + HII_R_INDEX(x,y,z)],\
                                    Splined_Fcoll, prev_Splined_Fcoll, curr_dens, prev_dens, log10_Mturnover,
                                    *((float *)fg_struct->log10_Mturnover_filtered + HII_R_FFT_INDEX(x,y,z)));
                            Throw(InfinityorNaNError);
                        }

                        if (Splined_Fcoll_MINI > 1.) Splined_Fcoll_MINI = 1.;
                        if (Splined_Fcoll_MINI < 0.) Splined_Fcoll_MINI = 1e-40;
                        if (prev_Splined_Fcoll_MINI > 1.) prev_Splined_Fcoll_MINI = 1.;
                        if (prev_Splined_Fcoll_MINI < 0.) prev_Splined_Fcoll_MINI = 1e-40;
                        box->Fcoll_MINI[fc_r_idx * HII_TOT_NUM_PIXELS + HII_R_INDEX(x,y,z)] = \
                                    previous_ionize_box->Fcoll_MINI[fc_r_idx * HII_TOT_NUM_PIXELS + HII_R_INDEX(x,y,z)] + Splined_Fcoll_MINI - prev_Splined_Fcoll_MINI;

                        if (box->Fcoll_MINI[fc_r_idx * HII_TOT_NUM_PIXELS + HII_R_INDEX(x,y,z)] >1.) box->Fcoll_MINI[fc_r_idx * HII_TOT_NUM_PIXELS + HII_R_INDEX(x,y,z)] = 1.;
                        //if (box->Fcoll_MINI[counter * HII_TOT_NUM_PIXELS + HII_R_INDEX(x,y,z)] <0.) box->Fcoll_MINI[counter * HII_TOT_NUM_PIXELS + HII_R_INDEX(x,y,z)] = 1e-40;
                        //if (box->Fcoll_MINI[counter * HII_TOT_NUM_PIXELS + HII_R_INDEX(x,y,z)] < previous_ionize_box->Fcoll_MINI[counter * HII_TOT_NUM_PIXELS + HII_R_INDEX(x,y,z)])
                        //    box->Fcoll_MINI[counter * HII_TOT_NUM_PIXELS + HII_R_INDEX(x,y,z)] = previous_ionize_box->Fcoll_MINI[counter * HII_TOT_NUM_PIXELS + HII_R_INDEX(x,y,z)];
                        f_coll_MINI_total += box->Fcoll_MINI[fc_r_idx * HII_TOT_NUM_PIXELS + HII_R_INDEX(x,y,z)];
                        if(isfinite(f_coll_MINI_total)==0) {
                            LOG_ERROR("f_coll_MINI is either infinite or NaN!(%d,%d,%d)%g,%g,%g,%g,%g,%g,%g",\
                                        x,y,z,curr_dens, prev_dens, previous_ionize_box->Fcoll_MINI[fc_r_idx * HII_TOT_NUM_PIXELS + HII_R_INDEX(x,y,z)],\
                                        Splined_Fcoll_MINI, prev_Splined_Fcoll_MINI, log10_Mturnover_MINI,\
                                        *((float *)fg_struct->log10_Mturnover_MINI_filtered + HII_R_FFT_INDEX(x,y,z)));
                            LOG_DEBUG("%g,%g",previous_ionize_box->Fcoll[fc_r_idx * HII_TOT_NUM_PIXELS + HII_R_INDEX(x,y,z)],\
                                        previous_ionize_box->Fcoll_MINI[fc_r_idx * HII_TOT_NUM_PIXELS + HII_R_INDEX(x,y,z)]);
                            Throw(InfinityorNaNError);
                        }
                    }
                    else{
                        box->Fcoll[fc_r_idx * HII_TOT_NUM_PIXELS + HII_R_INDEX(x,y,z)] = Splined_Fcoll;
                        f_coll_total += Splined_Fcoll;
                    }
                }
            }
        }
    } //  end loop through Fcoll box
    rspec->f_coll_grid_mean = f_coll_total / HII_TOT_NUM_PIXELS;
    rspec->f_coll_grid_mean_MINI = f_coll_MINI_total / HII_TOT_NUM_PIXELS;
}

int setup_radii(struct RadiusSpec **rspec_array, struct IonBoxConstants *consts){
    double maximum_radius = fmin(astro_params_global->R_BUBBLE_MAX,
                                    L_FACTOR*user_params_global->BOX_LEN);

    double cell_length_factor = L_FACTOR;
    //TODO: figure out why this is used in such a specific case
    if(flag_options_global->USE_HALO_FIELD && (global_params.FIND_BUBBLE_ALGORITHM == 2) && (consts->pixel_length < 1))
        cell_length_factor = 1.;

    double minimum_radius = fmax(global_params.R_BUBBLE_MIN,cell_length_factor*consts->pixel_length);

    //minimum number such that min_R*delta^N > max_R
    int n_radii = ceil(log(maximum_radius/minimum_radius)/log(global_params.DELTA_R_HII_FACTOR)) + 1;
    *rspec_array = malloc(sizeof(**rspec_array)*n_radii);

    //We want the following behaviour from our radius Values:
    //  The smallest radius is the cell size or global min
    //  The largest radius is the box size of global max
    //  Each step is set by multiplying by the same factor
    //This is not possible for most sets of these three parameters
    //  so we let the first step (largest -> second largest) be different,
    //  finding the other radii by stepping *up* from the minimum
    int i;
    for(i=0;i<n_radii;i++){
        (*rspec_array)[i].R_index = i;
        (*rspec_array)[i].R = minimum_radius * pow(global_params.DELTA_R_HII_FACTOR,i);
        //TODO: is this necessary? prevents the last step being small, but could hide some
        // unexpected behaviour/bugs if it finishes earlier than n_radii-2
        if((*rspec_array)[i].R > maximum_radius - FRACT_FLOAT_ERR){
            (*rspec_array)[i].R = maximum_radius;
            n_radii = i+1; //also ends the loop after this iteration
        }
        (*rspec_array)[i].M_max_R = RtoM((*rspec_array)[i].R);
        (*rspec_array)[i].ln_M_max_R = log((*rspec_array)[i].M_max_R);
        (*rspec_array)[i].sigma_maxmass = sigma_z0((*rspec_array)[i].M_max_R);
    }
    LOG_DEBUG("set max radius: %f", (*rspec_array)[n_radii-1].R);
    return n_radii;
}

void find_ionised_regions(IonizedBox *box, IonizedBox *previous_ionize_box, PerturbedField *perturbed_field, TsBox *spin_temp, struct RadiusSpec rspec,
                            struct IonBoxConstants *consts, struct FilteredGrids *fg_struct, gsl_rng **cell_rng,
                            double f_limit_acg, double f_limit_mcg){
    double mean_fix_term_acg = 1.;
    double mean_fix_term_mcg = 1.;
    int fc_r_idx;
    fc_r_idx = flag_options_global->USE_MINI_HALOS ? rspec.R_index : 0;
    if(consts->fix_mean){
        mean_fix_term_acg = box->mean_f_coll/rspec.f_coll_grid_mean;
        mean_fix_term_mcg = box->mean_f_coll_MINI/rspec.f_coll_grid_mean_MINI;
        LOG_SUPER_DEBUG("global mean fcoll %.4e box mean fcoll %.4e ratio %.4e",box->mean_f_coll,rspec.f_coll_grid_mean,mean_fix_term_acg);
        LOG_SUPER_DEBUG("MINI: global mean fcoll %.4e box mean fcoll %.4e ratio %.4e",box->mean_f_coll_MINI,rspec.f_coll_grid_mean_MINI,mean_fix_term_acg);
    }
    else{
        //if we don't fix the mean, make the mean_f_coll in the output reflect the box
        //since currently is is the global expected mean from the Unconditional MF
        box->mean_f_coll = rspec.f_coll_grid_mean;
        box->mean_f_coll_MINI = rspec.f_coll_grid_mean_MINI;
    }

    #pragma omp parallel num_threads(user_params_global->N_THREADS)
    {
        int x,y,z;
        double curr_dens;
        double curr_fcoll;
        double curr_fcoll_mini;
        double rec,xHII_from_xrays;
        double res_xH;

        double ave_M_coll_cell;
        double ave_N_min_cell;
        int N_halos_in_cell;
        #pragma omp for
        for(x = 0; x < user_params_global->HII_DIM; x++) {
            for(y = 0; y < user_params_global->HII_DIM; y++) {
                for(z = 0; z < HII_D_PARA; z++) {
                    //Use unfiltered density for CELL_RECOMB case, since the "Fcoll" represents photons
                    //  reaching the central cell rather than photons in the entire sphere
                    //TODO: if we don't filter on the last step either, should we use the original field to prevent aliasing?
                    //TODO: test using the filtered density here with CELL_RECOMB, since it's what Davies+22 does,
                    //  It's not clear how the MFP filter should count recombinations/local density.
                    if(flag_options_global->CELL_RECOMB)
                        curr_dens = perturbed_field->density[HII_R_INDEX(x,y,z)]*consts->photoncons_adjustment_factor;
                    else
                        curr_dens = *((float *)fg_struct->deltax_filtered + HII_R_FFT_INDEX(x,y,z));

                    curr_fcoll = box->Fcoll[fc_r_idx * HII_TOT_NUM_PIXELS + HII_R_INDEX(x,y,z)];
                    curr_fcoll = mean_fix_term_acg * curr_fcoll;

                    //Since the halo boxes give ionising photon output, this term accounts for the local density of absorbers
                    //  We have separated the source/absorber filtering in the halo model so this is necessary
                    if(flag_options_global->USE_HALO_FIELD)
                        curr_fcoll *= 1 / (RHOcrit*cosmo_params_global->OMb*(1+curr_dens));

                    //MINIHALOS are already included in the halo model
                    curr_fcoll_mini = 0.;
                    if (flag_options_global->USE_MINI_HALOS && !flag_options_global->USE_HALO_FIELD){
                        curr_fcoll_mini = box->Fcoll_MINI[fc_r_idx * HII_TOT_NUM_PIXELS + HII_R_INDEX(x,y,z)];
                        curr_fcoll_mini = mean_fix_term_mcg * curr_fcoll_mini;
                    }

                    if (flag_options_global->USE_MASS_DEPENDENT_ZETA) {
                        if (curr_fcoll < f_limit_acg) curr_fcoll = f_limit_acg;
                        if (flag_options_global->USE_MINI_HALOS){
                            if (curr_fcoll_mini < f_limit_mcg) curr_fcoll_mini = f_limit_mcg;
                        }
                    }

                    if (flag_options_global->INHOMO_RECO) {
                        if(flag_options_global->CELL_RECOMB)
                            rec = previous_ionize_box->dNrec_box[HII_R_INDEX(x,y,z)];
                        else
                            rec = (*((float *) fg_struct->N_rec_filtered + HII_R_FFT_INDEX(x, y, z))); // number of recombinations per mean baryon

                        rec /= (1. + curr_dens); // number of recombinations per baryon inside cell/filter
                    }
                    else {
                        rec = 0.;
                    }

                    // adjust the denominator of the collapse fraction for the residual electron fraction in the neutral medium
                    if (flag_options_global->USE_TS_FLUCT){
                        xHII_from_xrays = *((float *)fg_struct->xe_filtered + HII_R_FFT_INDEX(x,y,z));
                    } else {
                        xHII_from_xrays = 0.;
                    }

#if LOG_LEVEL > SUPER_DEBUG_LEVEL
                    if(x+y+z == 0 && !flag_options_global->USE_HALO_FIELD){
                        LOG_SUPER_DEBUG("Cell 0: R=%.1f | d %.4e | fcoll %.4e | rec %.4e | X %.4e",
                                            rspec.R,curr_dens,curr_fcoll,rec,xHII_from_xrays);
                        if(flag_options_global->USE_MINI_HALOS){
                            LOG_SUPER_DEBUG("Mini %.4e",curr_fcoll_mini);
                        }
                    }
#endif

                    // check if fully ionized!
                    if ( (curr_fcoll * consts->ion_eff_factor + curr_fcoll_mini * consts->ion_eff_factor_mini > (1. - xHII_from_xrays)*(1.0+rec)) ){ //IONIZED!!
                        // if this is the first crossing of the ionization barrier for this cell (largest R), record the gamma
                        // this assumes photon-starved growth of HII regions...  breaks down post EoR
                        if (flag_options_global->INHOMO_RECO && (box->xH_box[HII_R_INDEX(x,y,z)] > FRACT_FLOAT_ERR)){
                            if(flag_options_global->USE_HALO_FIELD){
                                //since ION_EFF_FACTOR==1 here, gamma_prefactor is the same for ACG and MCG
                                box->Gamma12_box[HII_R_INDEX(x,y,z)] = rspec.R * consts->gamma_prefactor / (1+curr_dens) \
                                                                    * (*((float *)fg_struct->sfr_filtered + HII_R_FFT_INDEX(x,y,z)));
                            }
                            else{
                                box->Gamma12_box[HII_R_INDEX(x,y,z)] = rspec.R * consts->gamma_prefactor * curr_fcoll + consts->gamma_prefactor_mini * curr_fcoll_mini;
                            }
                            box->MFP_box[HII_R_INDEX(x,y,z)] = rspec.R;
                        }

                        // keep track of the first time this cell is ionized (earliest time)
                        if (previous_ionize_box->z_re_box[HII_R_INDEX(x,y,z)] < 0){
                            box->z_re_box[HII_R_INDEX(x,y,z)] = consts->redshift; //TODO: stored_redshift???
                        } else{
                            box->z_re_box[HII_R_INDEX(x,y,z)] = previous_ionize_box->z_re_box[HII_R_INDEX(x,y,z)];
                        }

                        // FLAG CELL(S) AS IONIZED
                        if (global_params.FIND_BUBBLE_ALGORITHM == 2) // center method
                            box->xH_box[HII_R_INDEX(x,y,z)] = 0;
                        if (global_params.FIND_BUBBLE_ALGORITHM == 1) // sphere method
                            update_in_sphere(box->xH_box, user_params_global->HII_DIM, HII_D_PARA, rspec.R/(user_params_global->BOX_LEN), \
                                                x/(user_params_global->HII_DIM+0.0), y/(user_params_global->HII_DIM+0.0), z/(HII_D_PARA+0.0));
                    } // end ionized
                        // If not fully ionized, then assign partial ionizations
                    else if (rspec.R_index==0 && (box->xH_box[HII_R_INDEX(x, y, z)] > TINY)) {
                        //TODO: figure out why this model exists
                        //This places some RNG at the cell-scale on the last filter step for partial reionisations
                        //  This is done by sampling from the Poisson distribution, in units of the *total halo mass in the cell*
                        //  with an average value of (default)5.
                        //With NO_RNG, this means that all non-ionised cells have 1/5 of their previous collapsed fraction?
                        if (!flag_options_global->USE_HALO_FIELD){
                            //TODO: the N_halos.. was previously in the above loop, but was only used for partial ionisations anyway
                            ave_M_coll_cell = (curr_fcoll + curr_fcoll_mini) * consts->pixel_mass * (1. + curr_dens);
                            ave_N_min_cell = ave_M_coll_cell / consts->M_min; // ave # of M_MIN halos in cell
                            if(user_params_global->NO_RNG) {
                                N_halos_in_cell = 1.;
                            }
                            else {
                                N_halos_in_cell = (int)gsl_ran_poisson(cell_rng[omp_get_thread_num()],
                                                                        global_params.N_POISSON);
                            }

                            if (curr_fcoll>1) curr_fcoll=1;
                            if (curr_fcoll_mini>1) curr_fcoll_mini=1;
                            if(ave_N_min_cell < global_params.N_POISSON) {
                                curr_fcoll = N_halos_in_cell * ( ave_M_coll_cell / (float)global_params.N_POISSON ) / (consts->pixel_mass*(1. + curr_dens));
                                if (flag_options_global->USE_MINI_HALOS){
                                    curr_fcoll_mini = curr_fcoll * (curr_fcoll_mini * consts->ion_eff_factor) \
                                                     / (curr_fcoll * consts->ion_eff_factor + curr_fcoll_mini * consts->ion_eff_factor_mini);
                                    curr_fcoll = curr_fcoll - curr_fcoll_mini;
                                }
                                else{
                                    curr_fcoll_mini = 0.;
                                }
                            }

                            if (ave_M_coll_cell < (consts->M_min / 5.)) {
                                curr_fcoll = 0.;
                                curr_fcoll_mini = 0.;
                            }

                            if (curr_fcoll>1) curr_fcoll=1;
                            if (curr_fcoll_mini>1) curr_fcoll_mini=1;
                        }

                        res_xH = 1. - curr_fcoll * consts->ion_eff_factor - curr_fcoll_mini * consts->ion_eff_factor_mini;
                        // put the partial ionization here because we need to exclude xHII_from_xrays...
                        if (flag_options_global->USE_TS_FLUCT){
                            box->temp_kinetic_all_gas[HII_R_INDEX(x,y,z)] = ComputePartiallyIoinizedTemperature(spin_temp->Tk_box[HII_R_INDEX(x,y,z)], res_xH);
                        }
                        else{
                            box->temp_kinetic_all_gas[HII_R_INDEX(x,y,z)] = ComputePartiallyIoinizedTemperature(
                                    consts->TK_nofluct*(1 + consts->adia_TK_term*perturbed_field->density[HII_R_INDEX(x,y,z)]), res_xH);
                        }
                        res_xH -= xHII_from_xrays;

                        // and make sure fraction doesn't blow up for underdense pixels
                        if (res_xH < 0)
                            res_xH = 0;
                        else if (res_xH > 1)
                            res_xH = 1;

                        box->xH_box[HII_R_INDEX(x, y, z)] = res_xH;

                    } // end partial ionizations at last filtering step
                } // k
            } // j
        } // i
    }
}

void set_ionized_temperatures(IonizedBox *box, PerturbedField *perturbed_field, TsBox *spin_temp, struct IonBoxConstants *consts){
    int x,y,z;
    #pragma omp parallel private(x,y,z) num_threads(user_params_global->N_THREADS)
    {
        float thistk;
        #pragma omp for
        for (x=0; x<user_params_global->HII_DIM; x++){
            for (y=0; y<user_params_global->HII_DIM; y++){
                for (z=0; z<HII_D_PARA; z++){
                    if ((box->z_re_box[HII_R_INDEX(x,y,z)]>0) && (box->xH_box[HII_R_INDEX(x,y,z)] < TINY)){
                        //TODO: do we want to use the photoncons redshift here or the original one?
                        box->temp_kinetic_all_gas[HII_R_INDEX(x,y,z)] = ComputeFullyIoinizedTemperature(box->z_re_box[HII_R_INDEX(x,y,z)], \
                                                                    consts->stored_redshift, perturbed_field->density[HII_R_INDEX(x,y,z)]);
                        // Below sometimes (very rare though) can happen when the density drops too fast and to below T_HI
                        if (flag_options_global->USE_TS_FLUCT){
                            if (box->temp_kinetic_all_gas[HII_R_INDEX(x,y,z)] < spin_temp->Tk_box[HII_R_INDEX(x,y,z)])
                                box->temp_kinetic_all_gas[HII_R_INDEX(x,y,z)] = spin_temp->Tk_box[HII_R_INDEX(x,y,z)];
                            }
                        else{
                            thistk = consts->TK_nofluct*(1 + consts->adia_TK_term * \
                                        perturbed_field->density[HII_R_INDEX(x,y,z)]);
                            if (box->temp_kinetic_all_gas[HII_R_INDEX(x,y,z)] < thistk)
                                box->temp_kinetic_all_gas[HII_R_INDEX(x,y,z)] = thistk;
                        }
                    }
                }
            }
        }
    }

    for (x=0; x<user_params_global->HII_DIM; x++){
        for (y=0; y<user_params_global->HII_DIM; y++){
            for (z=0; z<HII_D_PARA; z++){
                if(isfinite(box->temp_kinetic_all_gas[HII_R_INDEX(x,y,z)])==0){
                    LOG_ERROR("Tk after fully ioinzation is either infinite or a Nan. Something has gone wrong "\
                                "in the temperature calculation: z_re=%.4f, redshift=%.4f, curr_dens=%.4e", box->z_re_box[HII_R_INDEX(x,y,z)], consts->stored_redshift,
                                perturbed_field->density[HII_R_INDEX(x,y,z)]);
                    Throw(InfinityorNaNError);
                }
            }
        }
    }
}

void set_recombination_rates(IonizedBox *box, IonizedBox *previous_ionize_box, PerturbedField *perturbed_field, struct IonBoxConstants *consts){
    bool finite_error;
    #pragma omp parallel num_threads(user_params_global->N_THREADS)
    {
        int x,y,z;
        double curr_dens,dNrec;
        double z_eff;
        #pragma omp for
        for(x = 0; x < user_params_global->HII_DIM; x++) {
            for(y = 0; y < user_params_global->HII_DIM; y++) {
                for(z = 0; z < HII_D_PARA; z++) {
                    // use the original density and redshift for the snapshot (not the adjusted redshift)
                    // Only want to use the adjusted redshift for the ionisation field
                    //NOTE: but the structure field wasn't adjusted, this seems wrong
                    // curr_dens = 1.0 + (perturbed_field->density[HII_R_INDEX(x, y, z)]) / adjustment_factor;
                    curr_dens = 1.0 + (perturbed_field->density[HII_R_INDEX(x, y, z)]);
                    z_eff = pow(curr_dens, 1.0 / 3.0);
                    z_eff *= (1 + consts->stored_redshift);

                    dNrec = splined_recombination_rate(z_eff - 1., box->Gamma12_box[HII_R_INDEX(x, y, z)]) *
                            consts->fabs_dtdz * consts->dz * (1. - box->xH_box[HII_R_INDEX(x, y, z)]);

                    if (isfinite(dNrec) == 0) {
                        finite_error = true;
                    }

                    box->dNrec_box[HII_R_INDEX(x, y, z)] =
                            previous_ionize_box->dNrec_box[HII_R_INDEX(x, y, z)] + dNrec;
                }
            }
        }
    }

    if(finite_error) {
        LOG_ERROR("Recombinations have returned either an infinite or NaN value.");
        Throw(InfinityorNaNError);
    }
}

int ComputeIonizedBox(float redshift, float prev_redshift, UserParams *user_params, CosmoParams *cosmo_params,
                       AstroParams *astro_params, FlagOptions *flag_options,
                       PerturbedField *perturbed_field,
                       PerturbedField *previous_perturbed_field,
                       IonizedBox *previous_ionize_box,
                       TsBox *spin_temp,
                       HaloBox *halos,
                       InitialConditions *ini_boxes,
                       IonizedBox *box) {

    int status;

    Try{ // This Try brackets the whole function, so we don't indent.
    LOG_DEBUG("input values:");
    LOG_DEBUG("redshift=%f, prev_redshift=%f", redshift, prev_redshift);
    #if LOG_LEVEL >= DEBUG_LEVEL
        writeUserParams(user_params);
        writeCosmoParams(cosmo_params);
        writeAstroParams(flag_options, astro_params);
        writeFlagOptions(flag_options);
    #endif

    //TODO: moved from inside the R loop, either place verification in python wrapper or write a function to verify backend parameters
    if (global_params.FIND_BUBBLE_ALGORITHM != 2 && global_params.FIND_BUBBLE_ALGORITHM != 1){ // center method
        LOG_ERROR("Incorrect choice of find bubble algorithm: %i",
                    global_params.FIND_BUBBLE_ALGORITHM);
        Throw(ValueError);
    }

    // Makes the parameter structs visible to a variety of functions/macros
    // Do each time to avoid Python garbage collection issues
    Broadcast_struct_global_all(user_params,cosmo_params,astro_params,flag_options);

    omp_set_num_threads(user_params->N_THREADS);

    unsigned long long ct;

    double global_xH;

    //TODO: check if this is used in this file with TS fluctuations
    init_heat();
    init_ps();

    struct IonBoxConstants ionbox_constants;
    set_ionbox_constants(redshift,prev_redshift,cosmo_params,astro_params,flag_options,&ionbox_constants);

    //boxes which aren't guaranteed to have every element assigned to need to be initialised
    //TODO: they should be zero'd in the wrapper, right?
    if(flag_options->INHOMO_RECO) {
        if(INIT_RECOMBINATIONS) {
            init_MHR();
            INIT_RECOMBINATIONS=0;
        }

        #pragma omp parallel shared(box) private(ct) num_threads(user_params->N_THREADS)
        {
            #pragma omp for
            for (ct=0; ct<HII_TOT_NUM_PIXELS; ct++) {
                box->Gamma12_box[ct] = 0.0;
                box->MFP_box[ct] = 0.0;
            }
        }
    }

    #pragma omp parallel shared(box) private(ct) num_threads(user_params->N_THREADS)
    {
        #pragma omp for
        for (ct=0; ct<HII_TOT_NUM_PIXELS; ct++) {
            box->z_re_box[ct] = -1.0;
        }
    }

    LOG_SUPER_DEBUG("z_re_box init: ");
    debugSummarizeBox(box->z_re_box, user_params->HII_DIM, user_params->NON_CUBIC_FACTOR, "  ");

    //These are intentionally done before any photoncons redshift adjustment

    // Modify the current sampled redshift to a redshift which matches the expected filling factor given our astrophysical parameterisation.
    // This is the photon non-conservation correction
    float absolute_delta_z = 0.;
    float redshift_pc, stored_redshift_pc;
    if(flag_options->PHOTON_CONS_TYPE == 1) {
        redshift_pc = redshift;
        adjust_redshifts_for_photoncons(astro_params,flag_options,&redshift_pc,&stored_redshift_pc,&absolute_delta_z);
        ionbox_constants.redshift = redshift_pc;
        ionbox_constants.stored_redshift = stored_redshift_pc;
        ionbox_constants.photoncons_adjustment_factor = dicke(redshift_pc)/dicke(stored_redshift_pc);
        //TODO: if we want this to work with minihalos we need to change prev_redshift too
        LOG_DEBUG("PhotonCons data:");
        LOG_DEBUG("original redshift=%f, updated redshift=%f delta-z = %f", stored_redshift_pc, redshift_pc, absolute_delta_z);
        if(isfinite(redshift_pc)==0 || isfinite(absolute_delta_z)==0) {
            LOG_ERROR("Updated photon non-conservation redshift is either infinite or NaN!");
            LOG_ERROR("This can sometimes occur when reionisation stalls (i.e. extremely low"\
                      "F_ESC or F_STAR or not enough sources)");
            Throw(PhotonConsError);
        }
    }
    /////////////////////////////////   BEGIN INITIALIZATION   //////////////////////////////////

    // perform a very rudimentary check to see if we are underresolved and not using the linear approx
    if ((user_params->BOX_LEN > user_params->DIM) && !(global_params.EVOLVE_DENSITY_LINEARLY)){
        LOG_WARNING("Resolution is likely too low for accurate evolved density fields\n It Is recommended \
                    that you either increase the resolution (DIM/BOX_LEN) or set the EVOLVE_DENSITY_LINEARLY flag to 1\n");
    }

    struct RadiusSpec *radii_spec;
    int n_radii;
    n_radii = setup_radii(&radii_spec,&ionbox_constants);

    //CONSTRUCT GRIDS OUTSIDE R LOOP HERE
    //if we don't have a previous ionised box, make a fake one here
    if (prev_redshift < 1)
        setup_first_z_prevbox(previous_ionize_box,previous_perturbed_field,n_radii);

    struct FilteredGrids *grid_struct;
    allocate_fftw_grids(&grid_struct);

    //Find the mass limits and average turnovers
    double Mturnover_global_avg, Mturnover_global_avg_MINI;
    if (flag_options->USE_MASS_DEPENDENT_ZETA){
        if (flag_options->USE_MINI_HALOS){
            LOG_SUPER_DEBUG("Calculating and outputting Mcrit boxes for atomic and molecular halos...");
            calculate_mcrit_boxes(previous_ionize_box,
                                    spin_temp,
                                    ini_boxes,
                                    &ionbox_constants,
                                    grid_struct->log10_Mturnover_unfiltered,
                                    grid_struct->log10_Mturnover_MINI_unfiltered,
                                    &(box->log10_Mturnover_ave),
                                    &(box->log10_Mturnover_MINI_ave));

            Mturnover_global_avg                 = pow(10., box->log10_Mturnover_ave);
            Mturnover_global_avg_MINI            = pow(10., box->log10_Mturnover_MINI_ave);
            LOG_DEBUG("average log10 turnover masses are %.2f and %.2f for ACGs and MCGs", box->log10_Mturnover_ave, box->log10_Mturnover_MINI_ave);
        }
        else{
            Mturnover_global_avg = astro_params->M_TURN;
            box->log10_Mturnover_ave = log10(Mturnover_global_avg);
            box->log10_Mturnover_MINI_ave = log10(Mturnover_global_avg);
        }
    }
    // lets check if we are going to bother with computing the inhmogeneous field at all...
    global_xH = 0.0;

    //HMF integral initialisation
    if(user_params->USE_INTERPOLATION_TABLES) {
        if(user_params->INTEGRATION_METHOD_ATOMIC == 2 || user_params->INTEGRATION_METHOD_MINI == 2)
            initialiseSigmaMInterpTable(fmin(MMIN_FAST,ionbox_constants.M_min),1e20);
        else
            initialiseSigmaMInterpTable(ionbox_constants.M_min,1e20);
    }
    LOG_SUPER_DEBUG("sigma table has been initialised");

    if(user_params->INTEGRATION_METHOD_ATOMIC == 1 || (flag_options->USE_MINI_HALOS && user_params->INTEGRATION_METHOD_MINI == 1))
        initialise_GL(ionbox_constants.lnMmin, ionbox_constants.lnMmax_gl);

    double f_limit_acg;
    double f_limit_mcg;
    set_mean_fcoll(&ionbox_constants,previous_ionize_box,box,Mturnover_global_avg,Mturnover_global_avg_MINI,&f_limit_acg,&f_limit_mcg);
    double exp_global_hii = box->mean_f_coll * ionbox_constants.ion_eff_factor_gl \
                            + box->mean_f_coll_MINI * ionbox_constants.ion_eff_factor_mini_gl;

    //We need some RNG for the cell-scale partial ionisations,
    // but we don't want to init inside find_ionsed regions since its a bit slow
    bool need_rng = !user_params->NO_RNG && !flag_options->USE_HALO_FIELD;
    gsl_rng * cell_rng[user_params->N_THREADS];

    //TODO: I want to move this in the `else` below but freeing becomes annoying
    if(need_rng){
        //TODO: proper seeding (not zero)
        seed_rng_threads_fast(cell_rng,0);
    }

    //TODO: change this from an if-else to an early-exit / cleanup call
    if(exp_global_hii < global_params.HII_ROUND_ERR){ // way too small to ionize anything...
        LOG_DEBUG("Mean collapsed fraciton %.3e too small to ionize, stopping early",exp_global_hii);
        global_xH = set_fully_neutral_box(box,spin_temp,perturbed_field,&ionbox_constants);
    }
    else{
        LOG_SUPER_DEBUG("Starting FFTs");
        //DO THE R2C TRANSFORMS
        //TODO: add debug average printing to these boxes
        //TODO: put a flag for to turn off clipping instead of putting the wide limits
        prepare_box_for_filtering(perturbed_field->density,grid_struct->deltax_unfiltered,ionbox_constants.photoncons_adjustment_factor,-1.,1e6);
        if(flag_options->USE_HALO_FIELD) {
            prepare_box_for_filtering(halos->n_ion,grid_struct->stars_unfiltered,1.,0.,1e20);
            prepare_box_for_filtering(halos->whalo_sfr,grid_struct->sfr_unfiltered,1.,0.,1e20);
        }
        else{
            if(flag_options->USE_MINI_HALOS){
                prepare_box_for_filtering(previous_perturbed_field->density,grid_struct->prev_deltax_unfiltered,1.,-1,1e6);
                //since the turnover mass boxes were assigned separately (they needed more complex functions)...
                dft_r2c_cube(user_params->USE_FFTW_WISDOM, user_params->HII_DIM, HII_D_PARA,
                                user_params->N_THREADS, grid_struct->log10_Mturnover_MINI_unfiltered);
                dft_r2c_cube(user_params->USE_FFTW_WISDOM, user_params->HII_DIM, HII_D_PARA,
                                user_params->N_THREADS, grid_struct->log10_Mturnover_unfiltered);
            }
        }
        if(flag_options->USE_TS_FLUCT){
            prepare_box_for_filtering(spin_temp->x_e_box,grid_struct->xe_unfiltered,1.,0,1.);
        }

        if(ionbox_constants.filter_recombinations){
            prepare_box_for_filtering(previous_ionize_box->dNrec_box,grid_struct->N_rec_unfiltered,1.,0,1e20);
        }
        LOG_SUPER_DEBUG("FFTs performed");

        // ************************************************************************************* //
        // ***************** LOOP THROUGH THE FILTER RADII (in Mpc)  *************************** //
        // ************************************************************************************* //
        // set the max radius we will use, making sure we are always sampling the same values of radius
        // (this avoids aliasing differences w redshift)

        int R_ct;
        struct RadiusSpec curr_radius;
        for(R_ct=n_radii;R_ct--;){
            curr_radius = radii_spec[R_ct];

            //TODO: As far as I can tell, This was the previous behaviour with the while loop
            //  So if the cell size is smaller than the minimum mass (rare) we still filter the last step
            //  and don't assign any partial ionisaitons
            if(ionbox_constants.M_min > RtoM(curr_radius.R)){
                LOG_DEBUG("Radius %.2e Mass %.2e smaller than minimum %.2e, stopping...",
                            radii_spec[R_ct].R,radii_spec[R_ct].M_max_R,ionbox_constants.M_min);
                break;
            }
            LOG_ULTRA_DEBUG("while loop for until RtoM(R)=%f reaches M_MIN=%f", RtoM(curr_radius.R), ionbox_constants.M_min);

            //do all the filtering and inverse transform
            copy_filter_transform(grid_struct,&ionbox_constants,curr_radius);

            bool need_prev_ion = previous_ionize_box->mean_f_coll_MINI * ionbox_constants.ion_eff_factor_mini_gl + \
                            previous_ionize_box->mean_f_coll * ionbox_constants.ion_eff_factor_gl > 1e-4;

            if (!flag_options->USE_HALO_FIELD) {
                setup_integration_tables(grid_struct,&ionbox_constants,curr_radius,need_prev_ion);
                LOG_SUPER_DEBUG("Initialised tables");
            }

            calculate_fcoll_grid(box,previous_ionize_box,grid_struct,&ionbox_constants,&curr_radius);
            // To avoid ST_over_PS becoming nan when f_coll = 0, I set f_coll = FRACT_FLOAT_ERR.
            // TODO: This was the previous behaviour, but is this right?
            // setting the *total* to the minimum for the adjustment factor,
            // then clipping the grid in the loop below?
            if (flag_options_global->USE_MASS_DEPENDENT_ZETA) {
                if (curr_radius.f_coll_grid_mean <= f_limit_acg) curr_radius.f_coll_grid_mean = f_limit_acg;
                if (flag_options->USE_MINI_HALOS){
                    if (curr_radius.f_coll_grid_mean_MINI <= f_limit_mcg) curr_radius.f_coll_grid_mean_MINI = f_limit_mcg;
                }
            }
            else {
                if (curr_radius.f_coll_grid_mean <= FRACT_FLOAT_ERR) curr_radius.f_coll_grid_mean = FRACT_FLOAT_ERR;
            }

            find_ionised_regions(box,previous_ionize_box,perturbed_field,spin_temp,curr_radius,&ionbox_constants,
                                    grid_struct,cell_rng,f_limit_acg,f_limit_mcg);

            LOG_SUPER_DEBUG("z_re_box after R=%f: ", curr_radius.R);
            debugSummarizeBox(box->z_re_box, user_params->HII_DIM, user_params->NON_CUBIC_FACTOR, "  ");
        }

        set_ionized_temperatures(box,perturbed_field,spin_temp,&ionbox_constants);

        // find the neutral fraction
#if LOG_LEVEL >= DEBUG_LEVEL
        global_xH = 0;

        #pragma omp parallel shared(box) private(ct) num_threads(user_params->N_THREADS)
        {
        #pragma omp for reduction(+:global_xH)
            for (ct = 0; ct < HII_TOT_NUM_PIXELS; ct++) {
                global_xH += box->xH_box[ct];
            }
        }
        global_xH /= (float) HII_TOT_NUM_PIXELS;
#endif

        if(isfinite(global_xH) == 0) {
            LOG_ERROR(
                    "Neutral fraction is either infinite or a Nan. Something has gone wrong in the ionisation calculation!");
            Throw(InfinityorNaNError);
        }

        // update the N_rec field
        if (flag_options->INHOMO_RECO) {
            set_recombination_rates(box,previous_ionize_box,perturbed_field,&ionbox_constants);
        }

        fftwf_cleanup_threads();
        fftwf_cleanup();
        fftwf_forget_wisdom();
    }

    destruct_heat();

    LOG_DEBUG("global_xH = %e",global_xH);
    free_fftw_grids(grid_struct);
    LOG_SUPER_DEBUG("freed fftw boxes");
    if (prev_redshift < 1){
        free(previous_ionize_box->z_re_box);
        if (flag_options->USE_MASS_DEPENDENT_ZETA && flag_options->USE_MINI_HALOS){
            free(previous_ionize_box->Gamma12_box);
            free(previous_ionize_box->dNrec_box);
            free(previous_ionize_box->Fcoll);
            free(previous_ionize_box->Fcoll_MINI);
        }
    }

    if(!flag_options->USE_TS_FLUCT && user_params->USE_INTERPOLATION_TABLES) {
            freeSigmaMInterpTable();
    }

    //This function checks for allocation so don't worry about double-freeing tables
    free_conditional_tables();

    free(radii_spec);

    if(need_rng){
        free_rng_threads(cell_rng);
    }

    LOG_DEBUG("finished!\n");

    } // End of Try()

    Catch(status){
        return(status);
    }
    return(0);
}
