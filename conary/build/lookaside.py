#
# Copyright (c) 2004-2009 rPath, Inc.
#
# This program is distributed under the terms of the Common Public License,
# version 1.0. A copy of this license should have been distributed with this
# source file in a file called LICENSE. If it is not present, the license
# is always available at http://www.rpath.com/permanent/licenses/CPL-1.0.
#
# This program is distributed in the hope that it will be useful, but
# without any warranty; without even the implied warranty of merchantability
# or fitness for a particular purpose. See the Common Public License for
# full details.
#

"""
Provides a cache for storing files locally, including
downloads and unpacking layers of files.
"""
import cookielib
import errno
import os
import socket
import time
import urllib2
import urlparse

from conary.lib import log
from conary.lib import sha1helper
from conary.lib import util
from conary import callbacks
from conary.build.mirror import Mirror
from conary.conaryclient.callbacks import FetchCallback, ChangesetCallback


NETWORK_PREFIXES = ('http', 'https', 'ftp', 'mirror')

# some recipes reach into Conary internals here, and have references
# to searchAll
def searchAll(cfg, repCache, name, location, srcdirs, autoSource=False,
              httpHeaders={}, localOnly=False):
    return findAll(cfg, repCache, name, location, srcdirs, autoSource,
                   httpHeaders, localOnly, allowNone=True)



# bw compatible findAll method.
def findAll(cfg, repCache, name, location, srcdirs, autoSource=False,
            httpHeaders={}, localOnly=False, guessName=None, suffixes=None,
            allowNone=False, refreshFilter=None, multiurlMap=None,
            unifiedSourcePath = None):
    if guessName:
        name = name + guessName
    ff = FileFinder(recipeName=location, repositoryCache=repCache,
                          localDirs=srcdirs, multiurlMap=multiurlMap,
                          mirrorDirs=cfg.mirrorDirs,
                          cfg=cfg)

    if localOnly:
        if srcdirs:
            searchMethod = ff.SEARCH_LOCAL_ONLY
        else:
            # BW COMPATIBLE HACK - since we know we aren't actually searching
            # srcdirs since they're empty, we take this to mean 
            # repository only.
            searchMethod = ff.SEARCH_REPOSITORY_ONLY
    elif autoSource:
        searchMethod = ff.SEARCH_REPOSITORY_ONLY
    else:
        searchMethod = ff.SEARCH_ALL


    results = ff.fetch(name, suffixes=suffixes, archivePath=unifiedSourcePath,
                       allowNone=allowNone, searchMethod=searchMethod,
                       headers=httpHeaders, refreshFilter=refreshFilter)
    return results[1]

# backwards compatible fetchURL method
def fetchURL(cfg, name, location, httpHeaders={}, guessName=None, mirror=None):
    repCache = RepositoryCache(None, cfg=cfg)
    ff = FileFinder(recipeName=location, repositoryCache=repCache,
                    cfg=cfg)
    try:
        return ff.searchNetworkSources(name, name, headers=httpHeaders)
    except PathFound, pathInfo:
        return pathInfo.path

class FileFinder(object):

    SEARCH_ALL = 0
    SEARCH_REPOSITORY_ONLY = 1
    SEARCH_LOCAL_ONLY = 2

    def __init__(self, recipeName, repositoryCache, localDirs=None,
                 multiurlMap=None, refreshFilter=None, mirrorDirs = None,
                 cfg=None):
        self.recipeName = recipeName
        self.repCache = repositoryCache
        if self.repCache:
            self.repCache.setConfig(cfg)
        if localDirs is None:
            localDirs = []
        self.localDirs = localDirs
        self.multiurlMap = multiurlMap
        self.mirrorDirs = mirrorDirs

    def fetch(self, uri, suffixes=None, archivePath=None, headers=None,
              allowNone=False, searchMethod=0, # SEARCH_ALL
              refreshFilter=None):
        uriList = self._getPathsToSearch(uri, suffixes)
        for newUri in uriList:
            try:
                self._fetch(newUri, uri,
                            archivePath, headers=headers,
                            refreshFilter=refreshFilter,
                            searchMethod=searchMethod)
            except PathFound, pathInfo:
                return pathInfo.isFromRepos, pathInfo.path

        # we didn't find any matching url.
        if not allowNone:
            raise OSError(errno.ENOENT, os.strerror(errno.ENOENT), uri)
        return None, None

    def _fetch(self, uri, originalUri, archivePath, searchMethod, headers=None,
               refreshFilter=None):
        refresh = (refreshFilter and
                   refreshFilter(os.path.basename(uri)))
        if searchMethod == self.SEARCH_LOCAL_ONLY:
            self.searchFilesystem(uri)
            return
        elif searchMethod == self.SEARCH_REPOSITORY_ONLY:
            if archivePath:
                self.searchArchive(archivePath, uri)
            elif refresh:
                self.searchNetworkSources(uri, originalUri, headers)
            self.searchRepository(uri)
        else: #SEARCH_ALL
            self.searchFilesystem(uri)
            if archivePath:
                self.searchArchive(archivePath, uri)
            elif refresh:
                self.searchNetworkSources(uri, originalUri, headers)
            self.searchRepository(uri)
            self.searchLocalCache(uri)
            self.searchNetworkSources(uri, originalUri, headers)

    def searchRepository(self, uri):
        if self.repCache.hasFileName(uri):
            log.info('found %s in repository', uri)
            path = self.repCache.cacheFile(self.recipeName, uri, uri)
            raise PathFound(path, True)
        basename = os.path.basename(uri)
        if self.repCache.hasFileName(basename):
            log.info('found %s in repository', basename)
            path = self.repCache.cacheFile(self.recipeName, uri, basename)
            raise PathFound(path, True)

    def searchLocalCache(self,  uri):
        # exact match first, then look for cached responses from other servers
        path = self.repCache.getCacheEntry(self.recipeName, uri)
        if path: raise PathFound(path, False)
        basename = os.path.basename(uri)
        prefix = uri.split('://', 1)[0]
        if prefix in NETWORK_PREFIXES or prefix == 'lookaside':
            path = self.repCache.findInCache(self.recipeName, basename)
            if path:
                raise PathFound(path, False)

    def searchFilesystem(self, uri):
        if uri[0] == '/':
            return
        path = util.searchFile(uri, self.localDirs)
        if path:
            raise PathFound(path, False)

    def searchArchive(self, archiveName, path):
        path =  self.repCache.getArchiveCacheEntry(archiveName, path)
        if path:
            raise PathFound(path, True)

    def searchNetworkSources(self, uri, originalUri, headers):
        prefix = uri.split('://', 1)[0]
        if prefix not in NETWORK_PREFIXES:
            return
        # Save users from themselves - encode some characters automatically
        uri = uri.replace(' ', '%20')
        # check for negative cache entries to avoid spamming servers
        negativePath =  self.repCache.checkNegativeCache(self.recipeName, uri)
        if negativePath:
            log.warning('not fetching %s (negative cache entry %s exists)',
                        uri, negativePath)
            return

        log.info('Trying %s...', uri)
        explicit = (uri == originalUri)
        if headers is None:
            headers = {}
        inFile = self._fetchUrl(uri, headers, explicit=explicit)
        if inFile is None:
            self.repCache.createNegativeCacheEntry(self.recipeName, uri)
        else:
            contentLength = int(inFile.headers.get('Content-Length', 0))
            path = self.repCache.addFileToCache(self.recipeName, uri,
                                                inFile, contentLength)
            if path:
                raise PathFound(path, False)
        return

    def _getPathsToSearch(self, uri, suffixes):
        if '://' in uri:
            prefix, path = uri.split('://', 1)
        else:
            prefix = None
            path = uri

        if prefix == 'multiurl':
            host, path = path.split('/', 1)
            pathList = self.multiurlMap[host]
            pathList = [ "%s/%s" % (x, path) for x in pathList ]
        else:
            pathList = [uri]

        newPathList = []
        for uri in pathList:
            if '://' in uri:
                prefix, path = uri.split('://', 1)
            else:
                prefix = None
                path = None
            if prefix == 'mirror':
                mirrorType, path = path.split('/', 1)
                for mirrorUrl in Mirror(self.mirrorDirs, mirrorType):
                    newPathList.append('%s/%s' % (mirrorUrl, path))
            else:
                newPathList.append(uri)
        pathList = newPathList

        if suffixes is not None:
            newPathList = []
            for path in pathList:
                for suffix in suffixes:
                    newPathList.append(path + '.' + suffix)
            pathList = newPathList

        return pathList



    def _fetchUrl(self, uri, headers, explicit=True):
        retries = 0
        inFile = None
        while retries < 5:
            try:
                # set up a urlopener that tracks cookies to handle
                # sites like Colabnet that want to set a session cookie
                cj = cookielib.LWPCookieJar()
                pwm = PasswordManager()
                # set up a urllib2 opener that can handle cookies and basic
                # authentication.
                # FIXME: should digest auth be handled too?
                opener = urllib2.build_opener(urllib2.HTTPCookieProcessor(cj),
                                              urllib2.HTTPBasicAuthHandler(pwm))
                split = list(urlparse.urlsplit(uri))
                protocol = split[0]
                if protocol != 'ftp':
                    server = split[1]
                    if '@' in server:
                        # username, and possibly password, were given
                        login, server = server.split('@')
                        # get rid of the username/password part of the server
                        split[1] = server
                        if ':' in login:
                            # password was given
                            user, passwd = login.split(':')
                        else:
                            # we don't have the ability to prompt.  Assume
                            # a blank password
                            user = login
                            passwd = ''
                        pwm.add_password(user, passwd)
                name = urlparse.urlunsplit(split)
                req = urllib2.Request(name, headers=headers)
                inFile = opener.open(req)
                if not name.startswith('ftp://'):
                    content_type = inFile.info()['content-type']
                    if not explicit and 'text/html' in content_type:
                        raise urllib2.URLError('"%s" not found' % name)
                log.info('Downloading %s...', name)
                break
            except urllib2.HTTPError, msg:
                if msg.code == 404:
                    return None
                else:
                    log.error('error downloading %s: %s',
                              name, str(msg))
                    return None
            except urllib2.URLError:
                return None
            except socket.error, err:
                num, msg = err
                if num == errno.ECONNRESET:
                    log.info('Connection Reset by FTP server'
                             'while retrieving %s.'
                             '  Retrying in 10 seconds.', name, msg)
                    time.sleep(10)
                    retries += 1
                else:
                    return None
            except IOError, msg:
                # only retry for server busy.
                ftp_error = msg.args[1]
                if isinstance(ftp_error, EOFError):
                    # server just hung and gave no response
                    return None
                response = msg.args[1].args[0]
                if isinstance(response, str) and response.startswith('421'):
                    log.info('FTP server busy when retrieving %s.'
                             '  Retrying in 10 seconds.', name)
                    time.sleep(10)
                    retries += 1
                else:
                    return None
        return inFile

class PasswordManager:
    # password manager class for urllib2 that handles exactly 1 password
    def __init__(self):
        self.user = ''
        self.passwd = ''

    def add_password(self, user, passwd):
        self.user = user
        self.passwd = passwd

    def find_user_password(self, *args, **kw):
        return self.user, self.passwd



class RepositoryCache(object):

    def __init__(self, repos, refreshFilter=None, cfg=None):
	self.repos = repos
        self.refreshFilter = refreshFilter
	self.nameMap = {}
        self.cacheMap = {}
        self.quiet = False
        self._basePath = self.downloadRatedLimit = None
        self.setConfig(cfg)

    def setConfig(self, cfg):
        if cfg:
            self.quiet = cfg.quiet
            self._basePath = cfg.lookaside
            self.downloadRateLimit = cfg.downloadRateLimit

    def _getBasePath(self):
        if self._basePath is None:
            raise RuntimeError('Tried to use repository cache with unset'
                               ' basePath')
        return self._basePath

    basePath = property(_getBasePath)

    def addFileHash(self, troveName, troveVersion, pathId, path, fileId,
                    fileVersion, sha1, mode):
	self.nameMap[path] = (troveName, troveVersion, pathId, path, fileId,
                              fileVersion, sha1, mode)

    def hasFileName(self, fileName):
        if self.refreshFilter:
            if self.refreshFilter(fileName):
                return False
	return fileName in self.nameMap

    def cacheFile(self, prefix, fileName, basename):
	cachePath = self.getCachePath(prefix, fileName)
        util.mkdirChain(os.path.dirname(cachePath))

        if basename in self.cacheMap:
            # don't check sha1 twice
            return self.cacheMap[basename]
	(troveName, troveVersion, pathId, troveFile, fileId,
                    troveFileVersion, sha1, mode) = self.nameMap[basename]
        sha1Cached = None
        cachedMode = None
	if os.path.exists(cachePath):
            sha1Cached = sha1helper.sha1FileBin(cachePath)
        if sha1Cached != sha1:
            if sha1Cached:
                log.info('%s sha1 %s != %s; fetching new...', basename,
                          sha1helper.sha1ToString(sha1),
                          sha1helper.sha1ToString(sha1Cached))
            else:
                log.info('%s not yet cached, fetching...', fileName)

            if self.quiet:
                csCallback = None
            else:
                csCallback = ChangesetCallback()

            f = self.repos.getFileContents(
                [ (fileId, troveFileVersion) ], callback = csCallback)[0].get()
            util.copyfileobj(f, open(cachePath, "w"))
            fileObj = self.repos.getFileVersion(
                pathId, fileId, troveFileVersion)
            fileObj.chmod(cachePath)

        cachedMode = os.stat(cachePath).st_mode & 0777
        if mode != cachedMode:
            os.chmod(cachePath, mode)
        self.cacheMap[basename] = cachePath
	return cachePath

    def addFileToCache(self, prefix, name, infile, contentLength):
        # cache needs to be hierarchical to avoid collisions, thus we
        # use prefix so that files with the same name and different
        # contents in different packages do not collide
        cachedname = self.getCachePath(prefix, name)
        util.mkdirChain(os.path.dirname(cachedname))
        f = open(cachedname, "w+")

        try:
            BLOCKSIZE = 1024 * 4

            if self.quiet:
                callback = callbacks.FetchCallback()
            else:
                callback = FetchCallback()

            wrapper = callbacks.CallbackRateWrapper(callback, callback.fetch,
                                                    contentLength)
            total = util.copyfileobj(infile, f, bufSize=BLOCKSIZE,
                                     rateLimit = self.downloadRateLimit,
                                     callback = wrapper.callback)

            f.close()
            infile.close()
        except:
            os.unlink(cachedname)
            raise

        # work around FTP bug (msw had a better way?)
        if name.startswith("ftp://"):
            if os.stat(cachedname).st_size == 0:
                os.unlink(cachedname)
                self.createNegativeCacheEntry(prefix, name[5:])
                return None

        return cachedname

    def setRefreshFilter(self, refreshFilter):
        self.refreshFilter = refreshFilter

    def getCacheDir(self, prefix, negative=False):
        if negative:
            prefix = 'NEGATIVE' + os.sep + prefix

        return os.sep.join((self.basePath, prefix))

    def getCachePath(self, prefix, name, negative=False):
        name = self._truncateName(name)
        cacheDir = self.getCacheDir(prefix, negative=negative)
        path = os.sep.join((cacheDir, name))
        return os.path.normpath(path)

    def clearCacheDir(self, prefix, negative=False):
        negativeCachePath = self.getCacheDir(prefix, negative = negative)
        util.rmtree(os.path.dirname(negativeCachePath), ignore_errors = True)

    def createNegativeCacheEntry(self, prefix, name):
        path = self.getCachePath(prefix, name, negative=True)
        util.mkdirChain(os.path.dirname(path))
        open(path, 'w+')

    def findInCache(self, prefix, basename):
        return util.searchPath(basename,
                               os.path.join(self.basePath, prefix))

    def getCacheEntry(self, prefix, path):
        path = self.getCachePath(prefix, path)
        if os.path.exists(path):
            return path

    def checkNegativeCache(self, prefix, name):
        path = self.getCachePath(prefix, name, negative=True)
        if os.path.exists(path):
            # Keep negative cache for 1h
            if time.time() < (60*60 + os.path.getmtime(path)):
                return path
            else:
                os.remove(path)
        return False

    def _truncateName(self, uri):
        items = uri.split('://', 1)
        if len(items) == 1:
            return uri
        prefix, path = items
        if prefix in NETWORK_PREFIXES or prefix == 'lookaside':
            return path
        return uri

    def getArchiveCachePath(self, archiveName, path=''):
        # CNY-2627 introduced a separate lookaside stack for archive contents
        # this dir tree is parallel to NEGATIVE and trovenames.
        # the name =X_CONTENTS= was chosen because = is an illegal character
        # in a trovename and thus will never conflict with real troves.
        archiveType, trailingPath = archiveName.split('://', 1)
        contentsPrefix = "=%s_CONTENTS=" % archiveType.upper()
        return self.getCachePath(contentsPrefix,
                                 os.path.join(trailingPath, path))


    def getArchiveCacheEntry(self, archiveName, path):
        fullPath = self.getArchiveCachePath(archiveName, path)
        if os.path.exists(fullPath):
            return fullPath

class PathFound(Exception):
    def __init__(self, path, isFromRepos):
        self.path = path
        self.isFromRepos = isFromRepos
