#
# Copyright (c) 2004-2007 rPath, Inc.
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

from mod_python import apache
from email import MIMEText
import os
import sys
import time
import smtplib
import traceback

from conary.lib import coveragehook
from conary.lib import log
from conary.lib import util
from conary.repository.netrepos import netserver
from conary.repository.netrepos import proxy
from conary.server.apachemethods import get, post, putFile

def formatRequestInfo(req):
    c = req.connection
    req.add_common_vars()
    info_dict = {
        'local_addr'     : c.local_ip + ':' + str(c.local_addr[1]),
        'remote_addr'    : c.remote_ip + ':' + str(c.remote_addr[1]),
        'remote_host'    : c.remote_host,
        'remote_logname' : c.remote_logname,
        'aborted'        : c.aborted,
        'keepalive'      : c.keepalive,
        'double_reverse' : c.double_reverse,
        'keepalives'     : c.keepalives,
        'local_host'     : c.local_host,
        'connection_id'  : c.id,
        'notes'          : c.notes,
        'the_request'    : req.the_request,
        'proxyreq'       : req.proxyreq,
        'header_only'    : req.header_only,
        'protocol'       : req.protocol,
        'proto_num'      : req.proto_num,
        'hostname'       : req.hostname,
        'request_time'   : time.ctime(req.request_time),
        'status_line'    : req.status_line,
        'status'         : req.status,
        'method'         : req.method,
        'allowed'        : req.allowed,
        'headers_in'     : req.headers_in,
        'headers_out'    : req.headers_out,
        'uri'            : req.uri,
        'unparsed_uri'   : req.unparsed_uri,
        'args'           : req.args,
        'parsed_uri'     : req.parsed_uri,
        'filename'       : req.filename,
        'subprocess_env' : req.subprocess_env,
        'referer'        : req.headers_in.get('referer', 'N/A')
    }
    l = []
    for key, val in sorted(info_dict.items()):
        if hasattr(val, 'iteritems') and str(val) > 120:
            l.append('  %-15s: %s' %(key, '{'))
            for k, v in sorted(val.items()):
                l.append('    %s : %s,' % (repr(k), repr(v)))
            l[-1] += '}'
        else:
            l.append('  %-15s: %s' %(key, val))
    info = '\n'.join(l)
    return info

def sendMail(fromEmail, fromEmailName, toEmail, subject, body):
    msg = MIMEText.MIMEText(body)
    msg['Subject'] = subject
    msg['From'] = '"%s" <%s>' % (fromEmailName, fromEmail)
    msg['To'] = toEmail

    s = smtplib.SMTP()
    s.connect()
    s.sendmail(fromEmail, [toEmail], msg.as_string())
    s.close()

def logAndEmail(req, cfg, header, msg):
    timeStamp = time.ctime(time.time())

    log.error(header)
    if not cfg.bugsFromEmail or not cfg.bugsToEmail:
        return
    log.error('sending mail to %s' % cfg.bugsToEmail)

    # send email
    body = header + '\n'
    body += 'Time of occurrence: %s\n' % timeStamp
    body += 'Conary repository server: %s\n\n' % req.hostname
    body += msg + '\n'
    body += '\nConnection Information:\n'
    body += formatRequestInfo(req)

    sendMail(cfg.bugsFromEmail, cfg.bugsEmailName, cfg.bugsToEmail, 
             cfg.bugsEmailSubject, body)

def logErrorAndEmail(req, cfg, exception, e, bt):
    timeStamp = time.ctime(time.time())

    header = 'Unhandled exception from conary repository:\n\n%s: %s\n\n' % (exception.__name__, e)

    # Nicely format the exception
    out = util.BoundedStringIO()
    util.formatTrace(exception, e, bt, stream = out, withLocals = False)
    out.write("\nFull stack:\n")
    util.formatTrace(exception, e, bt, stream = out, withLocals = True)
    out.seek(0)
    msg = out.read()

    logAndEmail(req, cfg, header, msg)
    # log error
    log.error(''.join(traceback.format_exception(*sys.exc_info())))

def handler(req):
    coveragehook.install()
    try:
        return _handler(req)
    finally:
        coveragehook.save()

def _handler(req):
    repName = req.filename
    if not repositories.has_key(repName):
        cfg = netserver.ServerConfig()
        cfg.read(req.filename)

        # Throw away any subdir portion.
        rest = req.uri[:-len(req.path_info)] + '/'

        urlBase = "%%(protocol)s://%s:%%(port)d" % \
                        (req.server.server_hostname) + rest

        if not cfg.repositoryDB and not cfg.proxyContentsDir:
            log.error("repositoryDB or proxyContentsDir is required in %s" % 
                      req.filename)
            return apache.HTTP_INTERNAL_SERVER_ERROR
        elif cfg.repositoryDB and cfg.proxyContentsDir:
            log.error("only one of repositoryDB or proxyContentsDir may be specified "
                      "in %s" % req.filename)
            return apache.HTTP_INTERNAL_SERVER_ERROR

        if cfg.repositoryDB:
            if not cfg.contentsDir:
                log.error("contentsDir is required in %s" % req.filename)
                return apache.HTTP_INTERNAL_SERVER_ERROR
            elif not cfg.serverName:
                log.error("serverName is required in %s" % req.filename)
                return apache.HTTP_INTERNAL_SERVER_ERROR

        if os.path.realpath(cfg.tmpDir) != cfg.tmpDir:
            log.error("tmpDir cannot include symbolic links")

        if cfg.closed:
            repos = netserver.ClosedRepositoryServer(cfg)
            repositories[repName] = proxy.SimpleRepositoryFilter(
                                                cfg, urlBase, repos)
            repositories[repName].forceSecure = False
            repositories[repName].cfg = cfg
        elif cfg.proxyContentsDir:
            repositories[repName] = proxy.ProxyRepositoryServer(cfg, urlBase)
            repositories[repName].forceSecure = False
        else:
            repos = netserver.NetworkRepositoryServer(cfg, urlBase)
            repositories[repName] = proxy.SimpleRepositoryFilter(
                                                cfg, urlBase, repos)
            repositories[repName].forceSecure = cfg.forceSSL
            repositories[repName].cfg = cfg

    port = req.connection.local_addr[1]
    # newer versions of mod_python provide a req.is_https() method
    secure = (req.subprocess_env.get('HTTPS', 'off') == 'on')

    repos = repositories[repName]
    method = req.method.upper()

    try:
        if method == "POST":
            return post(port, secure, repos, req)
        elif method == "GET":
            return get(port, secure, repos, req)
        elif method == "PUT":
            return putFile(port, secure, repos, req)
        else:
            return apache.HTTP_METHOD_NOT_ALLOWED
    except apache.SERVER_RETURN:
        # if the exception was an apache server return code,
        # re-raise it and let mod_python handle it.
        raise
    except IOError, e:
        # ignore when the client hangs up on us
        if str(e).endswith('client closed connection.'):
            pass
        else:
            raise
    except:
        cfg = repos.cfg
        exception, e, bt = sys.exc_info()
        logErrorAndEmail(req, cfg, exception, e, bt)
        return apache.HTTP_INTERNAL_SERVER_ERROR


repositories = {}
