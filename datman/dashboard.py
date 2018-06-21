"""Functions for interacting with the dashboard database"""
import logging
import dashboard
#from dashboard.models import Study, Session, Scan, ScanType
import datman.scanid
import datman.utils
import datman.config
from datetime import datetime
from datman.exceptions import DashboardException
from sqlalchemy import exc

logger = logging.getLogger(__name__)
db = dashboard.db
Study = dashboard.models.Study
Session = dashboard.models.Session
Scan = dashboard.models.Scan
ScanType = dashboard.models.ScanType
Session_Scan = dashboard.models.Session_Scan


class dashboard(object):
    study = None
    def __init__(self, study):
        self.set_study(study)

    def set_study(self, study):
        """Sets the object study"""
        cfg = datman.config.config()
        study_name = cfg.map_xnat_archive_to_project(study)
        qry = Study.query.filter(Study.nickname == study_name)
        if qry.count() < 1:
            logger.error('Study:{} not found in dashboard')
            raise DashboardException("Study not found")
        self.study = qry.first()

    def get_add_session(self, session_name, date=None, create=False):
        """Returns a session object, creates one if doesnt exist and create
        is True
        N.B. a session name is ID without timepoint"""
        if not self.study:
            logger.error('Study not set')
            return DashboardException('Study not set')

        try:
            ident = datman.scanid.parse(session_name)
        except datman.scanid.ParseException:
            logger.error('Invalid session:{}'.format(session_name))
            raise DashboardException('Invalid session name:{}'
                                      .format(session_name))

        dashboard_site = [site for site
                          in self.study.sites
                          if site.name == ident.site]
        if not dashboard_site:
            logger.error('Invalid site:{} in session:{}'
                         .format(ident.site, session_name))
            raise DashboardException('Invalid site')

        if date:
            try:
                date = datetime.strptime(date, '%Y-%m-%d')
            except ValueError:
                logger.error('Invalid date:{} for session:{}'
                             .format(date, session_name))
                raise DashboardException('Invalid date')

        qry = Session.query.filter(Session.study == self.study).filter(Session.name == session_name)

        if qry.count() == 1:
            logger.info('Found session:{}'.format(session_name))
            dashboard_session = qry.first()
            if date:
                db_session_date = ''
                xnat_session_date = ''
                try:
                    db_session_date = datetime.strftime(dashboard_session.date,
                                                        '%Y-%m-%d')
                except TypeError:
                    logger.debug('Failed parsing db_date for session:{}'
                                 .format(session_name))
                    pass
                try:
                    xnat_session_date = datetime.strftime(date, '%Y-%m-%d')
                except TypeError:
                    logger.debug('Failed parsing xnat date for session'
                                 .format(session_name))
                    pass
                if not db_session_date == xnat_session_date:
                    logger.debug('Updating date for session:{}'
                                 .format(session_name))
                    dashboard_session.date = date
                    db.session.add(dashboard_session)

        elif qry.count() < 1:
            logger.info("Session:{} doesnt exist".format(session_name))
            if create:
                logger.debug('Creating session:{}'.format(session_name))
                dashboard_session = Session()
                dashboard_session.site = dashboard_site[0]
                dashboard_session.name = session_name
                dashboard_session.study = self.study
                dashboard_session.date = date
                dashboard_session.is_repeated = False
                dashboard_session.repeat_count = 1
                if datman.scanid.is_phantom(session_name):
                    dashboard_session.is_phantom = True
                db.session.add(dashboard_session)
            else:
                return None
        # check for cheklist comments:
        try:
            cl_comment = datman.utils.check_checklist(session_name,
                                                      study=self.study.nickname)
        except ValueError as e:
            logger.error('Failed to check checklist for session:'
                         '{} with error:{}'.format(session_name, str(e)))
        if cl_comment and not cl_comment == dashboard_session.cl_comment:
            try:
                dashboard_session.cl_comment = cl_comment
            except Exception:
                logger.error('Failed updating db comment for session:{}'
                             .format(session_name))

        try:
            db.session.commit()
        except Exception as e:
            logger.error('An error occured adding session:{} to the database'
                         ' Error:{}'
                         .format(session_name, str(e)))
            return None
        return dashboard_session

    def get_add_scan(self, scan_name, create=False):
        """Returns a scan object, creates one if doesnt exist and create
        is True"""
        if not self.study:
            logger.error('Study not set')
            raise DashboardException('Study not set')

        try:
            ident, tag, series, desc = datman.scanid.parse_filename(scan_name)
        except datman.scanid.ParseException as e:
            logger.error('Invalid scan name:{}'.format(scan_name))
            raise DashboardException('Invalid scan_name')
        scan_id = '{}_{}_{}'.format(str(ident), tag, series)
        session_name = ident.get_full_subjectid_with_timepoint()

        qry = db.session.query(Scan) \
                        .join(Session_Scan) \
                        .join(Session) \
                        .join(Study, Session.study) \
                        .filter(Session.name == session_name) \
                        .filter(Study.nickname == self.study.nickname) \
                        .filter(Scan.name == scan_id)
        if ident.session:
            qry = qry.filter(Scan.repeat_number == int(ident.session))

        if qry.count() == 1:
            logger.debug('Found scan:{} in database'.format(scan_name))
            dashboard_scan = qry.first()

        elif qry.count() > 1:
            logger.error('Scan:{} was not uniquely identified in the database'
                         .format(scan_name))
            raise DashboardException('Scan not unique')

        else:
            if not create:
                logger.info('Scan:{} not found but create is false, skipping'
                            .format(scan_name))
                return
            try:
                dashboard_session = self.get_add_session(session_name,
                                                         create=create)
            except DashboardException as e:
                raise(e)

            try:
                dashboard_scantype = self.get_scantype(tag)
            except DashboardException as e:
                raise(e)

            if not dashboard_scantype in self.study.scantypes:
                logger.error('Scantype:{} not valid for study:{}'
                             .format(dashboard_scantype.name,
                                     self.study.nickname))
                raise DashboardException('Invalid scantype')

            dashboard_scan = Scan()
            dashboard_scan.name = scan_id
            dashboard_scan.series_number = series
            dashboard_scan.scantype = dashboard_scantype
            dashboard_scan.description = desc

            if ident.session:
                dashboard_scan.repeat_number = int(ident.session)

            db.session.add(dashboard_scan)
            # need to flush changes to the db to get the primary key scan.id
            # flushing isn't a commit, so entering the session_scan link fails
            # the whole transaction, including adding the scan will rollback
            # note - the session will still have been added as this is a
            # seperate transaction
            db.session.flush()

            dashboard_session_scan_link = Session_Scan()
            dashboard_session_scan_link.scan_id = dashboard_scan.id
            dashboard_session_scan_link.session_id = dashboard_session.id
            # Anything entered this way is a primary scan, linked scans should
            # come from dm-link-project-scans.py
            dashboard_session_scan_link.is_primary = True
            dashboard_session_scan_link.scan_name = scan_id

            db.session.add(dashboard_session_scan_link)
        # finally check the blacklist
        try:
            bl_comment = datman.utils.check_blacklist(scan_name,
                                                      study=self.study.nickname)
        except ValueError as e:
            logger.error('Failed to check blacklist for scan:{} with error:{}'
                         .format(scan_name, str(e)))

        try:
            if not bl_comment and not bl_comment == dashboard_scan.bl_comment:
                # this shouldn't happen but is possible
                logger.error('Scan:{} has a blacklist comment in dashboard db'
                             ' which is not present in metadata/blacklist.csv.'
                             ' Comment:{}'.format(dashboard_scan.name,
                                                  dashboard_scan.bl_comment))
            elif bl_comment and not bl_comment == dashboard_scan.bl_comment:
                dashboard_scan.bl_comment = bl_comment

            db.session.commit()
        except Exception as e:
            logger.error('An error occured adding scan:{} to the db.Error:{}'
                         .format(scan_name, str(e)))
            raise DashboardException
        return(dashboard_scan)

    def delete_extra_scans(self, session_label, scanlist):
        """Checks scans associated with session,
        deletes scans not in scanlist.

        Sorry about this, but watch for the difference between:
        db.session - the database session
            and
        db_session - a session (visit) object in the database

        """
        try:
            ident = datman.scanid.parse(session_label)
        except datman.scanid.ParseException:
            logger.error('Invalid session:{}'.format(session_label))
            raise DashboardException('Invalid session name:{}'
                                      .format(session_label))

        # extract the repeat number
        if datman.scanid.is_phantom(session_label):
            repeat = None
        else:
            repeat = int(ident.session)

        session_label = ident.get_full_subjectid_with_timepoint()
        db_session = self.get_add_session(session_label)
        scan_names = []
        # need to convert full scan names to scanid's in the db
        for scan_name in scanlist:
            try:
                db_scan = self.get_add_scan(scan_name)
                scan_names.append(db_scan.name)
            except:
                continue

        # Need to filter out linked scans and spirals (which are also links,
        # but are considered 'primary' in the database).
        source_scans = list(filter(lambda x: not is_linked(x), db_session.scans))
        # need to get the scan objects from the session_scan links
        session_scans = [link.scan for link in source_scans]
        db_scans = [scan.name for scan in session_scans if scan.repeat_number == repeat]
        extra_scans = set(db_scans) - set(scan_names)

        for scan in extra_scans:
            db_scan = Scan.query.filter(Scan.name == scan).first()
            db_session_scan_link = Session_Scan.query.filter(Session_Scan.scan_id == db_scan.id,
                                                             Session_Scan.session_id == db_session.id)
            db.session.delete(db_session_scan_link.first())
            db.session.delete(db_scan)
        db.session.commit()

    def get_scantype(self, scantype):
        qry = ScanType.query.filter(ScanType.name == scantype)
        if qry.count() < 1:
            logger.error('Scantype:{} not found in database'.format(scantype))
            raise DashboardException('Invalid scantype')
        else:
            return qry.first()

    def delete_session(self, session_name):
        session = self.get_add_session(session_name, create=False)
        try:
            session.delete()
        except AttributeError:
            logger.error("Cannot delete session {}, does not exist.".format(session_name))
            return False
        except Exception as e:
            logger.error('An error occured deleting session:{} from the database'
                         ' Error:{}'
                         .format(session_name, str(e)))
            return False
        return True

def is_linked(scan):
    if not scan.is_primary:
        return True
    # Ugh, sorry about the naming. Result of the database query :(
    scan_type = scan.scan.scantype
    # Sort of hacky way of identifying spirals. Need a refactor to really fix
    # this whole issue. Again, sorry.
    if scan_type.name == 'SPRL':
        return True
    return False

def get_add_session_scan_link(target_session, scan, new_name=None, is_primary=False):
    """
    Creates an entry in the Session_Scans table, linking a scan to session
    """
    qry = Session_Scan.query.filter(Session_Scan.session == target_session,
                                    Session_Scan.scan == scan)

    if qry.count() == 1:
        return qry.first()

    link = Session_Scan()
    link.scan = scan
    link.session = target_session
    if new_name:
        link.scan_name = new_name
    else:
        link.scan_name = scan.name
    link.is_primary = False
    db.session.add(link)
    db.session.commit()
    return link
