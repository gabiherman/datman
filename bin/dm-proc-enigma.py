#!/usr/bin/env python
"""
This runs the ENIGMA DTI pipeline on FA maps after DTI-fit has been run.
Calls (or submits) doInd-enigma-dti.py for each subject in order to do so.

Usage:
  dm-proc-enigma.py [options] <study>

Arguments:
    <study>                A study code from the site config file

Options:
  --config PATH            The path to a site config file. If not set, the
                           value of the environment variable DM_CONFIG will be
                           used.
  --system STR             The system configuration to use from the site config.
                           If not set, the environment variable DM_SYSTEM will
                           be used.
  --FA-tag STR             String used to identify FA maps within DTI-fit input
                           (default = '_FA.nii.gz'))
  --calc-MD                Calculate values for MD
  --calc-all               Calculate values for MD, AD, and RD
  --subject-filter STR     String used to filter subject ID (i.e. a site tag?)
  --FA-filter STR          Optional second filter used (as well as '--FA-tag',
                           ex. 'DTI-60')) to identify the maps within DTI-fit
                           input
  --QC-transfer QCFILE     QC checklist file - if this option is given than only
                           QCed participants will be processed.
  --walltime TIME          A walltime for the enigma stage [default: 2:00:00]
  --walltime-post TIME     A walltime for the post-engima stage [default: 2:00:00]
  --no-post                Do not submit the post-processing (concatenation) script
  --post-only              Submit the post-processing (concatenation) script by
                           itself
  -q, --quiet              Only log errors
  -v,--verbose             Verbose logging
  --debug                  Debug logging in Erin's very verbose style
  -n,--dry-run             Dry run
  -h, --help               Show help

DETAILS
This run ENIGMA DTI pipeline on FA maps after DTI-fit has been run.
Calls (or submits) doInd-enigma-dti.py for each subject in order to do so.
Also submits a concatcsv-enigmadti.py and enigmadti-qc.py as a held job
to concatenate the results from each participant into outputs .csv files
and create some QC web pages.

This script will search inside the dti-fit output folder for FA images to
process. It uses the '--FA-tag' string (which is '_FA.nii.gz' by default) to do
so. If this optional argument (('--tag2') is given, this string will be used to
refine the search, if more than one FA file is found inside the participants
directory.

The FA image found for each participant in printed in the 'FA_nii' column
of "ENIGMA-DTI-checklist.csv". If no FA image is found, or more than one FA
image is found, a note to that effect is printed in the "notes" column of the
same file. You can manually overide this process by editing the
"ENIGMA-DTI-checklist.csv" with the name of the FA image you would like
processed (esp. in the case of repeat scans).

The script then looks to see if any of the FA images (listed in the
"ENIGMA-DTI-checklist.csv" "FA_nii" column) have not been processed (i.e. have
no outputs). These images are then submitted to the queue.

If the "--QC-transfer" option is used, the QC checklist from data transfer
(i.e. metadata/checklist.csv) and only those participants who passed QC will be
processed.

Requires ENIGMA dti enviroment to be set (for example):
module load FSL/5.0.7 R/3.1.1 ENIGMA-DTI/2015.01
(also requires the datmat python enviroment)

Written by Erin W Dickie, July 30 2015
Adapted from ENIGMA_MASTER.sh - Generalized October 2nd David Rotenberg
Updated Feb 2015 by JP+TB
"""
import os
import sys
import glob
import datetime
import tempfile
import shutil
import filecmp
import difflib
import contextlib
import logging

from docopt import docopt
import pandas as pd

import datman as dm
import datman.utils
import datman.proc
import datman.config

DRYRUN = False

logging.basicConfig(level=logging.WARN,
                    format="[%(name)s] %(levelname)s: %(message)s")
logger = logging.getLogger(os.path.basename(__file__))

def main():
    global dryrun

    arguments       = docopt(__doc__)
    study           = arguments['<study>']
    config          = arguments['--config']
    system          = arguments['--system']
    QC_file         = arguments['--QC-transfer']
    FA_tag          = arguments['--FA-tag']
    subject_filter  = arguments['--subject-filter']
    FA_filter       = arguments['--FA-filter']
    CALC_MD         = arguments['--calc-MD']
    CALC_ALL        = arguments['--calc-all']
    walltime        = arguments['--walltime']
    walltime_post  = arguments['--walltime-post']
    POST_ONLY      = arguments['--post-only']
    NO_POST        = arguments['--no-post']
    quiet           = arguments['--quiet']
    verbose         = arguments['--verbose']
    debug           = arguments['--debug']
    DRYRUN          = arguments['--dry-run']

    if quiet:
        logger.setLevel(logging.ERROR)

    if verbose:
        logger.setLevel(logging.INFO)

    if debug:
        logger.setLevel(logging.DEBUG)

    config = datman.config.config(filename=config, system=system, study=study)

    ## make the output directory if it doesn't exist
    input_dir = config.get_path('dtifit')
    output_dir = config.get_path('enigmaDTI')
    log_dir = os.path.join(output_dir,'logs')
    run_dir = os.path.join(output_dir,'bin')
    dm.utils.makedirs(log_dir)
    dm.utils.makedirs(run_dir)

    logger.debug(arguments)

    if FA_tag == None: FA_tag = '_FA.nii.gz'

    subjects = dm.proc.get_subject_list(input_dir, subject_filter, QC_file)

    # check if we have any work to do, exit if not
    if len(subjects) == 0:
        logger.info('No outstanding scans to process.')
        sys.exit(1)

    # grab the prefix from the subid if not given
    prefix = config.get_key('STUDY_TAG')

    ## write and check the run scripts
    script_names = ['run_engimadti.sh','concatresults.sh']
    write_run_scripts(script_names, run_dir, output_dir, CALC_MD, CALC_ALL, debug)

    checklist_file = os.path.normpath(output_dir + '/ENIGMA-DTI-checklist.csv')
    checklist_cols = ['id', 'FA_nii', 'date_ran','qc_rator', 'qc_rating', 'notes']
    checklist = dm.proc.load_checklist(checklist_file, checklist_cols)
    checklist = dm.proc.add_new_subjects_to_checklist(subjects,
                                                      checklist, checklist_cols)

    # Update checklist with new FA files to process listed under FA_nii column
    checklist = dm.proc.find_images(checklist, 'FA_nii', input_dir, FA_tag,
                                    subject_filter = subject_filter,
                                    image_filter = FA_filter)

    job_name_prefix="edti{}_{}".format(prefix,datetime.datetime.today().strftime("%Y%m%d-%H%M%S"))
    submit_edti = False

    ## Change dir so it can be submitted without the full path
    os.chdir(run_dir)
    if not POST_ONLY:
        with make_temp_directory() as temp_dir:
            cmds_file = os.path.join(temp_dir,'commands.txt')
            with open(cmds_file, 'w') as cmdlist:
                for i in range(0,len(checklist)):
                    subid = checklist['id'][i]

                    # make sure that second filter is being applied to the qsub bit
                    if subject_filter and subject_filter not in subid:
                        continue

                    ## make sure that a T1 has been selected for this subject
                    if pd.isnull(checklist['FA_nii'][i]):
                        continue

                    ## format contents of T1 column into recon-all command input
                    smap = checklist['FA_nii'][i]

                    if subject_previously_completed(output_dir, subid, smap):
                        continue

                    # If POSTFS_ONLY == False, the run script will be the first or
                    # only name in the list
                    cmdlist.write("bash -l {rundir}/{script} {output} {inputFA}\n".format(
                                    rundir = run_dir,
                                    script = script_names[0],
                                    output = os.path.join(output_dir,subid),
                                    inputFA = os.path.join(input_dir, subid, smap)))

                    ## add today's date to the checklist
                    checklist['date_ran'][i] = datetime.date.today()

                    submit_edti = True

            if submit_edti:
                qbatch_run_cmd = dm.proc.make_file_qbatch_command(cmds_file,
                                                        job_name_prefix,
                                                        log_dir, walltime)
                os.chdir(run_dir)
                dm.utils.run(qbatch_run_cmd, DRYRUN)
    ## if any subjects have been submitted,
    ## submit a final job that will consolidate the results after they are finished
    os.chdir(run_dir)
    post_edit_cmd = 'echo bash -l {rundir}/{script}'.format(
                    rundir = run_dir,
                    script = script_names[1])
    if submit_edti:
        qbatch_post_cmd = dm.proc.make_piped_qbatch_command(post_edit_cmd,
                                                '{}_post'.format(job_name_prefix),
                                                log_dir,
                                                walltime_post,
                                                afterok = job_name_prefix)
        dm.utils.run(qbatch_post_cmd, DRYRUN)

    if not DRYRUN:
        ## write the checklist out to a file
        checklist.to_csv(checklist_file, sep=',', index = False)

def write_run_scripts(script_names, run_dir, output_dir, CALC_MD, CALC_ALL, DEBUG):
    """
    Write DTI run scripts for this project if they don't
    already exist.
    """
    for name in script_names:
        runsh = os.path.join(run_dir, name)
        if os.path.isfile(runsh):
            ## create temporary run file and test it against the original
            check_runsh(runsh, output_dir, CALC_MD, CALC_ALL, DEBUG)
        else:
            ## if it doesn't exist, write it now
            write_run_script(runsh, output_dir, CALC_MD, CALC_ALL)

def write_run_script(filename, output_dir, CALC_MD, CALC_ALL):
    """
    builds a script in the outputdir (run.sh)
    """
    bname = os.path.basename(filename)
    if bname == 'run_engimadti.sh':
        ENGIMASTEP = 'doInd'
    if bname == 'concatresults.sh':
        ENGIMASTEP = 'concat'

    #open file for writing
    enigmash = open(filename,'w')
    enigmash.write('#!/bin/bash\n\n')

    enigmash.write('## this script was created by dm-proc-engima.py\n\n')
    ## can add section here that loads chosen CIVET enviroment
    enigmash.write('## Prints loaded modules to the log\nmodule list\n\n')

    if ENGIMASTEP == 'doInd':
        enigmash.write('OUTDIR=${1}\n')
        enigmash.write('FAMAP=${2}\n')
        ## add the engima-dit command
        enigmash.write('\ndoInd-enigma-dti.py ')
        if CALC_MD: enigmash.write('--calc-MD ')
        if CALC_ALL: enigmash.write('--calc-all ')
        enigmash.write('${OUTDIR} ${FAMAP} \n')

    if ENGIMASTEP == 'concat':
        enigmash.write('OUTDIR=' + output_dir + ' \n')
        ## add the engima-concat command
        enigmash.write('\ndm-proc-enigma-concat.py ${OUTDIR} "FA"'
                '"${OUTDIR}/enigmaDTI-FA-results.csv"\n')
        if CALC_MD | CALC_ALL:
             enigmash.write('\ndm-proc-enigma-concat.py ${OUTDIR} "MD"'
                    '"${OUTDIR}/enigmaDTI-MD-results.csv"\n')
        if CALC_ALL:
             enigmash.write('\ndm-proc-enigma-concat.py ${OUTDIR} "AD"'
                    '"${OUTDIR}/enigmaDTI-AD-results.csv"\n')
             enigmash.write('\ndm-proc-enigma-concat.py ${OUTDIR} "RD"'
                    '"${OUTDIR}/enigmaDTI-RD-results.csv"\n')
        # now with a qc step
        enigmash.write('\ndm-qc-enigma.py ')
        if CALC_MD: enigmash.write('--calc-MD ')
        if CALC_ALL: enigmash.write('--calc-all ')
        enigmash.write('${OUTDIR} \n')

    #and...don't forget to close the file
    enigmash.close()
    os.chmod(filename, 0o755)


### check the template .sh file that gets submitted to the queue to make sure
## option haven't changed
def check_runsh(filename, output_dir, CALC_MD, CALC_ALL, DEBUG):
    """
    write a temporary (run.sh) file and than checks it againts the run.sh file
    already there. This is used to double check that the pipeline is not being
    called with different options
    """
    with make_temp_directory() as temp_dir:
        tmprunsh = os.path.join(temp_dir,os.path.basename(filename))
        write_run_script(tmprunsh, output_dir, CALC_MD, CALC_ALL)
        if filecmp.cmp(filename, tmprunsh):
            logger.debug("{} already written - using it".format(filename))
        else:
            # If the two files differ - then we use difflib package to print differences to screen
            logger.debug('#############################################################\n')
            logger.debug('# Found differences in {} these are marked with (+) '.format(filename))
            logger.debug('#############################################################')
            with open(filename) as f1, open(tmprunsh) as f2:
                differ = difflib.Differ()
                logger.debug(''.join(differ.compare(f1.readlines(), f2.readlines())))
            sys.exit("\nOld {} doesn't match parameters of this run....Exiting".format(filename))

def subject_previously_completed(output_dir, subid, FA_img):
    edti_completed = os.path.join(output_dir, subid, 'ROI',
                                  '{}skel_ROIout_avg.csv'.format(
                                  FA_img.replace('.nii','').replace('.gz','')))
    if os.path.isfile(edti_completed):
        return True
    return False

@contextlib.contextmanager
def make_temp_directory():
    temp_dir = tempfile.mkdtemp()
    try:
        yield temp_dir
    finally:
        shutil.rmtree(temp_dir)

## runs the main function
if __name__ == "__main__":
    main()
