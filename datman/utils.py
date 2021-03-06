"""
A collection of utilities for generally munging imaging data.
"""
import os
import sys
import re
import io
import glob
import zipfile
import tarfile
import logging
import random
import time
import tempfile
import shutil
import contextlib
import subprocess as proc

import pydicom as dcm
import pyxnat

import datman.config
import datman.scanid as scanid
import datman.dashboard as dashboard
from datman.exceptions import MetadataException

logger = logging.getLogger(__name__)

def locate_metadata(filename, study=None, subject=None, config=None, path=None):
    if not (path or study or config or subject):
        raise MetadataException("Can't locate metadata file {} without either "
                "1) a full path to the file 2) a study or subject ID or "
                "3) a datman.config object".format(filename))

    if path:
        file_path = path
    else:
        if not config:
            given_study = subject or study
            config = datman.config.config(study=given_study)
        file_path = os.path.join(config.get_path('meta'), filename)

    return file_path


def read_checklist(study=None, subject=None, config=None, path=None):
    """
    This function is used to look-up QC checklist entries. If the dashboard is
    found it will ONLY check the dashboard database, otherwise it expects a
    datman style 'checklist' file on the filesystem.

    This function can accept either:
        1) A study name (nickname, not the study tag) or subject ID (Including
           a session number)
        2) A datman config object, initialized to the study being worked with
        3) A full path directly to a checklist file (Will circumvent the
           dashboard database check and ignore any datman config files)

    Returns:
        - A dictionary of subject IDs mapped to their comment / name of the
          person who signed off on their data
        - OR the comment for a specific subject if a subject ID is given
        - OR 'None' if a specific subject ID is given and they're not found
          in the list
    """
    if not (study or subject or config or path):
        raise MetadataException("Can't read dashboard checklist "
                "contents without either 1) a subject or study ID 2) a "
                "datman.config object or 3) a full path to the checklist")

    if subject:
        ident = datman.scanid.parse(subject)

    if dashboard.dash_found and not path:
        if subject:
            subject = ident.get_full_subjectid_with_timepoint_session()
        try:
            entries = _fetch_checklist(subject=subject, study=study,
                    config=config)
        except Exception as e:
            raise MetadataException("Can't retrieve checklist information "
                    "from dashboard database. Reason - {}".format(str(e)))
        return entries

    logger.info("Dashboard not found, attempting to find a checklist "
            "metadata file instead.")
    checklist_path = locate_metadata('checklist.csv', path=path,
            subject=subject, study=study, config=config)

    if subject:
        subject = ident.get_full_subjectid_with_timepoint()

    try:
        with open(checklist_path, 'r') as checklist:
            entries = _parse_checklist(checklist,
                    subject=subject)
    except Exception as e:
        raise MetadataException("Failed to read checklist file "
                "{}. Reason - {}".format(checklist_path, str(e)))

    return entries


def _fetch_checklist(subject=None, study=None, config=None):
    """
    Support function for read_checklist(). Gets a list of existing / signed off
    sessions from the dashboard.

    The checklist.csv file dropped the session number, so only information on
    the first session is reported to maintain consistency. :(

    Returns a dictionary formatted like that of '_parse_checklist' or a string
    comment if the 'subject' argument was given
    """
    if not (subject or study or config):
        raise MetadataException("Can't retrieve dashboard checklist "
                "contents without either 1) a subject or study ID 2) a "
                "datman.config object")

    if subject:
        session = dashboard.get_session(subject)
        if not session:
            return
        if session.signed_off:
            return str(session.reviewer)
        return ''


    if config and not study:
        study = config.study_name

    db_study = dashboard.get_project(study)
    entries = {}
    for timepoint in db_study.timepoints:
        if timepoint.is_phantom or not len(timepoint.sessions):
            continue
        session = timepoint.sessions.values()[0]
        if session.signed_off:
            comment = str(session.reviewer)
        else:
            comment = ''
        str_name = timepoint.name.encode('utf-8')
        entries[str_name] = comment

    return entries


def _parse_checklist(checklist, subject=None):
    """
    Support function for read_checklist(). Gets a list of existing / signed off
    sessions from a checklist.csv file.

    The 'checklist' argument is expected to be a handler for an already opened
    file.

    Returns: A dictionary of subject IDs (minus session/repeat num) mapped to
    their QC comments (or an empty string if it's a new entry). Or a single
    comment string if the 'subject' option was used
    """
    if subject:
        entries = None
    else:
        entries = {}

    for line in checklist.readlines():
        fields = line.split()
        if not fields:
            # Ignore blank lines
            continue
        try:
            subid = os.path.splitext(fields[0].replace('qc_', ''))[0]
        except:
            raise MetadataException("Found malformed checklist entry: "
                    "{}".format(line))
        try:
            datman.scanid.parse(subid)
        except:
            logger.error("Found malformed subject ID {} in checklist. "
                    "Ignoring.".format(subid))
            continue

        if entries and subid in entries:
            logger.info("Found duplicate checklist entries for {}. Ignoring "
                    "all except the first entry found.".format(subid))
            continue

        comment = " ".join(fields[1:]).strip()
        if subject:
            if subid != subject:
                continue
            return comment
        else:
            entries[subid] = comment

    return entries


def update_checklist(entries, study=None, config=None, path=None):
    """
    Handles QC checklist updates. Will preferentially update the dashboard
    (ignoring any 'checklist.csv' files) unless the dashboard is not installed
    or a specific path is given to a file.

    <entries> should be a dictionary with subject IDs (minus session/repeat) as
    the keys and qc entries as the value (with an empty string for new/blank
    QC entries)

    This will raise a MetadataException if any part of the update fails for
    any entry.
    """
    if not isinstance(entries, dict):
        raise MetadataException("Checklist entries must be in dictionary "
                "format with subject ID as the key and comment as the value "
                "(empty string for new, unreviewed subjects)")

    if dashboard.dash_found and not path:
        _update_qc_reviewers(entries)
        return

    # No dashboard, or path was given, so update file system.
    checklist_path = locate_metadata('checklist.csv', study=study,
            config=config, path=path)
    old_entries = read_checklist(path=checklist_path)

    # Merge with existing list
    for subject in entries:
        try:
            ident = datman.scanid.parse(subject)
        except:
            raise MetadataException("Attempt to add invalid subject ID {} to "
                    "QC checklist".format(subject))
        subject = ident.get_full_subjectid_with_timepoint()
        old_entries[subject] = entries[subject]

    # Reformat to expected checklist line format
    lines = ["qc_{}.html {}\n".format(sub, old_entries[sub])
            for sub in old_entries]

    write_metadata(sorted(lines), checklist_path)


def _update_qc_reviewers(entries):
    """
    Support function for update_checklist(). Updates QC info on the dashboard.
    """
    try:
        user = dashboard.get_default_user()
    except:
        raise MetadataException("Can't update dashboard QC information without "
                "a default dashboard user defined. Please add "
                "'DEFAULT_DASH_USER' to your config file.")

    for subject in entries:
        timepoint = dashboard.get_subject(subject)
        if not timepoint or not timepoint.sessions:
            raise MetadataException("{} not found in the in the dashboard "
                    "database.".format(subject))

        comment = entries[subject]
        if not comment:
            # User was just registering a new QC entry. As long as the
            # session exists in the database there is no work to do.
            continue

        for num in timepoint.sessions:
            session = timepoint.sessions[num]
            if session.is_qcd():
                # Dont risk writing over QC-ers from the dashboard.
                continue
            session.sign_off(user.id)


def read_blacklist(study=None, scan=None, subject=None, config=None, path=None):
    """
    This function is used to look up blacklisted scans. If the dashboard is
    found it ONLY checks the dashboard database. Otherwise it expects a datman
    style 'blacklist' file on the filesystem.

    This function can accept:
        - A study name (nickname, not study tag)
        - A scan name (may include the full path and extension)
        - A subject ID
        - A datman config object, initialized to the study being worked with
        - A full path directly to a blacklist file. If given, this will
           circumvent any dashboard database checks and ignore any datman
           config files.

    Returns:
        - A dictionary of scan names mapped to the comment provided when they
          were blacklisted (Note: If reading from the filesystem, commas
          contained in comments will be removed)
        - OR a dictionary of the same format containing only entries
          for a single subject if a specific subject ID was given
        - OR the comment for a specific scan if a scan is given
        - OR 'None' if a scan is given but not found in the blacklist
    """
    if dashboard.dash_found and not path:
        return _fetch_blacklist(scan=scan, subject=subject, study=study,
                config=config)

    if scan:
        try:
            ident, tag, series, descr = scanid.parse_filename(scan)
        except:
            logger.error("Invalid scan name: {}".format(scan))
            return
        tmp_sub = ident.get_full_subjectid_with_timepoint_session()
        # Need to drop the path and extension if in the original 'scan'
        scan = "_".join([str(ident), tag, series, descr])
    else:
        tmp_sub = subject

    blacklist_path = locate_metadata("blacklist.csv", study=study,
            subject=tmp_sub, config=config, path=path)
    try:
        with open(blacklist_path, 'r') as blacklist:
            entries = _parse_blacklist(blacklist, scan=scan, subject=subject)
    except Exception as e:
        raise MetadataException("Failed to read checklist file {}. Reason - "
                "{}".format(blacklist_path, str(e)))

    return entries


def _fetch_blacklist(scan=None, subject=None, study=None, config=None):
    """
    Helper function for 'read_blacklist()'. Gets the blacklist contents from
    the dashboard's database
    """
    if not (scan or subject or study or config):
        raise MetadataException("Can't retrieve dashboard blacklist info "
            "without either 1) a scan name 2) a subject ID 3) a study ID or "
            "4) a datman config object")

    if scan:
        db_scan = dashboard.get_scan(scan)
        if db_scan and db_scan.blacklisted():
            try:
                return db_scan.get_comment().encode('utf-8')
            except:
                return db_scan.get_comment()
        return

    if subject:
        db_subject = dashboard.get_subject(subject)
        blacklist = db_subject.get_blacklist_entries()
    else:
        if config:
            study = config.study_name
        db_study = dashboard.get_project(study)
        blacklist = db_study.get_blacklisted_scans()

    entries = {}
    for entry in blacklist:
        scan_name = str(entry.scan) + "_" + entry.scan.description
        try:
            scan_name = scan_name.encode('utf-8')
            comment = entry.comment.encode('utf-8')
        except:
            comment = entry.comment
        entries[scan_name] = comment

    return entries


def _parse_blacklist(blacklist, scan=None, subject=None):
    """
    Helper function for 'read_blacklist()'. Gets the blacklist contents from
    the file system
    """
    if scan:
        entries = None
    else:
        entries = {}

    # This will mangle any commas in comments, but is the most reliable way
    # to split the lines
    regex = ',|\s'
    for line in blacklist:
        fields = re.split(regex, line.strip())
        try:
            scan_name = fields[0]
            datman.scanid.parse_filename(scan_name)
            comment = fields[1:]
        except:
            logger.info("Ignoring malformed line: {}".format(line))
            continue

        comment = " ".join(comment).strip()

        if scan_name == 'series':
            continue

        if scan:
            if scan_name == scan:
                return comment
            continue

        if subject and not scan_name.startswith(subject):
            continue

        if entries and scan_name in entries:
            logger.info("Found duplicate blacklist entries for {}. Ignoring "
                    "all except the first entry found.".format(scan_name))
            continue
        entries[scan_name] = comment

    return entries


def update_blacklist(entries, study=None, config=None, path=None):
    if not isinstance(entries, dict):
        raise MetadataException("Blacklist entries must be in dictionary "
                "format with scan name as the key and reason for blacklisting "
                "as the value")

    if dashboard.dash_found and not path:
        _update_scan_checklist(entries)
        return

    blacklist_path = locate_metadata('blacklist.csv', study=study,
            config=config, path=path)
    old_entries = read_blacklist(path=blacklist_path)

    for scan_name in entries:
        try:
            datman.scanid.parse_filename(scan_name)
        except:
            raise MetadataException("Attempt to add invalid scan name {} "
                    "to blacklist".format(scan_name))
        if not entries[scan_name]:
            logger.error("Can't add blacklist entry with empty comment. "
                    "Skipping {}".format(scan_name))
            continue
        old_entries[scan_name] = entries[scan_name]

    lines = ["{} {}\n".format(sub, old_entries[sub]) for sub in old_entries]
    new_list = ['series\treason\n']
    new_list.extend(sorted(lines))
    write_metadata(new_list, blacklist_path)


def _update_scan_checklist(entries):
    """
    Helper function for 'update_blacklist()'. Updates the dashboard's database.
    """
    try:
        user = dashboard.get_default_user()
    except:
        raise MetadataException("Can't update dashboard QC information without "
                "a default dashboard user defined. Please add "
                "'DEFAULT_DASH_USER' to your config file.")

    for scan_name in entries:
        scan = dashboard.get_scan(scan_name)
        if not scan:
            raise MetadataException("{} does not exist in the dashboard "
                    "database".format(scan_name))
        scan.add_checklist_entry(user.id, comment=entries[scan_name],
                sign_off=False)


def write_metadata(lines, path, retry=3):
    """
    Repeatedly attempts to write lines to <path>. The destination file
    will be overwritten with <lines> so any contents you wish to preserve
    should be contained within the list.
    """
    if not retry:
        raise MetadataException("Failed to update {}".format(path))

    try:
        with open(path, "w") as meta_file:
            meta_file.writelines(lines)
    except:
        logger.error("Failed to write metadata file {}. Tries "
                "remaining - {}".format(path, retry))
        wait_time = random.uniform(0, 10)
        time.sleep(wait_time)
        write_metadata(lines, path, retry=retry-1)


def get_subject_metadata(config=None, study=None):
    if not config:
        if not study:
            raise MetadataException("A study name or config object must be "
                    "given to locate study metadata.")
        config = datman.config.config(study=study)

    checklist = read_checklist(config=config)
    blacklist = read_blacklist(config=config)

    all_qc = {subid: [] for subid in checklist if checklist[subid]}
    for bl_entry in blacklist:
        try:
            ident, _, _, _ = datman.scanid.parse_filename(bl_entry)
        except:
            logger.error("Malformed scan name {} found in blacklist. "
                    "Ignoring.".format(bl_entry))
            continue

        subid = ident.get_full_subjectid_with_timepoint()
        try:
            all_qc[subid]
        except KeyError:
            logger.error("{} has blacklisted series {} but does not "
                    "appear in QC checklist. Ignoring blacklist entry".format(
                    subid, bl_entry))
            continue

        all_qc[subid].append(bl_entry)

    return all_qc


def get_extension(path):
    """
    Get the filename extension on this path.

    This is a slightly more sophisticated version of os.path.splitext in that
    this will correctly return the extension for '.tar.gz' files, for example.
    :D
    """
    if path.endswith('.tar.gz'):
        return '.tar.gz'
    if path.endswith('.nii.gz'):
        return '.nii.gz'
    else:
        return os.path.splitext(path)[1]


def get_archive_headers(path, stop_after_first = False):
    """
    Get dicom headers from a scan archive.

    Path can be a path to a tarball or zip of dicom folders, or a folder. It is
    assumed that this archive contains the dicoms from a single exam, organized
    into folders for each series.

    The entire archive is scanned and dicom headers from a single file in each
    folder are returned as a dictionary that maps path->headers.

    If stop_after_first == True only a single set of dicom headers are
    returned for the entire archive, which is useful if you only care about the
    exam details.
    """
    if os.path.isdir(path):
        return get_folder_headers(path, stop_after_first)
    elif zipfile.is_zipfile(path):
        return get_zipfile_headers(path, stop_after_first)
    elif os.path.isfile(path) and path.endswith('.tar.gz'):
        return get_tarfile_headers(path, stop_after_first)
    else:
        raise Exception("{} must be a file (zip/tar) or folder.".format(path))


def get_tarfile_headers(path, stop_after_first = False):
    """
    Get headers for dicom files within a tarball
    """
    tar = tarfile.open(path)
    members = tar.getmembers()

    manifest = {}
    # for each dir, we want to inspect files inside of it until we find a dicom
    # file that has header information
    for f in filter(lambda x: x.isfile(), members):
        dirname = os.path.dirname(f.name)
        if dirname in manifest: continue
        try:
            manifest[dirname] = dcm.read_file(tar.extractfile(f))
            if stop_after_first: break
        except dcm.filereader.InvalidDicomError as e:
            continue
    return manifest


def get_zipfile_headers(path, stop_after_first = False):
    """
    Get headers for a dicom file within a zipfile
    """
    zf = zipfile.ZipFile(path)

    manifest = {}
    for f in zf.namelist():
        dirname = os.path.dirname(f)
        if dirname in manifest:
            continue
        try:
            manifest[dirname] = dcm.read_file(io.BytesIO(zf.read(f)))
            if stop_after_first:
                break
        except dcm.filereader.InvalidDicomError as e:
            continue
        except zipfile.BadZipfile:
            logger.warning('Error in zipfile:{}'
                           .format(path))
            break
    return manifest


def get_folder_headers(path, stop_after_first=False):
    """
    Generate a dictionary of subfolders and dicom headers.
    """

    manifest = {}

    # for each dir, we want to inspect files inside of it until we find a dicom
    # file that has header information
    subdirs = []
    for filename in os.listdir(path):
        filepath = os.path.join(path, filename)
        try:
            if os.path.isdir(filepath):
                subdirs.append(filepath)
                continue
            manifest[path] = dcm.read_file(filepath)
            break
        except dcm.filereader.InvalidDicomError as e:
            pass

    if stop_after_first:
        return manifest

    # recurse
    for subdir in subdirs:
        manifest.update(get_folder_headers(subdir, stop_after_first))
    return manifest


def get_all_headers_in_folder(path, recurse=False):
    """
    Get DICOM headers for all files in the given path.

    Returns a dictionary mapping path->headers for *all* files (headers == None
    for files that are not dicoms).
    """

    manifest = {}
    for dirname, dirnames, filenames in os.walk(path):
        for filename in filenames:
            filepath = os.path.join(dirname, filename)
            headers = None
            try:
                headers = dcm.read_file(filepath)
            except dcm.filereader.InvalidDicomError as e:
                continue
            manifest[filepath] = headers
        if not recurse:
            break
    return manifest


def subject_type(subject):
    """
    Uses subject naming to determine what kind of files we are looking at. If
    we find a strangely-named subject, we return None.

    TO DEPRICATE.
    """
    try:
        subject = subject.split('_')

        if subject[2] == 'PHA':
            return 'phantom'
        elif subject[2] != 'PHA' and subject[2][0] == 'P':
            return 'humanphantom'
        elif str.isdigit(subject[2]) == True and len(subject[2]) == 4:
            return 'subject'
        else:
            return None

    except:
        return None


def get_subjects(path):
    """
    Finds all of the subject folders in the supplied directory, and returns
    their basenames.
    """
    subjects = filter(os.path.isdir, glob.glob(os.path.join(path, '*')))
    for i, subj in enumerate(subjects):
        subjects[i] = os.path.basename(subj)
    subjects.sort()

    return subjects


def get_phantoms(path):
    """
    Finds all of the phantom folders in the supplied directory, and returns
    their basenames.
    """
    phantoms = []
    subjects = get_subjects(path)
    for subject in subjects:
        subjtype = subject_type(subject)
        if subjtype == 'phantom':
            phantoms.append(subject)

    return phantoms


def get_xnat_catalog(data_path, subject):
    """
    For a given subject, finds and returns all of the xml files as full
    paths. In almost all cases, this will be a single catalog.


    THIS IS BROKEN.
    """
    dicoms = os.listdir(os.path.join(data_path, 'dicom'))
    subjects = filter(lambda x: subject in x, dicoms)

    catalogs = []

    for subject in subjects:
        folders = os.listdir(os.path.join(data_path, 'dicom', subject))
        folders.sort()
        files = os.listdir(os.path.join(data_path, 'dicom', subject, folders[0]))
        files = filter(lambda x: '.xml' in x, files)
        catalogs.append(os.path.join(data_path, 'dicom', subject, folders[0], files[0]))

    catalogs.sort()

    return catalogs


def define_folder(path):
    """
    Sets a variable to be the path to a folder. Also, if the folder does not
    exist, this makes it so, unless we lack the permissions to do so, which
    leads to a graceful exit.
    """
    if not os.path.isdir(path):
        try:
            os.makedirs(path)
        except OSError as e:
            logger.error('failed to make directory {}'.format(path))
            raise(e)

    if not has_permissions(path):
        raise OSError("User does not have permission to access {}".format(path))

    return path


def has_permissions(path):
    """
    Checks for write access to submitted path.
    """
    if os.access(path, 7):
        flag = True
    else:
        logger.error('You do not have write access to path {}'.format(path))
        flag = False

    return flag


def run_dummy_q(list_of_names):
    """
    This holds the script until all of the queued items are done.
    """
    logger.info('Holding for remaining processes.')
    opts = 'h_vmem=3G,mem_free=3G,virtual_free=3G'
    holds = ",".join(list_of_names)
    cmd = 'qsub -sync y -hold_jid {} -l {} -b y echo'.format(holds, opts)
    run(cmd)
    logger.info('... Done.')


def run(cmd, dryrun=False, specialquote=True, verbose=True):
    """
    Runs the command in default shell, returning STDOUT and a return code.
    The return code uses the python convention of 0 for success, non-zero for
    failure
    """
    # Popen needs a string command.
    if isinstance(cmd, list):
        cmd = " ".join(cmd)

    # perform shell quoting for special characters in filenames
    if specialquote:
        cmd = _escape_shell_chars(cmd)

    if dryrun:
        logger.info("Performing dry-run. Skipped command: {}".format(cmd))
        return 0, ''

    logger.debug("Executing command: {}".format(cmd))

    p = proc.Popen(cmd, shell=True, stdout=proc.PIPE, stderr=proc.PIPE)
    out, err = p.communicate()

    if p.returncode and verbose:
        logger.error('run({}) failed with returncode {}. STDERR: {}'
                     .format(cmd, p.returncode, err))

    return p.returncode, out


def _escape_shell_chars(arg):
    """
    An attempt to sanitize shell arguments without disabling
    shell expansion.

    >>> _escape_shell_chars('This (; file has funky chars')
    'This \\(\\; file has funky chars'
    """
    arg = arg.replace('(', '\\(')
    arg = arg.replace(';', '\\;')
    arg = arg.replace(')', '\\)')

    return(arg)


def get_files_with_tag(parentdir, tag, fuzzy=False):
    """
    Returns a list of files that have the specified tag.

    Filenames must conform to the datman naming convention (see
    scanid.parse_filename) in order to be considered.

    If fuzzy == True, then filenames are matched if the given tag is found
    within the filename's tag.
    """

    files = []
    for f in os.listdir(parentdir):
        try:
            _, filetag, _, _ = scanid.parse_filename(f)
            if tag == filetag or (fuzzy and tag in filetag):
                files.append(os.path.join(parentdir, f))
        except scanid.ParseException:
            continue

    return files


def makedirs(path):
    """
    Make the directory (including parent directories) if they don't exist
    """
    if not os.path.exists(path):
        os.makedirs(path)


def check_returncode(returncode):
    if returncode != 0:
        raise ValueError


def get_loaded_modules():
    """Returns a space separated list of loaded modules

    These are modules loaded by the environment-modules system. This function
    just looks in the LOADEDMODULES environment variable for the list.
    """
    return " ".join(os.environ.get("LOADEDMODULES", "").split(":"))


def splitext(path):
    """
    Function that will remove extension, including specially-defined extensions
    that fool os.path.splitext
    """
    for ext in ['.nii.gz', '.mnc.gz']:
        if path.endswith(ext):
            return path[:-len(ext)], path[-len(ext):]
    return os.path.splitext(path)


@contextlib.contextmanager
def make_temp_directory(suffix='', prefix='tmp', path=None):
    temp_dir = tempfile.mkdtemp(suffix=suffix, prefix=prefix, dir=path)
    try:
        yield temp_dir
    finally:
        shutil.rmtree(temp_dir)


def remove_empty_files(path):
    for root, dirs, files in os.walk(path):
        for f in files:
            filename = os.path.join(root, f)
            if os.path.getsize(filename) == 0:
                os.remove(filename)


def nifti_basename(fpath):
    """
    return basename without extension (either .nii.gz or .nii)
    """
    basefpath = os.path.basename(fpath)
    stem = basefpath.replace('.nii', '').replace('.gz', '')

    return(stem)


def filter_niftis(candidates):
    """
    Takes a list and returns all items that contain the extensions '.nii' or '.nii.gz'.
    """
    candidates = filter(lambda x: 'nii.gz' == '.'.join(x.split('.')[1:]) or
                                     'nii' == '.'.join(x.split('.')[1:]), candidates)

    return candidates


def split_path(path):
    """
    Splits a path into all the component parts, returns a list

    >>> split_path('a/b/c/d.txt')
    ['a', 'b', 'c', 'd.txt']
    """
    dirname = path
    path_split = []
    while True:
        dirname, leaf = os.path.split(dirname)
        if (leaf):
            path_split = [leaf] + path_split
        else:
            break
    return(path_split)


class cd(object):
    """
    A context manager for changing directory. Since best practices dictate
    returning to the original directory, saves the original directory and
    returns to it after the block has exited.

    May raise OSError if the given path doesn't exist (or the current directory
    is deleted before switching back)
    """

    def __init__(self, path):
        user_path = os.path.expanduser(path)
        self.new_path = os.path.expandvars(user_path)

    def __enter__(self):
        self.old_path = os.getcwd()
        os.chdir(self.new_path)

    def __exit__(self, e, value, traceback):
        os.chdir(self.old_path)


class XNATConnection(object):
    def __init__(self,  xnat_url, user_name, password):
        self.server = xnat_url
        self.user = user_name
        self.password = password

    def __enter__(self):
        self.connection = pyxnat.Interface(server=self.server, user=self.user,
                password=self.password)
        return self.connection

    def __exit__(self, type, value, traceback):
        self.connection.disconnect()


def get_xnat_credentials(config, xnat_cred):
    if not xnat_cred:
        xnat_cred = os.path.join(config.get_path('meta'), 'xnat-credentials')

    logger.debug("Retrieving xnat credentials from {}".format(xnat_cred))
    try:
        credentials = read_credentials(xnat_cred)
        user_name = credentials[0]
        password = credentials[1]
    except IndexError:
        logger.error("XNAT credential file {} is missing the user name or " \
                "password.".format(xnat_cred))
        sys.exit(1)
    return user_name, password


def read_credentials(cred_file):
    credentials = []
    try:
        with open(cred_file, 'r') as creds:
            for line in creds:
                credentials.append(line.strip('\n'))
    except:
        logger.error("Cannot read credential file or file does not exist: " \
                "{}.".format(cred_file))
        sys.exit(1)
    return credentials


def get_relative_source(source, target):
    if os.path.isfile(source):
        source_file = os.path.basename(source)
        source = os.path.dirname(source)
    else:
        source_file = ''

    rel_source_dir = os.path.relpath(source, os.path.dirname(target))
    rel_source = os.path.join(rel_source_dir, source_file)
    return rel_source


def check_dependency_configured(program_name, shell_cmd=None, env_vars=None):
    """
    <program_name>      Name to add to the exception message if the program is
                        not correctly configured.
    <shell_cmd>         A command line command that will be put into 'which', to
                        check whether the shell can find it.
    <env_vars>          A list of shell variables that are expected to be set.
                        Doesnt verify the value of these vars, only that they are
                        all set.

    Raises EnvironmentError if the command is not findable or if any environment
    variable isnt configured.
    """
    message = ("{} required but not found. Please check that "
            "it is installed and correctly configured.".format(program_name))

    if shell_cmd is not None:
        return_val, found = run('which {}'.format(shell_cmd))
        if return_val or not found:
            raise EnvironmentError(message)

    if env_vars is None:
        return

    if not isinstance(env_vars, list):
        env_vars = [env_vars]

    try:
        for variable in env_vars:
            os.environ[variable]
    except KeyError:
        raise EnvironmentError(message)


def validate_subject_id(subject_id, config):
    """
    Checks that a given subject id
        a) Matches the datman convention
        b) Matches a study tag that is defined in the configuration file for
           the current study
        c) Matches a site that is defined for the given study tag

    If all validation checks pass, will return a datman scanid instance. This
    can be ignored if the validation is all that's wanted.
    """
    try:
        scanid = datman.scanid.parse(subject_id)
    except datman.scanid.ParseException:
        raise RuntimeError("Subject id {} does not match datman"
                " convention".format(subject_id))

    valid_tags = config.get_study_tags()

    try:
        sites = valid_tags[scanid.study]
    except KeyError:
        raise RuntimeError("Subject id {} has undefined study code {}".format(
                subject_id, scanid.study))

    if scanid.site not in sites:
        raise RuntimeError("Subject id {} has undefined site {} for study {}".format(
                subject_id, scanid.site, scanid.study))

    return scanid


def submit_job(cmd, job_name, log_dir, system='other', cpu_cores=1,
        walltime="2:00:00", dryrun=False, partition=None, argslist="",
        workdir="/tmp"):
    '''
    submits a job or joblist the queue depending on the system

    Args:
        cmd                         Command or list of commands to submit
        job_name                    The name for the job
        log_dir                     Path to where the job logs should go
        system                      Current system running (similar to DM_SYSTEM) [default=other]
        cpu_cores                   Number of cores to allocate for job [default=1]
        walltime                    Real clock time for job [default=2:00:00]
        dryrun                      Set to true if you want job to not submit [default=False]
        partition                   Slurm partition. If none specified the default queue will be used
        argslist                    String of additional slurm arguments (etc: --mem X --verbose ...) [default=None]
        workdir                     Location for slurm to use as the work dir [default='/tmp']
    '''
    if dryrun:
        return

    # Bit of an ugly hack to allow job submission on the scc. Should be replaced
    # with drmaa or some other queue interface later
    if system == 'kimel':
        job_file = '/tmp/{}'.format(job_name)

        with open(job_file, 'wb') as fid:
            fid.write('#!/bin/bash\n')
            fid.write(cmd)
            
        arg_list = '-c {cores} -t {walltime} {args} --job-name {jobname} '\
                '-o {log_dir}/{jobname} -D {workdir}'.format(cores=cpu_cores,
                walltime=walltime, args=argslist, jobname=job_name, 
                log_dir=log_dir, workdir=workdir)
        
        if partition:
            arg_list = arg_list + ' -p {} '.format(partition)
            
        job = 'sbatch ' + arg_list + ' {}'.format(job_file)

        rtn, out = run(job)
    else:
        job = "echo {} | qbatch -N {} --logdir {} --ppj {} -i -c 1 -j 1 --walltime {} -".format(
                cmd, job_name, log_dir, cpu_cores, walltime)
        rtn, out = run(job, specialquote=False)

    if rtn:
        logger.error("Job submission failed.")
        if out:
            logger.error("stdout: {}".format(out))
        sys.exit(1)


def get_resources(open_zipfile):
    # filter dirs
    files = open_zipfile.namelist()
    files = filter(lambda f: not f.endswith('/'), files)

    # filter files named like dicoms
    files = filter(lambda f: not is_named_like_a_dicom(f), files)

    # filter actual dicoms :D.
    resource_files = []
    for f in files:
        try:
            if not is_dicom(io.BytesIO(open_zipfile.read(f))):
                resource_files.append(f)
        except zipfile.BadZipfile:
            logger.error('Error in zipfile:{}'.format(f))
    return resource_files


def is_named_like_a_dicom(path):
    dcm_exts = ('dcm', 'img')
    return any(map(lambda x: path.lower().endswith(x), dcm_exts))


def is_dicom(fileobj):
    try:
        dcm.read_file(fileobj)
        return True
    except dcm.filereader.InvalidDicomError:
        return False


def make_zip(source_dir, dest_zip):
    # Can't use shutil.make_archive here because for python 2.7 it fails on
    # large zip files (seemingly > 2GB) and zips with more than about 65000 files
    # Soooo, doing it the hard way. Can change this if we ever move to py3
    with zipfile.ZipFile(dest_zip, "w", compression=zipfile.ZIP_DEFLATED,
            allowZip64=True) as zip_handle:
        # We want this to use 'w' flag, since it should overwrite any existing zip
        # of the same name
        for current_dir, folders, files in os.walk(source_dir):
            for item in files:
                item_path = os.path.join(current_dir, item)
                archive_path = item_path.replace(source_dir + "/", "")
                zip_handle.write(item_path, archive_path)

# vim: ts=4 sw=4 sts=4:
