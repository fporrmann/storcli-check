#!/usr/bin/python3
"""
This script is used to check the state of the MR controller on any system
running Python 3.
"""

# Imports ######################################################################
import os
import re
import sys
import socket
import logging
import smtplib
import zipfile
import subprocess
from getpass import getuser
from optparse import OptionParser
from email.mime.multipart import MIMEMultipart
from email.mime.base import MIMEBase
from email.mime.text import MIMEText
from email import encoders

from dateutil import parser as duParser
from datetime import datetime
from os.path import exists

# Metadata #####################################################################
__author__ = "Timothy McFadden and Timothy J. Massey"
__creationDate__ = "06/02/2015"
__license__ = "MIT"
__version__ = "1.4.0"

# Python on VMware ESXi (20200330, ESXi 6.7) identifies itself as "linux", but
#  uses very few of the standard LSB paths.  Check for ESXI by file paths.
IS_WIN = "win" in sys.platform
IS_LIN = "lin" in sys.platform
if os.path.exists("/vmfs/volumes") and os.path.exists("/etc/vmware"):
    IS_LIN = False
    IS_ESXI = True
# Configuration ################################################################
CONTROLLER_OK_STATUSES = ["optimal"]
CV_OK_STATES = ["optimal"]
VD_OK_STATES = ["optl"]
PD_OK_STATES = ["onln", "ugood", "dhs", "ghs"]
BBU_OK_STATES = ["optimal"]
SUPPORTED_DRIVERS = ["megaraid_sas", "megasas35.sys", "lsi-mr3"] # lsi-mr3: VMware driver
DEFAULT_FROM = "%s@%s" % (getuser(), socket.gethostname())
LOGFILE = os.path.join(os.sep, "var", "log", "storcli_check.log") if IS_LIN else "storcli_check.log"
START_DATE_FILE=f"{os.path.dirname(os.path.realpath(__file__))}/date_file"
CHECK_FOR_BBU=False
################################################################################
INFO_RE = re.compile("""
    ^Model\s=\s(?P<model>.*?)$                              .*
    ^Serial\sNumber\s=\s(?P<serial>.*?)$                    .*
    ^SAS\sAddress\s=\s(?P<sasaddress>.*?)$                  .*
    ^Firmware\sPackage\sBuild\s=\s(?P<fw_package>.*?)$      .*
    ^Controller\sStatus\s=\s(?P<ctrl_status>.*?)$           .*
""", re.VERBOSE | re.MULTILINE | re.DOTALL | re.IGNORECASE)
VD_INFO_LINE_RE = re.compile("""
    ^(?P<dg>\d+)/(?P<vd>\d+)    \s+
    (?P<type>.+?)               \s+
    (?P<state>.+?)              \s+
    (?P<access>.+?)             \s+
    (?P<consistent>.+?)         \s+
    (?P<cache>.+?)              \s+
    (?P<scc>.+?)                \s+
    (?P<size>.+?\s[MGT]B)       \s*
""", re.VERBOSE | re.IGNORECASE)
PD_INFO_LINE_RE = re.compile("""
    ^(?P<enclosure>\d+|\s+):(?P<slot>\d+)   \s+
    (?P<devid>\d+)                      \s+
    (?P<state>.+?)                      \s+
    (?P<drive_group>-|\d+?)             \s+
    (?P<size>.+?\s[MGT]B)               \s+
    (?P<interface>.+?)                  \s+
    (?P<medium>.+?)                     \s+
    (?P<sed>.+?)                        \s+
    (?P<pi>.+?)                         \s+
    (?P<sector_size>.+?)                \s+
    (?P<model>.+?)                      \s+
    (?P<spun>.+?)                       \s*
""", re.VERBOSE | re.IGNORECASE)
BBU_LINE_RE = re.compile("""
   ^(?P<model>.+?)\s+
    (?P<state>.+?)\s+
    (?P<retention_time>.+?)\s+
    (?P<temp>\d+C)\s+
    (?P<mode>.+?)\s+
    (?P<mfg_date>.+?)\s+
    (?P<next_learn_date>.+?)\s+
    (?P<next_learn_time>.+?)\s*
""", re.VERBOSE | re.IGNORECASE)
CACHEVAULT_LINE_RE = re.compile("""
   ^(?P<model>.+?)\s+
    (?P<state>.+?)\s+
    (?P<temp>\d+C)\s+
    (?P<mode>.+?)\s+
    (?P<mfg_date>.+?)\s*
""", re.VERBOSE | re.IGNORECASE)
DRIVER_RE = re.compile("""
    ^Driver\sName\s=\s(?P<name>.+?)\s*$        .*
    ^Driver\sVersion\s=\s(?P<version>.+?)\s*$
""", re.VERBOSE | re.MULTILINE | re.IGNORECASE | re.DOTALL)

##############################################################


st = datetime(1970, 1, 1, 0, 0, 0)

if exists(START_DATE_FILE):
   f = open(START_DATE_FILE, "r")
   st = duParser.parse(f.read())
   f.close()


##############################################################




def find_storcli(logger, names=["storcli", "storcli64"]):
    """Look for the storcli application.  This is a little tricky because we
    may be running from cron (which has a very different path).
    """
    default_paths = []

    if IS_WIN:
        names = ["%s.exe" % x for x in names]

    # Let the user use CWD
    for name in names:
        if os.path.exists(name):
            path = os.path.abspath(os.path.join(".", name))
            logger.debug("found %s at %s", name, path)
            return path

    # Search the $PATH env var
    # NOTE: This gets around some issues w/ Linux version and the return from
    # `which`.  Some *nix's (e.g XenServer) returns something like `no storcli`
    # if it can't find it.  Other *nix's (e.g. Debian) return a null string.
    # Since `which` searches `$PATH`, we can just do that instead and not
    # worry about scraping the return of `which`.
    for path in os.environ['PATH'].split(os.pathsep):
        default_paths += [os.path.join(path, x) for x in names]

    # Add the default location of the RPM on Linux
    default_paths += [
        os.path.join(os.sep, "opt", "MegaRAID", "storcli", x)
        for x in names]

    # Add the default location of the VIB on ESXi (1.23.02)
    default_paths += [
        os.path.join(os.sep, "opt", "lsi", "storcli", x)
        for x in names]

    # I like to put stuff in /usr/local/bin, which may not be in $PATH depending
    # on who's running this command.
    default_paths += [os.path.join("/usr/local/bin", x) for x in names]

    # Finally, search for the executable
    for path in default_paths:
        if os.path.exists(path):
            logger.debug("found %s", path)
            return path

    logger.error("Can't find storcli64")
    raise Exception("Can't find storcli64")


def get_logger(name=None, screen_level=logging.INFO,
               logfile_path=None, logfile_level=logging.DEBUG,
               logfile_mode="ab"):
    """Initializes the logging object.

    :param str name: The name of the logger; defaults to the script name
    :param int screen_level: The level of the screen logger
    :param str logfile_path: The path of the log file, if any
    :param int logfile_level: The level of the file logger
    :param str logfile_mode: The file mode of the file logger
    """
    if not name:
        name = os.path.splitext(os.path.basename(__file__))[0]

    _format = "%(asctime)s - %(filename)s:%(lineno)d - %(levelname)s - %(message)s"
    _logger = logging.getLogger(name)
    _logger.setLevel(logging.DEBUG)

    ch = logging.StreamHandler()
    screen_formatter = logging.Formatter(_format)
    ch.setFormatter(screen_formatter)
    ch.setLevel(screen_level)
    _logger.addHandler(ch)

    if logfile_path:
        logfile_formatter = logging.Formatter(_format)
        fh = logging.FileHandler(logfile_path, logfile_mode)
        fh.setLevel(logfile_level)
        fh.setFormatter(logfile_formatter)
        _logger.addHandler(fh)

    return _logger


def flush_logfile(logger):
    """Finds any FileHandlers and flushes them."""
    for handler in [x for x in logger.handlers if isinstance(x, logging.FileHandler)]:
        handler.flush()


def remove_directory(top, remove_top=True, list_filter=None):
    '''
    Removes all files and directories, bottom-up.

    :param str top: The top-level directory to clean out
    :param bool remove_top: Whether or not to delete the top
        directory when cleared.
    :param code filter: A function that returns True or False
        based on the name of the file or folder.  Returning
        True means "delete it", False means "keep it".
    '''
    if not (top and os.path.exists(top)):
        return

    for root, dirs, files in os.walk(top, topdown=False):
        for name in filter(list_filter, files):
            os.remove(os.path.join(root, name))

        for name in filter(list_filter, dirs):
            os.rmdir(os.path.join(root, name))

    if remove_top:
        os.rmdir(top)


def zip(items, destination):
    '''Zip up all request items into a single file.  We will attempt to use the
    zipfile package.  However, there's a bug in 2.7 where files > 4G will not
    work (http://bugs.python.org/issue9720).
    '''
    def add_directory(zipfile_obj, source_dir, dest_dir):
        '''Walk a directory and add all files to the zipfile.'''
        rootlen = len(source_dir) + 1
        for base, dirs, files in os.walk(source_dir):
            for item in [x for x in files]:
                fn = os.path.join(base, item)
                zipfile_obj.write(fn, dest_dir + fn[rootlen:])

    myzip = zipfile.ZipFile(destination, 'w', zipfile.ZIP_DEFLATED)

    try:
        for item in items:
            if os.path.isdir(item):
                add_directory(myzip, item, item + os.sep)
            else:
                myzip.write(item)
    finally:
        myzip.close()

    if not os.path.isfile(destination):
        raise Exception("Zip file was not created")  # pragma: no cover


def execute(command, cwd=None):
    p = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, shell=True, cwd=cwd)
    out, _ = p.communicate()

    if IS_WIN:
        out = out.replace('\r\n', '\n')

    return out.strip()


def sendmail(subject, to, sender, body, mailserver, body_type="html", attachments=None, cc=None):
    """Send an email message using the specified mail server using Python's
    standard `smtplib` library and some extras (e.g. attachments).

    NOTE: This function has no authentication.  It was written for a mail server
    that already does sender/recipient validation.

    WARNING: This is a non-streaming message system.  You should not send large
    files with this function!

    NOTE: The body should include newline characters such that no line is greater
    than 990 characters.  Otherwise the email server will insert newlines which
    may not be appropriate for your content.
    http://stackoverflow.com/a/18568276
    """
    msg = MIMEMultipart()
    msg['Subject'] = subject
    msg['From'] = sender
    msg['To'] = ', '.join(to)

    if cc:
        msg['Cc'] = ", ".join(cc)
    else:
        cc = []

    msg.attach(MIMEText(body, body_type))

    attachments = attachments or []

    for attachment in attachments:
        part = MIMEBase('application', "octet-stream")
        part.set_payload(open(attachment, "rb").read())
        encoders.encode_base64(part)

        part.add_header('Content-Disposition', 'attachment', filename=os.path.basename(attachment))

        msg.attach(part)

    server = smtplib.SMTP(mailserver)
    server.sendmail(sender, to + cc, msg.as_string())  # pragma: no cover


class Controller(object):
    def __init__(self, show_all_data, event_data, logger):
        self._cached_info = show_all_data
        self._cached_events = event_data
        self._logger = logger

        self._basic_data = {}
        self._driver_data = {}
        self._vd_info = []
        self._pd_info = []
        self._bbu_info = {}
        self._cv_info = {}
        self._event_info = []

        self.vd_list = "--- no virtual drives found ---"
        self.pd_list = "--- no physical drives found ---"
        self.cv_list = "--- no Cachevault found ---"
        self.bbu_list = "--- no BBU found ---"
        self._parse_info()
        self._parse_events()
        self._check()

    def __repr__(self):
        return "Controller(%s)" % self._basic_data.get("sasaddress", "unknown")

    def _event_data(self, text):
        result = {
            "time": None,
            "description": None
        }

        match = re.search("Time: (.*?)$", text, re.MULTILINE)
        if match:
            result["time"] = match.group(1)

        match = re.search("Event Description: (.*?)$", text, re.MULTILINE)
        if match:
            result["description"] = match.group(1)

        return result

    def _parse_events(self):
        if not self._cached_events:
            return

        parts = re.split("seqNum", self._cached_events)
        for part in parts[1:]:
            self._event_info.append(self._event_data(part))

    def _parse_info(self):
        """Parsed the output of the "show all" command into Python types"""
        self._logger.debug("begin parse")
        try:
            self._basic_data = INFO_RE.search(self._cached_info.decode('ascii')).groupdict()
            self._driver_data = DRIVER_RE.search(self._cached_info.decode('ascii')).groupdict()

            vd_count_match = re.search("Virtual Drives = (\d+)", self._cached_info.decode('ascii'), re.IGNORECASE)
            if vd_count_match:
                vd_count = int(vd_count_match.group(1))
            else:
                vd_count = None

            match = re.search("^VD\sLIST.*?===$(.*?)Cac=", self._cached_info.decode('ascii'), re.MULTILINE | re.DOTALL)
            if match:
                self.vd_list = match.group(1)
                for line in self.vd_list.split("\n"):
                    match = VD_INFO_LINE_RE.search(line)
                    if match:
                        self._vd_info.append(match.groupdict())

            # Make sure we parse each VD line
            if vd_count and (len(self._vd_info) != vd_count):
                self._logger.error("Unparsed VDs on: %s", self)
                raise Exception("Unparsed VDs on: %s" % self)

            pd_count_match = re.search("Physical Drives = (\d+)", self._cached_info.decode('ascii'), re.IGNORECASE)
            if pd_count_match:
                pd_count = int(pd_count_match.group(1))
            else:
                pd_count = None

            match = re.search("^PD\sLIST.*?===$(.*?)EID=", self._cached_info.decode('ascii'), re.MULTILINE | re.DOTALL)
            if match:
                self.pd_list = match.group(1)

                for line in self.pd_list.split("\n"):
                    match = PD_INFO_LINE_RE.search(line)
                    if match:
                        self._pd_info.append(match.groupdict())

            # Make sure we parse each PD line
            if pd_count and (len(self._pd_info) != pd_count):
                self._logger.error("Unparsed PDs on: %s", self)
                raise Exception("Unparsed PDs on: %s" % self)

            match = re.search("^BBU.Info.*?(---.*---$)", self._cached_info.decode('ascii'), re.MULTILINE | re.DOTALL)
            if match:
                self.bbu_list = match.group(1)
                for line in self.bbu_list.split("\n"):
                    match = BBU_LINE_RE.search(line)
                    if match:
                        self._bbu_info = match.groupdict()
                        break

            match = re.search("^Cachevault.Info.*?(---.*---$)", self._cached_info.decode('ascii'), re.MULTILINE | re.DOTALL)
            if match:
                self.cv_list = match.group(1)
                for line in self.cv_list.split("\n"):
                    match = CACHEVAULT_LINE_RE.search(line)
                    if match:
                        self._cv_info = match.groupdict()
                        break

            self._parsed = True
        except Exception as e:
            self._logger.error(e)
            raise

        self._logger.debug("...ok")

    def _check(self):
        """Checks the state and status of the controller and all virtual/physical
        drives.
        """
        # https://github.com/mtik00/storcli-check/issues/8
        # Newer versions of storcli include HBAs in its list as well as
        # MR.  This script isn't designed to check HBAs.  Therefore, if we
        # *are* an HBA, we don't want to check ourselves.
        # NOTE: We still want to *see* HBAs.  Otherwise the controller indicies
        # might get mangled.
        if not self._driver_data.get("name", '') in SUPPORTED_DRIVERS:
            self._logger.debug("Driver [%s] not supported.  Info not actually checked!", self._driver_data.get("name", '') )
            self.result, self.errors = True, []
            return

        self._logger.debug("begin info check")
        result = True
        errors = []

        if self._basic_data["ctrl_status"].lower() not in CONTROLLER_OK_STATUSES:
            errors.append("%r status: '%s' not in %s" % (
                self,
                self._basic_data["ctrl_status"].lower(),
                CONTROLLER_OK_STATUSES))
            result = False

        if not self._vd_info:
            errors.append("ERROR: No VD info!")
        else:
            for info in self._vd_info:
                if str(info["state"]).lower() not in VD_OK_STATES:
                    errors.append("VD(%s/%s) state: '%s' not in %s" % (
                        info.get("dg", "?"),
                        info.get("vd", "?"),
                        info.get("state", "?").lower(),
                        VD_OK_STATES))
                    result = False

        if not self._pd_info:
            errors.append("ERROR: No PD info!")
        else:
            for info in self._pd_info:
                if str(info["state"]).lower() not in PD_OK_STATES:
                    errors.append("PD(%s:%s [devid %s]) state: '%s' not in %s" % (
                        info.get("enclosure", "?"),
                        info.get("slot", "?"),
                        info.get("devid", "?"),
                        info.get("state", "?").lower(),
                        PD_OK_STATES))
                    result = False

        if CHECK_FOR_BBU:
           if not self._bbu_info:
               errors.append("ERROR:  No BBU info!")
           else:
               if str(self._bbu_info["state"]).lower() not in BBU_OK_STATES:
                   errors.append("BBU state: '%s' not in %s" % (
                       str(self._bbu_info["state"]).lower(),
                       BBU_OK_STATES))
                   result = False

        if self._event_info:
            self._logger.debug("Event found in controller log.  Clear event log to clear 'failure'.")
            self._logger.debug("  (Example command line: storcli /cX delete events)")
            result = False
            errors += ["%s: %s" % (x["time"], x["description"]) for x in self._event_info]

        if result:
            self._logger.debug("...pass")
        else:
            for error in errors:
                self._logger.debug("...%s", error)

            self._logger.warning("%s: !!!FAIL!!!", self)

        self.result, self.errors = result, errors

    def _vd_list_as_html(self):
        return self._format_table_html(self.vd_list, VD_INFO_LINE_RE, VD_OK_STATES)

    def _pd_list_as_html(self):
        return self._format_table_html(self.pd_list, PD_INFO_LINE_RE, PD_OK_STATES)

    def _bbu_list_as_html(self):
        return "<br><br>" + self._format_table_html(self.bbu_list, BBU_LINE_RE, BBU_OK_STATES)

    def _cv_list_as_html(self):
        return "<br><br>" + self._format_table_html(self.cv_list, CACHEVAULT_LINE_RE, CV_OK_STATES)

    def _format_table_html(self, text, info_regex, states):
        """Reformat the data table based on the info_regex["state"] match.

        All of the tables are in the same basic format (unformatted text).  This
        function will loop through the lines in text and look for the info_regex
        match (using re.search).  If a match is found, the "state" is compared
        to `states`.  If it's not found in *that* list, a red background is
        applied to the entire line.

        The return from this function will also have all spaces replaced with
        `&nbsp;` and newlines replaced with "<br>\n".
        """
        newlines = []
        lines = text.split("\n")
        for line in lines:
            match = info_regex.search(line)
            if match and (match.groupdict()["state"].lower() not in states):
                line = line.replace(" ", "&nbsp;")
                line = "<span style='background:red;'>" + line + "</span>"
            else:
                line = line.replace(" ", "&nbsp;")

            newlines.append(line + "<br>\n")

        return ''.join(newlines)

    def ok(self):
        return (self.result, self.errors)

    def report_as_html(self):
        """Generates an HTML report of the state of the topology."""
        # NOTE: Mail servers have a line-length limitation (who knew?).  It's
        # important to break up the lists in the body with actual newlines.  If
        # we don't do this, the mail server will kindly do it for us in a
        # non-appropriate manner.
        # http://stackoverflow.com/a/18568276

        if self.errors:
            status = '<span style="color:red;">ERROR</span>'
        else:
            status = '<span style="color:green;">OK</span>'

        body = """
        <h1>Controller Status: %s</h1>
        <p><code>Status: %s<br>Model: %s<br>SAS Address: %s<br>Firmware Package: %s<br></code></p>
        <p><b>VD Status</b><code>%s</code></p>
        <p><b>PD Status</b><code>%s</code></p>
        <p><b>BBU Status</b><code>%s</code></p>
        <p><b>CV Info</b><code>%s</code></p>
        """ % (
            status,
            self._basic_data["ctrl_status"], self._basic_data["model"],
            self._basic_data["sasaddress"], self._basic_data["fw_package"],
            self._vd_list_as_html(),
            self._pd_list_as_html(),
            self._bbu_list_as_html(),
            self._cv_list_as_html()
        )

        if self.errors:
            body += "<b>Errors<font color='red'><pre>\n%s</pre></font></b>" % "\n".join(self.errors)

        return body


class StorCLI(object):
    def __init__(
        self, path, logger=None, working_directory=None, _debug_dir=None,
        ignored_ids=None
    ):
        """This object is used to interact with the LSI storcli utility and parse
        its output.

        :param str path: The path of the storcli/storcli64 binary
        :param logging logger: The logger to use
        :param str working_directory: The working directory to run the storcli
            commands and store the output of the "show all" command.
        :param list(str) ignored_ids: Any controller ID you want to ignore.
            E.g.: [1, 2]
        """
        super(StorCLI, self).__init__()
        self._path = path
        self._logger = logger or get_logger()
        self._cached_info = {}
        self._cached_events = {}
        self._parsed = False
        self._working_directory = working_directory or os.getcwd()
        self._count = None
        self._controllers = []
        self._ignored_ids = list(map(int, ignored_ids)) if ignored_ids else []

        if _debug_dir:
            self._load_from_debug_dir(_debug_dir)
        else:
            self._load()

    def _command(self, command):
        """Execute a generic command on the command line and return the result"""
        command = "%s %s nolog" % (self._path, command)
        return execute(command, cwd=self._working_directory)

    def _load_from_debug_dir(self, path):
        prefixes = set([x[:2] for x in os.listdir(path)])
        for controller_id in prefixes:
            if int(controller_id, 10) in self._ignored_ids:
                continue

            fh = open(os.path.join(path, "%s-show-all.txt" % controller_id), "rb")
            self._cached_info[controller_id] = fh.read()
            fh.close()

            fh = open(os.path.join(path, "%s-events.txt" % controller_id), "rb")
            self._cached_events[controller_id] = fh.read()
            fh.close()

            self._controllers.append(Controller(
                show_all_data=self._cached_info[controller_id],
                event_data=self._cached_events[controller_id],
                logger=self._logger))

        self._check()

    def _load(self):
        """Run the "show all" command, store it to a text file, then parse the
        text.
        """
        for controller_id in range(self.controller_count()):
            if controller_id in self._ignored_ids:
                continue

            self._cached_info[controller_id] = self._command("/c%i show all" % controller_id)

            # Store off the info so it gets zipped up for the report
            temp_file = os.path.join(self._working_directory, "%02i-show-all.txt" % controller_id)
            fh = open(temp_file, "wb")
            fh.write(self._cached_info[controller_id])
            fh.close()
            self._logger.debug("wrote [%s]", temp_file)

            self._cached_events[controller_id] = self._command("/c%i show events filter=warning,critical,fatal" % controller_id)

            ce = self._cached_events[controller_id].decode('ascii')
            match = re.search("Time: (.*?)$", ce, re.MULTILINE)
            valid = False

            while match:
               if match:
                   t = match.group(1)
                   if(duParser.parse(t) < st):
                       ce = ce[match.start() + 1:]
                   else:
                       ce = ce[match.start():]
                       valid = True
                       break
               match = re.search("Time: (.*?)$", ce, re.MULTILINE)

            if not valid:
               ce = ""

            self._cached_events[controller_id] = ce

            # Store off the events so they get zipped up for the report
            temp_file = os.path.join(self._working_directory, "%02i-events.txt" % controller_id)
            fh = open(temp_file, "w")
            fh.write(self._cached_events[controller_id])
            fh.close()
            self._logger.debug("wrote [%s]", temp_file)

        for controller_id in list(self._cached_info.keys()):
            self._controllers.append(Controller(
                show_all_data=self._cached_info[controller_id],
                event_data=self._cached_events[controller_id],
                logger=self._logger))

        self._check()

    def _check(self):
        """Checks the state and status of the controller and all virtual/physical
        drives.
        """
        self.errors = []
        self.result = True

        if not self._controllers:
            self.result = False
            self.errors = ["no controllers found to check!"]
            return

        self._logger.debug("begin OK check")
        for controller in self._controllers:
            result, errors = controller.ok()

            self.result &= result
            self.errors += errors

    def controller_count(self):
        """Returns the number of controllers found on the system"""
        # Cache the number of controllers
        if self._count is not None:
            return self._count

        result = self._command("show ctrlcount")

        match = re.search("controller count = (\d+)", result.decode('ascii'), re.IGNORECASE)
        if match:
            self._count = int(match.group(1))
        else:
            self._count = 0

        return self._count

    def ok(self):
        return (self.result, self.errors)

    def report_as_html(self):
        """Generates an HTML report of the state of the topology."""
        body = ""
        subject = "PASS: %s MR Check Result: PASS" % socket.gethostname()

        for controller in self._controllers:
            result, errors = controller.ok()

            if not result:
                subject = "FAIL: %s MR Check Result: FAIL" % socket.gethostname()

            body += controller.report_as_html()

        return (subject, body)

    def dump_all_info(self, prefix="show-all-"):
        """Dumps the 'show all' command for controllers, enclosures, and
        physical drives.  This command will attempt to overwrite any file
        in the current working directory matching
        "<prefix>(controllers|enclosures|physicaldrives).txt"
        """
        self._command("/call show all > %scontrollers.txt" % prefix)
        self._command("/call/eall show all > %senclosures.txt" % prefix)
        self._command("/call/eall/sall show all > %sphysicaldrives.txt" % prefix)


def parse_arguments(parser, logger, args=None):
    (options, args) = parser.parse_args(args)
    logger.debug("options: %s; args: %s", options, args)
    return (options, args)


def init_parser():
    parser = OptionParser(version=__version__)
    parser.add_option(
        "--keepfiles", dest="keepfiles", action="store_true",
        help="Keep all temporary files generated during run.", default=False)
    parser.add_option(
        "--mailto", dest="mailto",
        help="REQUIRED: comma-separated list of email addresses to send the report to")
    parser.add_option(
        "--mailserver", dest="mailserver",
        help="REQUIRED: The hostname of the SMTP server to use (e.g. 'mailhost.example.com')")
    parser.add_option(
        "--force", dest="force", action="store_true",
        help="send the report regardless of the result")
    parser.add_option(
        "--mailfrom", dest="mailfrom",
        help="the 'user' sending the report (defaults to %s)" % DEFAULT_FROM,
        default=DEFAULT_FROM)
    parser.add_option(
        "--mailcc", dest="mailcc",
        help="comma-separated list of email addresses to CC the report to",
        default="")
    parser.add_option(
        "--no-attachments", dest="attachments", action="store_false",
        help="don't attach the logfile to the email", default=True)
    parser.add_option(
        "--ignore", dest="ignore",
        help="comma-separated listed of controller indicies to ignore",
        default=""
    )
    return parser


if __name__ == '__main__':
    import tempfile

    # Create a temporary directory to store all of our stuff
    working_directory = tempfile.mkdtemp()
    logger = get_logger(logfile_path=LOGFILE, logfile_mode="a")
    logger.debug("================================= Start of script ==========")
    logger.debug("using working directory: [%s]", working_directory)

    parser = init_parser()
    (options, args) = parse_arguments(parser, logger)

    ignored_ids = options.ignore.split(",") if options.ignore else None

    storcli_path = find_storcli(logger)
    s = StorCLI(
        path=storcli_path,
        working_directory=working_directory,
        logger=logger,
        ignored_ids=ignored_ids)
    result, errors = s.ok()

    if not result or options.force:
        # Store off as much info as we can
        if not result:
            s.dump_all_info()

        zipped_log_path = None
        zipdir = None
        if options.attachments:
            zipdir = tempfile.mkdtemp()
            zipped_log_path = os.path.abspath(os.path.join(zipdir, "logs.zip"))
            flush_logfile(logger)
            zip([working_directory, LOGFILE], zipped_log_path)

        subject, body = s.report_as_html()

        fh = open("output.html", "w")
        fh.write(body)
        fh.close()

        if not (options.mailto and options.mailserver):
            print(body)
        else:
            sendmail(
                subject=subject,
                to=options.mailto.split(","),
                body=body,
                sender=options.mailfrom,
                mailserver=options.mailserver,
                attachments=[zipped_log_path] if zipped_log_path else None,
                cc=options.mailcc.split(","))

        if not options.keepfiles: remove_directory(zipdir)

    if not options.keepfiles:
        remove_directory(working_directory)
        if os.path.exists(LOGFILE): os.remove(LOGFILE)
        if os.path.exists("output.html"): os.remove("output.html")

    f = open(START_DATE_FILE, "w")
    dStr = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    f.write(dStr)
    f.close()

    sys.exit(0)
