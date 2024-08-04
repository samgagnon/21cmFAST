#ifndef _IONBOX_H
#define _IONBOX_H

#include "InputParameters.h"
#include "OutputStructs.h"

int ComputeIonizedBox(float redshift, float prev_redshift, UserParams *user_params, CosmoParams *cosmo_params,
                        AstroParams *astro_params, FlagOptions *flag_options,
                        PerturbedField *perturbed_field, PerturbedField *previous_perturbed_field,
                        IonizedBox *previous_ionize_box, TsBox *spin_temp,
                        HaloBox *halos, InitialConditions *ini_boxes,
                        IonizedBox *box);

#endif
