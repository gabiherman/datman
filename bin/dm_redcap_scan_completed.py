#!/usr/bin/env python
"""
Add MR comments from the Scan Completed instrument on REDCap to the database.

Usage:
    dm_redcap_scan_completed.py [options] <study>

Arguments:
    <study>             Name of the study to process

Options:
    -q --quiet          Less logging
    -v --verbose        Verbose logging
    -d --debug          Debug logging
"""

import os
import sys
import requests
import logging

from docopt import docopt

import datman.config
import datman.scanid
import datman.dashboard as dashboard

logger = logging.getLogger(os.path.basename(__file__))


def read_token(token_file):
    if not os.path.isfile(token_file):
        logger.error('REDCap token file: {} not found'.format(token_file))
        raise IOError

    with open(token_file, 'r') as token_file:
        token = token_file.readline().strip()

    return token


def get_records(api_url, token, instrument):
    payload = {'token': token,
               'content': 'record',
               'forms': instrument,
               'format': 'json',
               'type': 'flat',
               'rawOrLabel': 'raw',
               'fields': 'record_id'}
    response = requests.post(api_url, data=payload)

    #http status code 200 indicates a successful request, everything else is an error.
    if response.status_code != 200:
        raise Exception('API request failed. HTTP status code: {}.  Reason: {}'.format(
        response.status_code,response.text))

    return response.json()


def get_version(api_url, token):
    payload = {'token': token,
               'content': 'version'}
    response = requests.post(api_url, data=payload)
    version = response.content
    return version


def add_session_redcap(record, subj_val, date_field, redcap_comments, event_key, redcap_project, redcap_url, instrument, redcap_version):
    record_id = record['record_id']
    subject_id = record[subj_val].upper()
    if not datman.scanid.is_scanid(subject_id):
        try:
            subject_id = subject_id + '_01'
            datman.scanid.is_scanid(subject_id)
        except:
            logger.error('Invalid session: {}, skipping'.format(subject_id))
            return
    try:
        ident = datman.scanid.parse(subject_id)
    except datman.scanid.ParseException:
        logger.error('Invalid session: {}, skipping'.format(subject_id))
        return

    session_date = record[date_field]

    try:
        session = dashboard.get_session(ident, date=session_date, create=True)
    except datman.exceptions.DashboardException as e:
        logger.error('Failed adding session {} to dashboard. Reason: {}'.format(
                ident, e))
        return

    try:
        session.add_redcap(record_id, redcap_project, redcap_url, instrument,
                date=session_date,
                comment=record[redcap_comments],
                event_id=event_key[record['redcap_event_name']],
                version=redcap_version)
    except:
        logger.error('Failed adding REDCap info for session {} to dashboard'.format(ident))


def main():
    arguments = docopt(__doc__)
    study = arguments['<study>']
    quiet = arguments['--quiet']
    verbose = arguments['--verbose']
    debug = arguments['--debug']

    # setup logging
    ch = logging.StreamHandler(sys.stdout)
    log_level = logging.WARN

    if quiet:
        log_level = logging.ERROR
    if verbose:
        log_level = logging.INFO
    if debug:
        log_level = logging.DEBUG

    logger.setLevel(log_level)
    ch.setLevel(log_level)

    formatter = logging.Formatter('%(asctime)s - %(name)s - {study} - '
                                  '%(levelname)s - %(message)s'.format(
                                       study=study))
    ch.setFormatter(formatter)
    logger.addHandler(ch)
    logging.getLogger('datman.utils').addHandler(ch)
    logging.getLogger('datman.dashboard').addHandler(ch)

    # setup the config object
    cfg = datman.config.config(study=study)

    # get paths
    dir_meta = cfg.get_path('meta')

    # configure redcap variables
    api_url = cfg.get_key('REDCAP_URL')
    redcap_url = api_url.replace('/api/', '/')

    token_path = os.path.join(dir_meta, cfg.get_key('REDCAP_TOKEN'))
    token = read_token(token_path)

    redcap_project = cfg.get_key('REDCAP_PROJECTID')
    instrument = cfg.get_key('REDCAP_INSTRUMENT')
    date_field = cfg.get_key('REDCAP_DATE')
    status_field = cfg.get_key('REDCAP_STATUS')
    status_val = cfg.get_key('REDCAP_STATUS_VALUE')
    subj_val = cfg.get_key('REDCAP_SUBJ')
    redcap_comments = cfg.get_key('REDCAP_COMMENTS')
    event_key = cfg.get_key('REDCAP_EVENTID')


    #make status_val into a list
    if not (isinstance(status_val,list)):
        status_val=[status_val]

    redcap_version = get_version(api_url, token)

    response_json = get_records(api_url, token, instrument)

    project_records = []
    for item in response_json:
        # only grab records where instrument has been marked complete
        if not (item[date_field] and item[status_field] in status_val):
            continue
        project_records.append(item)

    for record in project_records:
        add_session_redcap(record, subj_val, date_field, redcap_comments, event_key, redcap_project, redcap_url, instrument, redcap_version)


if __name__ == '__main__':
    main()
