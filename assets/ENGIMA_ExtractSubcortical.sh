#!/bin/bash

##################################################################
# This extracts the surcortical volumes from freesurfer in the format used by ENIGMA meta-analysis
# taken from ENIGMA Consortium Instruction doc
# adapted into shell script (taking two arguments) by Erin W. Dickie, Sep 15, 2015
# Usage:
#     ENIGMA_ExtractSubcortical.sh <SUBJECT_DIR> <PREFIX>
#
# Arguements:
# SUBJECT_DIR     the output folder of freesurfer Results
# PREFIX          a string for filtering SUBJECTS_DIR contents to find subjects
# (Ouputfile is always "$SUBJECTS_DIR/LandRvolumes.csv")
####################################################################

SUBJECTS_DIR=${1}
PREFIX=${2}

cd ${SUBJECTS_DIR}

echo "SubjID, LLatVent,RLatVent,Lthal,Rthal,Lcaud,Rcaud,Lput,Rput,\
  Lpal,Rpal,Lhippo,Rhippo,Lamyg,Ramyg,Laccumb,Raccumb,ICV" > LandRvolumes.csv

for subj_id in `ls -d ${PREFIX}*`;
do #may need to change this so that is selects subjects with FS output

  printf "%s,"  "${subj_id}" >> LandRvolumes.csv

  for x in Left-Lateral-Ventricle Right-Lateral-Ventricle Left-Thalamus-Proper Right-Thalamus-Proper Left-Caudate Right-Caudate Left-Putamen Right-Putamen Left-Pallidum Right-Pallidum Left-Hippocampus Right-Hippocampus Left-Amygdala Right-Amygdala Left-Accumbens-area Right-Accumbens-area;
  do

    printf "%g," `grep  ${x} ${subj_id}/stats/aseg.stats | awk '{print $4}'` >> LandRvolumes.csv

  done

printf "%g" `cat ${subj_id}/stats/aseg.stats | grep IntraCranialVol | awk -F, '{print $4}'` >> LandRvolumes.csv

echo "" >> LandRvolumes.csv

done 
