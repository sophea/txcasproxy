#! /usr/bin/env python

#Standard library
import Cookie
import cookielib
import datetime
import os.path
import pprint
import socket
from urllib import urlencode
import urlparse

# Application modules
from ca_trust import CustomPolicyForHTTPS
import proxyutils

#External modules
from dateutil.parser import parse as parse_date
from klein import Klein

from OpenSSL import crypto

import treq
#from twisted.internet.defer import inlineCallbacks
from twisted.internet import reactor
from twisted.internet.ssl import Certificate
from twisted.python import log
import twisted.web.client as twclient
from twisted.web.client import BrowserLikePolicyForHTTPS, Agent
from twisted.web.client import HTTPConnectionPool
from twisted.web.static import File
from lxml import etree



class ProxyApp(object):
    app = Klein()

    ns = "{http://www.yale.edu/tp/cas}"
    port = None
    
    logout_instant_skew = 5
    
    ticket_name = 'ticket'
    service_name = 'service'
    renew_name = 'renew'
    pgturl_name = 'pgtUrl'
    
    def __init__(self, proxied_url, cas_info, fqdn=None, authorities=None):
        if proxied_url.endswith('/'):
            proxied_url = proxied_url[:-1]
        self.proxied_url = proxied_url
        p = urlparse.urlparse(proxied_url)
        self.p = p
        netloc = p.netloc
        self.proxied_netloc = netloc
        self.proxied_host = netloc.split(':')[0]
        self.proxied_path = p.path
        self.cas_info = cas_info
        
        cas_param_names = set([])
        cas_param_names.add(self.ticket_name.lower())
        cas_param_names.add(self.service_name.lower())
        cas_param_names.add(self.renew_name.lower())
        cas_param_names.add(self.pgturl_name.lower())
        self.cas_param_names = cas_param_names
        
        if fqdn is None:
            fqdn = socket.getfqdn()
        self.fqdn = fqdn
        
        self.valid_sessions = {}
        self.logout_tickets = {}
        
        self._make_agent(authorities)
        
        self.resource_handlers = {
            '/grouperExternal/public/OwaspJavaScriptServlet': self.csrf_js_hack,}

    def _make_agent(self, auth_files):
        """
        """
        if auth_files is None or len(auth_files) == 0:
            self.agent = None
        else:
            extra_ca_certs = []
            for ca_cert in auth_files:
                with open(ca_cert, "rb") as f:
                    data = f.read()
                cert = crypto.load_certificate(crypto.FILETYPE_PEM, data)
                del data
                extra_ca_certs.append(cert)
            
            policy = CustomPolicyForHTTPS(extra_ca_certs)
            agent = Agent(reactor, contextFactory=policy)
            self.agent = agent

    def mod_headers(self, h):
        keymap = {}
        for k,v in h.iteritems():
            key = k.lower()
            if key in keymap:
                keymap[key].append(k)
            else:
                keymap[key] = [k]
                
        if 'host' in keymap:
            for k in keymap['host']:
                h[k] = [self.proxied_netloc]
        if 'origin' in keymap:
            for k in keymap['origin']:
                h[k] = [self.proxied_netloc]
        if 'content-length' in keymap:
            for k in keymap['content-length']:
                del h[k]
                
        if 'referer' in keymap:
            for k in keymap['referer']:
                del h[k]
        if False:
            keys = keymap['referer']
            if len(keys) == 1:
                k = keys[0]
                values = h[k]
                if len(values) == 1:
                    referer = values[0]
                    new_referer = self.proxy_url_to_proxied_url(referer)
                    if new_referer is not None:
                        h[k] = [new_referer]
                        log.msg("[DEBUG] Re-wrote Referer header: '%s' => '%s'" % (referer, new_referer))
        return h

    def _check_for_logout(self, request):
        """
        """
        data = request.content.read()
        samlp_ns = "{urn:oasis:names:tc:SAML:2.0:protocol}"
        try:
            root = etree.fromstring(data)
        except Exception as ex:
            log.msg("[DEBUG] Not XML.\n%s" % str(ex))
            root = None
        if (root is not None) and (root.tag == "%sLogoutRequest" % samlp_ns):
            instant = root.get('IssueInstant')
            if instant is not None:
                log.msg("[DEBUG] instant string == '%s'" % instant)
                try:
                    instant = parse_date(instant)
                except ValueError:
                    log.msg("[WARN] Odd issue_instant supplied: '%s'." % instant)
                    instant = None
                if instant is not None:
                    utcnow = datetime.datetime.utcnow()
                    log.msg("[DEBUG] UTC now == %s" % utcnow.strftime("%Y-%m-%dT%H:%M:%S"))
                    seconds = abs((utcnow - instant.replace(tzinfo=None)).total_seconds())
                    if seconds <= self.logout_instant_skew:
                        results = root.findall("%sSessionIndex" % samlp_ns)
                        if len(results) == 1:
                            result = results[0]
                            ticket = result.text
                            log.msg("[INFO] Received request to logout session with ticket '%s'." % ticket)
                            sess_uid = self.logout_tickets.get(ticket, None)
                            if sess_uid is not None:
                                self._expired(sess_uid)
                                return True
                            else:
                                log.msg("[WARN] No matching session for logout request for ticket '%s'." % ticket)
                    else:
                        log.msg("[DEBUG] Issue instant was not within %d seconds of actual time." % self.logout_instant_skew)
                else:
                    log.msg("[DEBUG] Could not parse issue instant.")
            else:
                log.msg("[DEBUG] 'IssueInstant' attribute missing from root.")
        elif root is None:
            log.msg("[DEBUG] Could not parse XML.")
        else:
            log.msg("[DEBUG] root.tag == '%s'" % root.tag)
            
        return False

    @app.route("/", branch=True)
    def proxy(self, request):
        """
        """
        valid_sessions = self.valid_sessions
        sess = request.getSession()
        sess_uid = sess.uid
        if not sess_uid in valid_sessions:
            if request.method == 'POST':
                headers = request.requestHeaders
                if headers.hasHeader("Content-Type"):
                    ct_list =  headers.getRawHeaders("Content-Type") 
                    log.msg("[DEBUG] ct_list: %s" % str(ct_list))
                    for ct in ct_list:
                        if ct.find('text/xml') != -1 or ct.find('application/xml') != -1:
                            if self._check_for_logout(request):
                                return ""
                            else:
                                # If reading the body failed the first time, it won't succeed later!
                                log.msg("[DEBUG] _check_for_logout() returned failure.")
                                break
                else:
                    log.msg("[DEBUG] No content-type.")
                            
            # CAS Authentication
            # Does this request have a ticket?  I.e. is it coming back from a successful
            # CAS authentication?
            args = request.args
            ticket_name = self.ticket_name
            if ticket_name in args:
                values = args[ticket_name]
                if len(values) == 1:
                    ticket = values[0]
                    d = self.validate_ticket(ticket, request)
                    return d
            # No ticket (or a problem with the ticket)?
            # Off to CAS you go!
            d = self.redirect_to_cas_login(request)
            return d
        else:
            d = self.reverse_proxy(request)
            return d
        
    #def clean_params(self, qs_map):
    #    """
    #    Remove any CAS-specific parameters from a query string map.
    #    """
    #    cas_param_names = self.cas_param_names
    #    del_keys = []
    #    for k, v in qs_map.iteritems():
    #        if k.lower() in cas_param_names:
    #            del_keys.append(k)
    #    for k in del_keys:
    #        del qs_map[k]
        
    def get_url(self, request):
        """
        """
        fqdn = self.fqdn
        port = self.port
        if port is None:
            port = 443
        if port == 443:
            return urlparse.urljoin("https://%s" % fqdn, request.uri)
        else:
            return urlparse.urljoin("https://%s:%d" % (fqdn, port), request.uri)
        
    def redirect_to_cas_login(self, request):
        """
        """
        cas_info = self.cas_info
        login_url = cas_info['login_url']
                
        p = urlparse.urlparse(login_url)
        
        params = {self.service_name: self.intercept_service_url(self.get_url(request), request)}
    
        if p.query == '':
            param_str = urlencode(params)
        else:
            qs_map = urlparse.parse_qs(p.query)
            qs_map.update(params)
            param_str = urlencode(qs_map)
        p = urlparse.ParseResult(*tuple(p[:4] + (param_str,) + p[5:]))
        
        url = urlparse.urlunparse(p)
        d = request.redirect(url)
        return d
        
    def validate_ticket(self, ticket, request):
        """
        """
        service_name = self.service_name
        ticket_name = self.ticket_name
        
        this_url = self.get_url(request)
        p = urlparse.urlparse(this_url)
        qs_map = urlparse.parse_qs(p.query)
        if ticket_name in qs_map:
            del qs_map[ticket_name]
        param_str = urlencode(qs_map)
        p = urlparse.ParseResult(*tuple(p[:4] + (param_str,) + p[5:]))
        service_url = urlparse.urlunparse(p)
        
        params = {
                service_name: service_url,
                ticket_name: ticket,}
        param_str = urlencode(params)
        p = urlparse.urlparse(self.cas_info['service_validate_url'])
        p = urlparse.ParseResult(*tuple(p[:4] + (param_str,) + p[5:]))
        service_validate_url = urlparse.urlunparse(p)
        
        log.msg("[INFO] requesting URL '%s' ..." % service_validate_url)
        kwds = {}
        kwds['agent'] = self.agent
        
        d = treq.get(service_validate_url, **kwds)
        d.addCallback(treq.content)
        d.addCallback(self.parse_sv_results, service_url, ticket, request)
        return d
        
    def parse_sv_results(self, payload, service_url, ticket, request):
        """
        """
        log.msg("[INFO] Parsing /serviceValidate results  ...")
        ns = self.ns
        root = etree.fromstring(payload)
        if root.tag != ('%sserviceResponse' % ns):
            return request.redirect(service_url)
        results = root.findall("%sauthenticationSuccess" % ns)
        if len(results) != 1:
            return request.redirect(service_url)
        success = results[0]
        results = success.findall("%suser" % ns)
        if len(results) != 1:
            return request.redirect(service_url)
        user = results[0]
        username = user.text
        
        # Update session session
        valid_sessions = self.valid_sessions
        logout_tickets = self.logout_tickets
        sess = request.getSession()
        sess_uid = sess.uid
        valid_sessions[sess_uid] = {
            'username': username,
            'ticket': ticket,}
        if not ticket in logout_tickets:
            logout_tickets[ticket] = sess_uid
            
        sess.notifyOnExpire(lambda: self._expired(sess_uid))
        
        # Reverse proxy.
        return request.redirect(service_url)
        
    def _expired(self, uid):
        """
        """
        valid_sessions = self.valid_sessions
        if uid in valid_sessions:
            session_info = valid_sessions[uid]
            username = session_info['username']
            ticket = session_info['ticket']
            del valid_sessions[uid]
            logout_tickets = self.logout_tickets
            if ticket in logout_tickets:
                del logout_tickets[ticket]
            log.msg("[INFO] label='Expired session.' session_id='%s' username='%s'" % (uid, username))
        
        
    def reverse_proxy(self, request):
        sess = request.getSession()
        valid_sessions = self.valid_sessions
        sess_uid = sess.uid
        username = valid_sessions[sess_uid]['username']
        # Normal reverse proxying.
        kwds = {}
        #cookiejar = cookielib.CookieJar()
        cookiejar = {}
        kwds['agent'] = self.agent
        kwds['allow_redirects'] = False
        kwds['cookies'] = cookiejar
        req_headers = self.mod_headers(dict(request.requestHeaders.getAllRawHeaders()))
        kwds['headers'] = req_headers
        kwds['headers']['REMOTE_USER'] = [username]
        #print "** HEADERS **"
        #pprint.pprint(self.mod_headers(dict(request.requestHeaders.getAllRawHeaders())))
        #print
        if request.method in ('PUT', 'POST'):
            kwds['data'] = request.content.read()
        #print "request.method", request.method
        #print "url", self.proxied_url + request.uri
        #print "kwds:"
        #pprint.pprint(kwds)
        #print
        url = self.proxied_url + request.uri
        log.msg("[INFO] Proxying URL: %s" % url)
        d = treq.request(request.method, url, **kwds)
        #print "** Requesting %s %s" % (request.method, self.proxied_url + request.uri)
        def process_response(response, request):
            req_resp_headers = request.responseHeaders
            resp_code = response.code
            resp_headers = response.headers
            resp_header_map = dict(resp_headers.getAllRawHeaders())
            # Rewrite Location headers for redirects as required.
            if resp_code in (301, 302, 303, 307, 308) and "Location" in resp_header_map:
                values = resp_header_map["Location"]
                if len(values) == 1:
                    location = values[0]
                    new_location = self.proxied_url_to_proxy_url(location)
                    if new_location is not None:
                        resp_header_map['Location'] = [new_location]
                        log.msg("[DEBUG] Re-wrote Location header: '%s' => '%s'" % (location, new_location))
            request.setResponseCode(response.code, message=response.phrase)
            for k,v in resp_header_map.iteritems():
                if k == 'Set-Cookie':
                    v = self.mod_cookies(v)
                print "Browser Response >>> Setting response header: %s: %s" % (k, v)
                req_resp_headers.setRawHeaders(k, v)
            return response
            
        def show_cookies(resp):
            jar = resp.cookies()
            print "Cookie Jar:"
            pprint.pprint(cookiejar)
            print ""
            return resp
            
        def mod_content(body, request):
            """
            """
            handler = self.resource_handlers.get(request.uri)
            if handler is not None:
                body = handler(body)
            return body
            
        d.addCallback(show_cookies)
        d.addCallback(process_response, request)
        d.addCallback(treq.content)
        d.addCallback(mod_content, request)
        return d
    
    def mod_cookies(self, value_list):
        """
        """
        proxied_path = self.proxied_path
        proxied_path_size = len(proxied_path)
        results = []
        for cookie_value in value_list:
            c = Cookie.SimpleCookie()
            c.load(cookie_value)
            for k in c.keys():
                m = c[k]
                if m.has_key('path'):
                    m_path = m['path']
                    if self.is_proxy_path_or_child(m_path):
                        m_path = m_path[proxied_path_size:]
                        m['path'] = m_path
            results.append(c.output(header='')[1:])
        return results
                     
    def is_proxy_path_or_child(self, path):
        """
        """
        return proxyutils.is_proxy_path_or_child(self.proxied_path, path)
    
    def proxied_url_to_proxy_url(self, target_url):
        """
        """
        return proxyutils.proxied_url_to_proxy_url(
            self.fqdn, 
            self.port, 
            self.proxied_netloc, 
            self.proxied_path, 
            target_url)
        
    def proxy_url_to_proxied_url(self, target_url):
        """
        """
        return proxyutils.proxy_url_to_proxied_url(
            self.fqdn, 
            self.port, 
            self.proxied_netloc,
            self.proxied_path,
            target_url)
        
    def csrf_js_hack(self, s):
        """
        """
        s = s.replace(self.proxied_host, self.fqdn)
        s = s.replace('''part = "/grouper/" + url;''', '''part = "/" + url;''')
        s = s.replace('''"/grouper/grouperExternal/public/OwaspJavaScriptServlet"''', '''"/grouperExternal/public/OwaspJavaScriptServlet"''');
        return s

    def intercept_service_url(self, service_url, request):
        """
        """
        # Handle AJAX error failure CAS interception.
        p = urlparse.urlparse(service_url)
        params = urlparse.parse_qs(p.query)
        values = params.get('code', None)
        if values is not None and 'ajaxError' in values:
            p = urlparse.ParseResult(*tuple(p[:2] + ('/',) + p[3:4] + ('',) + p[5:]))
            url = urlparse.urlunparse(p)
            return url
        # Nothing to intercept.
        return service_url
                    

