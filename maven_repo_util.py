
"""maven_repo_util.py: Common functions for dealing with a maven repository"""

import hashlib
import httplib
import logging
import os
import shutil
import urllib2
import urlparse
import re
import sys
from subprocess import Popen
from subprocess import PIPE
from xml.etree.ElementTree import ElementTree


# Constants
MAX_THREADS = 10

_regexGATCVS = None


class ChecksumMode:
    generate = 'generate'
    download = 'download'
    check = 'check'


def _downloadChecksum(url, filePath, checksumType, expectedSize, retries=3):
    """
    Download specified checksum from given url to filepath. Both these inputs include filename of the original file
    to which the checksum belongs.

    :param url: url of the original file
    :param filePath: local filepath where the original file is stored
    :param checksumType: the type of downloaded checksum, e.g. md5 or sha1
    :param expectedSize: expected filesize of the downloaded file
    :param retries: number of retries when a strange error occurs or filesize doesn't match the expected one'
    """
    csDownloaded = False
    while retries > 0 and not csDownloaded:
        retries -= 1
        csUrl = url + "." + checksumType.lower()
        logging.debug('Downloading %s checksum from %s', checksumType.upper(), csUrl)
        try:
            csHttpResponse = urllib2.urlopen(urllib2.Request(csUrl))
            csFilePath = filePath + "." + checksumType.lower()
            with open(csFilePath, 'wb') as localfile:
                shutil.copyfileobj(csHttpResponse, localfile)
            if (csHttpResponse.code != 200):
                logging.warning('Unable to download checksum from %s, error code: %s', csUrl, csHttpResponse.code)
                if csHttpResponse.code / 100 != 5:  # if other than 5xx error occurs do not try again
                    retries = 0
            elif not readChecksumFromFile(csFilePath, expectedSize):
                logging.warning('Downloaded %s checksum from %s is in invalid format',
                                checksumType.upper(), csUrl)
                os.remove(csFilePath)
            else:
                csDownloaded = True
        except urllib2.HTTPError as err:
                logging.warning('Unable to download checksum from %s, error code: %s', csUrl, err.code)
                if err.code / 100 != 5:  # if other than 5xx error occurs do not try again
                    retries = 0
        except urllib2.URLError as err:
            logging.warning('Unknown error while downloading checksum from %s: %s', csUrl, str(err))
    return csDownloaded


def download(url, filePath=None, checksumMode=ChecksumMode.check):
    """Download the given url to a local file"""
    logging.debug('Attempting download: %s', url)

    if filePath:
        if os.path.exists(filePath):
            logging.debug('Local file already exists, skipping: %s', filePath)
            return
        localdir = os.path.dirname(filePath)
        if not os.path.exists(localdir):
            os.makedirs(localdir)

    def getFileName(url, openUrl):
        if 'Content-Disposition' in openUrl.info():
            # If the response has Content-Disposition, try to get filename from it
            cd = dict(map(
                lambda x: x.strip().split('=') if '=' in x else (x.strip(), ''),
                openUrl.info()['Content-Disposition'].split(';')))
            if 'filename' in cd:
                filename = cd['filename'].strip("\"'")
                if filename:
                    return filename
        # if no filename was found above, parse it out of the final URL.
        return os.path.basename(urlparse.urlsplit(openUrl.url)[2])

    try:
        retries = 3
        checksumsOk = False
        while retries > 0 and not checksumsOk:
            retries -= 1
            try:
                httpResponse = urllib2.urlopen(urllib2.Request(url))
                if (httpResponse.code == 200):
                    filePath = filePath or getFileName(url, httpResponse)
                    with open(filePath, 'wb') as localfile:
                        shutil.copyfileobj(httpResponse, localfile)
                    httpResponse.close()

                    if checksumMode in (ChecksumMode.download, ChecksumMode.check):
                        md5Downloaded = _downloadChecksum(url, filePath, "md5", 32)
                        sha1Downloaded = _downloadChecksum(url, filePath, "sha1", 40)
                        if not md5Downloaded or not sha1Downloaded:
                            logging.warning('No chance to download checksums to %s correctly.', filePath)

                    if checksumMode == ChecksumMode.check:
                        if checkChecksum(filePath):
                            checksumsOk = True
                    else:
                        checksumsOk = True

                    if checksumsOk:
                        logging.debug('Download of %s complete', filePath)
                        return httpResponse.code
                    elif retries > 0:
                        logging.warning('Checksum problem with %s, trying again...', url)
                        os.remove(filePath)
                        if os.path.exists(filePath + ".md5"):
                            os.remove(filePath + ".md5")
                        if os.path.exists(filePath + ".sha1"):
                            os.remove(filePath + ".sha1")
                    else:
                        logging.error('Checksum problem with %s. No chance to download the file correctly. Exiting',
                                      url)
                        sys.exit(1)
                else:
                    httpResponse.close()
                    if retries:
                        logging.warning('Unable to download, HTTP Response code: %s. Trying again...',
                                        httpResponse.code)
                    else:
                        logging.warning('Unable to download, HTTP Response code: %s. Exiting', httpResponse.code)
                        sys.exit(1)
            except urllib2.HTTPError as err:
                if retries > 0:
                    if err.code / 100 == 5:
                        logging.debug('Unable to download, HTTP Response code = %s, trying again...', err.code)
                    else:
                        logging.debug('Unable to download, HTTP Response code = %s.', err.code)
                        return err.code
                else:
                    logging.debug('Unable to download, HTTP Response code = %s, giving up...', err.code)
                    return err.code
    except urllib2.URLError as e:
        logging.error('Unable to download %s, URLError: %s', url, e.reason)
    except httplib.HTTPException as e:
        logging.exception('Unable to download %s, HTTPException: %s', url, e.message)
    except ValueError as e:
        logging.error('ValueError: %s', e.message)


def _downloadFile(url, filePath, checksumMode=ChecksumMode.check, warnOnError=True):
    """Downloads file from the given URL to local path if the path does not exist yet."""
    fetched = False
    try:
        returnCode = download(url, filePath, checksumMode)
        if (returnCode == 404):
            if warnOnError:
                logging.warning("Remote file not found: %s", url)
            elif (returnCode >= 400):
                if warnOnError:
                    logging.warning("Error code %d returned while downloading %s", returnCode, url)
        fetched = (returnCode == 200)
    except SystemExit:
        fetched = False
    return fetched


def _copyFile(filePath, fileLocalPath, checksumMode=ChecksumMode.check):
    """Copies file from the given path to local path if the path does not exist yet."""
    logging.debug('Copying file: %s', filePath)
    fetched = True

    dirname = os.path.dirname(fileLocalPath)
    if not os.path.exists(dirname):
        os.makedirs(dirname)

    if os.path.exists(filePath):
        shutil.copyfile(filePath, fileLocalPath)
        if checksumMode in (ChecksumMode.download, ChecksumMode.check):
            if os.path.exists(filePath + ".md5"):
                shutil.copyfile(filePath + ".md5", fileLocalPath + ".md5")
            if os.path.exists(filePath + ".sha1"):
                shutil.copyfile(filePath + ".sha1", fileLocalPath + ".sha1")

        if checksumMode == ChecksumMode.check:
            if not checkChecksum(filePath):
                logging.error('Checksum problem with copy of %s. Exiting', filePath)
                sys.exit(1)
    else:
        logging.warning("Source file not found: %s", filePath)
        fetched = False
    return fetched


def fetchFile(url, filePath, checksumMode=ChecksumMode.check, warnOnError=True, exitOnError=False,
              filesetLock=None, fileset=None):
    """
    Fetch file from the given URL (remote or local), to local path if the path does not exist yet. When using this
    method in multiple threads, it is needed to pass filesetLock and fileset arguments to ensure it is thread-safe.
    filesetLock is a threading.Lock instance, which should be shared in all fetchFile calls. fileset is a set in which
    is stored set of files which are actually downloaded. It has to be shared too.
    """
    fetched = False
    if filesetLock is not None:
        filesetLock.acquire()

    if os.path.exists(filePath):
        logging.debug("File already fetched: %s", url)
        if filesetLock is not None:
            filesetLock.release()
        fetched = True
    else:
        if filesetLock is not None:
            if filePath in fileset:
                logging.debug("File is already being downloaded by another thread: %s", url)
                filesetLock.release()
                fetched = True
            else:
                fileset.add(filePath)
                filesetLock.release()

        if not fetched:
            protocol = urlProtocol(url)
            if protocol == 'http' or protocol == 'https':
                fetched = _downloadFile(url, filePath, checksumMode, warnOnError)
            elif protocol == 'file':
                fetched = _copyFile(url[7:], filePath, checksumMode)
            elif protocol == '':
                fetched = _copyFile(url, filePath, checksumMode)
            else:
                logging.warning("Unknown protocol %s. URL: '%s'", protocol, url)
                fetched = False

            if filesetLock is not None:
                filesetLock.acquire()
                fileset.remove(filePath)
                filesetLock.release()

    if exitOnError and not fetched:
        sys.exit(1)
    return fetched


def setLogLevel(level, logfile=None):
    """Sets the desired log level."""
    logLevel = getattr(logging, level.upper(), None)
    unknownLevel = False
    if not isinstance(logLevel, int):
        unknownLevel = True
        logLevel = logging.INFO
    logformat = '%(asctime)s %(levelname)s (%(threadName)s): %(message)s'
    if logfile:
        logging.basicConfig(format=logformat, level=logLevel, filename=logfile,
                            filemode='a')
    else:
        logging.basicConfig(format=logformat, level=logLevel)

    if unknownLevel:
        logging.warning('Unrecognized log level: %s. Log level set to info', level)


def getSha1Checksum(filepath):
    return getChecksum(filepath, hashlib.sha1())


def getChecksum(filepath, sum_constr):
    """Generate a checksums for the file using the given algorithm"""
    logging.debug('Generate %s checksum for: %s', sum_constr.name.upper(), filepath)
    checksum = sum_constr
    with open(filepath, 'rb') as fobj:
        while True:
            content = fobj.read(8192)
            if not content:
                break
            checksum.update(content)
    return checksum.hexdigest()


def readChecksumFromFile(checksumFilepath, expectedLength):
    """Read checksum digest from checksum file
    The content of the checksum file must be e.g. in the following format:

        some text da39a3ee5e6b4b0d3255bfef95601890afd80709

    There can also be CR, LF or both at the end of the line.

    :param checksumFilepath: Location of the checksum file
    :param expectedLength: Expected length of the checksum digest (e.g. 32, 40..)
    :returns: Checksum digest if present in file, None otherwise
    """
    checksumRegex = re.compile("^(?:.*\s+)?([0-9a-f]{%d})\s*$" % expectedLength)

    with open(checksumFilepath, "r") as checksumFile:
        checksumContent = checksumFile.read()
    checksum = checksumRegex.search(checksumContent)

    return checksum.group(1) if checksum else None


def checkChecksum(filepath):
    """Checks if SHA1 and MD5 checksums equals to the ones saved in corresponding files if they are available."""
    return _checkChecksum(filepath, hashlib.md5()) and _checkChecksum(filepath, hashlib.sha1())


def _checkChecksum(filepath, sum_constr):
    """Checks if desired checksum equals to the one saved in corresponding file if it is available."""
    checksumFilepath = filepath + '.' + sum_constr.name.lower()
    if os.path.exists(checksumFilepath):
        logging.debug("Checking %s checksum of %s", sum_constr.name.upper(), filepath)
        generatedChecksum = getChecksum(filepath, sum_constr)
        downloadedChecksum = readChecksumFromFile(checksumFilepath, len(sum_constr.hexdigest()))
        if generatedChecksum != downloadedChecksum:
            return False

        logging.debug("%s checksum of %s OK.", sum_constr.name.upper(), filepath)
    else:
        logging.debug("Checksum file %s doesn't exist, skipping the check.", checksumFilepath)

    return True


def str2bool(v):
    """Convert string value to bool.

    :param v: String representation of bool value
    :returns: True if value of lowercased v is 'true', 'yes', 't', 'y' or '1',
              False if its 'false', 'no', 'f', 'n' or '0',
              raises ValueError if its none of the above
    """
    if isinstance(v, bool):
        return v
    elif isinstance(v, basestring):
        if v.lower() in ['true', 'yes', 't', 'y', '1']:
            return True
        elif v.lower() in ['false', 'no', 'f', 'n', '0']:
            return False
        else:
            raise ValueError("Failed to convert '" + v + "' to boolean")
    else:
        raise ValueError("Failed to convert '" + v + "' to boolean, not a string.")


def gavExists(repoUrl, artifact):
    """Checks if GAV of the given artifact exists in repository with the given root URL."""
    logging.debug("Checking if %s exists in repository %s", str(artifact), repoUrl)

    repoUrl = slashAtTheEnd(repoUrl)

    gavUrl = repoUrl + artifact.getDirPath()
    result = urlExists(gavUrl)

    if not result:
        logging.debug("URL %s does not exist, trying to find the version in artifact metadata", gavUrl)
        metadataUrl = repoUrl + artifact.getArtifactDirPath() + "maven-metadata.xml"
        gaPath = getTempDir(artifact.getArtifactDirPath())
        metadataFilePath = gaPath + 'maven-metadata.xml'
        if os.path.exists(metadataFilePath):
            fetched = True
        else:
            fetched = fetchFile(metadataUrl, metadataFilePath, warnOnError=False)
        if fetched:
            metadataDoc = ElementTree(file=metadataFilePath)
            root = metadataDoc.getroot()
            for versionTag in root.findall("versioning/versions/version"):
                if versionTag.text == artifact.version:
                    result = True
                    break
        else:
            # we want to try pom file only when there are no metadata present
            pomUrl = repoUrl + artifact.getPomFilepath()
            logging.debug("URL %s does not exist. Trying pom file at %s", metadataUrl, pomUrl)
            result = urlExists(pomUrl)

    logging.debug("Artifact %s %sfound at %s", str(artifact), ("" if result else "not "), repoUrl)

    return result


def urlExists(url):
    parsedUrl = urlparse.urlparse(url)
    protocol = parsedUrl[0]
    if protocol == 'http' or protocol == 'https':
        if protocol == 'http':
            connection = httplib.HTTPConnection(parsedUrl[1])
        else:
            connection = httplib.HTTPSConnection(parsedUrl[1])
        connection.request('HEAD', parsedUrl[2], headers={"User-Agent": "Python-Maven Repository Builder"})
        response = connection.getresponse()
        return response.status in [200, 302]
    else:
        if protocol == 'file':
            url = url[7:]
        return os.path.exists(url)


def urlProtocol(url):
    """Determines the protocol in the url, can be empty if there is none in the url."""
    parsedUrl = urlparse.urlparse(url)
    return parsedUrl[0]


def slashAtTheEnd(url):
    """
    Adds a slash at the end of given url if it is missing there.

    :param url: url to check and update
    :returns: updated url
    """
    return url if url.endswith('/') else url + '/'


def transformAsterixStringToRegexp(string):
    return re.escape(string).replace("\\*", ".*")


def getRegExpsFromStrings(strings, exact=True):
    """
    Compiles all given strings into regular expressions. If exact=True, the
    expressions have prepended ^ and appended $.
    """
    rep = re.compile("^r\/.*\/$")
    regExps = []
    for s in strings:
        if rep.match(s):
            regexpString = s[2:-1]
        else:
            regexpString = transformAsterixStringToRegexp(s).strip()
        if exact:
            regexpString = "^" + regexpString + "$"
        regExps.append(re.compile(regexpString))

    return regExps


def getTempDir(relativePath=""):
    """Gets temporary directory for this running instance of Maven Repository Builder."""
    return '/tmp/maven-repo-builder/' + str(3232) + "/" + relativePath


def cleanTempDir():
    """Cleans temporary directory for this running instance of Maven Repository Builder."""
    if os.path.exists(getTempDir()):
        try:
            shutil.rmtree(getTempDir())
        except BaseException as ex:
            logging.error("An error occured while cleaning up temporary directory: %s", str(ex))


def updateSnapshotVersionSuffix(artifact, repoUrl):
    """
    Updates snapshotVersionSuffix in given artifact if the artifact is snapshot and pom
    file with '-SNAPSHOT' in filename does not exist. It reads maven-metadata.xml in
    artifact's directory and reads from there timastamp and builn number of the last
    snapshot build.
    """
    if not artifact.isSnapshot():
        return

    logging.debug("Adding snapshot version suffix for %s:%s:%s:%s", artifact.groupId,
                  artifact.artifactId, artifact.artifactType, artifact.version)
    pomUrl = slashAtTheEnd(repoUrl) + artifact.getPomFilepath()
    if urlExists(pomUrl):
        logging.debug("Not adding, because pom file %s exists", pomUrl)
        return

    metadataUrl = slashAtTheEnd(repoUrl) + artifact.getDirPath() + 'maven-metadata.xml'
    gavPath = getTempDir(artifact.getDirPath())
    metadataFilePath = gavPath + 'maven-metadata.xml'
    if not os.path.exists(metadataFilePath) and not fetchFile(metadataUrl, metadataFilePath):
        logging.debug("Unable to read metadata from %s", metadataUrl)
        return

    metadataDoc = ElementTree(file=metadataFilePath)
    root = metadataDoc.getroot()
    timestamp = root.findtext("versioning/snapshot/timestamp")
    buildNumber = root.findtext("versioning/snapshot/buildNumber")

    if timestamp and buildNumber:
        artifact.snapshotVersionSuffix = '-' + timestamp + '-' + buildNumber
        logging.debug("Version suffix for %s set to %s", artifact.getGATCV(), artifact.snapshotVersionSuffix)


def somethingMatch(regexs, string):
    """
    Returns True if at least one of regular expresions from specified list matches string.

    :param regexs: list of regular expresions
    :param filename: string to match
    :returns: True if at least one of the regular expresions matched the string.
    """
    return any(regex.match(string) for regex in regexs)


def _sortVersionsWithAtlas(versions, versionSorterDir="versionSorter/"):
    """
    Returns sorted list of given verisons using Atlas versionSorter

    :param versions: versions to sort.
    :param versionSorterDir: directory with version sorter maven project
    :returns: sorted versions.
    """
    jarLocation = versionSorterDir + "target/versionSorter.jar"
    if not os.path.isfile(jarLocation):
        logging.debug("Version sorter jar '%s' not found, running 'mvn clean package' in '%s'",
                      jarLocation,
                      versionSorterDir)
        Popen(["mvn", "clean", "package"], cwd=versionSorterDir).wait()
    args = ["java", "-jar", jarLocation] + versions
    ret = Popen(args, stdout=PIPE).communicate()[0].split('\n')[::-1]
    ret.remove("")
    return ret


def loadFlatFile(filename):
    if filename:
        with open(filename, "r") as openedfile:
            lines = openedfile.readlines()
        result = []
        for line in lines:
            resultLine = line.strip()
            if resultLine:
                result.append(resultLine)
        return result


def loadArtifactFile(filename):
    """
    Loads lines from the given file in a list trimming them while loading to contain only GA(TC)V. Can be used to read
    dependency:list output.
    """
    if filename:
        regexComment = re.compile('#.*$')
        with open(filename, "r") as openedfile:
            lines = openedfile.readlines()

        result = []

        for line in lines:
            line = regexComment.sub('', line).strip()
            gatcv = parseGATCVS(line)
            if gatcv:
                result.append(gatcv)
        return result


def parseGATCVS(string):
    global _regexGATCVS
    if not _regexGATCVS:
        # Match pattern (((groupId):)(artifactId:)(type:)(classifier:)?(version))(:scope)?
        _regexGATCVS = re.compile('(([\w\-.]+:){3}([\w\-.]+:)?([\d][\w\-.]+))(:[\w]*\S)?')

    match = _regexGATCVS.search(string)
    if match:
        return match.group(1)


def gatvc_to_gatcv(gatvc):
    """
    Checks if the input contains 5 parts and if so, it swaps the last two.

    :param gatvc: combination of groupId:artifactId:type:version:classifier where classifier is not mandatory
    :return: combination of groupId:artifactId:type:classifier:version if classifier available, input if not
    """
    if gatvc and gatvc.count(":") == 4:
        parts = gatvc.split(":")
        return ":".join(parts[0:3] + [parts[4], parts[3]])
    else:
        return gatvc
