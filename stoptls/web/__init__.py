import asyncio
import aiohttp.web
import bs4
import urllib.parse

from stoptls.web import regex
from stoptls.cache import InMemoryCache


HEADER_BLACKLIST = {
    'request': [
        'Upgrade-Insecure-Requests',
        'Host'
    ],
    'response': [
        'Strict-Transport-Security',
        'Content-Length',
        'Content-Encoding',
        'Transfer-Encoding',
        'Set-Cookie'
    ]
}


def strip_headers(headers, type_):
    for header in HEADER_BLACKLIST[type_]:
        headers.pop(header, None)

    return headers


class StopTLSProxy(object):
    def __init__(self):
        self._tcp_connector = aiohttp.TCPConnector(ttl_dns_cache=None)
        self.session = aiohttp.ClientSession(connector=self._tcp_connector,
                                             cookie_jar=aiohttp.DummyCookieJar())
        self.cache = InMemoryCache()

    async def strip(self, request):
        response = await self.proxy_request(request)
        stripped_response = await self.strip_response(response,
                                                      request.remote,
                                                      request.host)
        await stripped_response.prepare(request)
        return stripped_response

    async def proxy_request(self, request):
        # check if URL was previously stripped and cached
        if self.cache.has_url(request.remote,
                              request.host,
                              request.rel_url.human_repr()):
            scheme = 'https'
        else:
            scheme = 'http'

        query_params = self.unstrip_query_params(request.url.query,
                                                 request.remote)

        orig_headers = dict(request.headers)
        headers = strip_headers(orig_headers, 'request')
        try:
            parsed_origin = urllib.parse.urlsplit(headers['Origin'])
            if self.cache.has_domain(request.remote_ip, parsed_origin.netloc):
                headers['Origin'] = parsed_origin._replace(scheme='https').geturl()
        except KeyError:
            pass

        # Kill sesssions
        cookies = self.filter_incoming_cookies(request.cookies,
                                               request.remote,
                                               request.host)
        headers['Cookie'] = '; '.join(cookies)
        # TODO: possibly also remove certain types of auth (e.g. Authentication: Bearer)

        url = urllib.parse.urlunsplit((scheme,
                                       request.host,
                                       request.path,
                                       '',
                                       request.url.fragment))
        method = request.method.lower()
        data = request.content if request.can_read_body else None

        #TODO: possibly use built-in aiohttp.ClientSession cache to store cookies,
        # maybe by subclassing aiohttp.abc.AbstractCookieJar
        return await self.session.request(method,
                                          url,
                                          data=data,
                                          headers=headers,
                                          params=query_params,
                                          # max_redirects=100)
                                          allow_redirects=False)  # potentially set this to False to prevent auto-redirection)

    async def strip_response(self, response, remote_ip, host):
        # strip secure URLs from HTML and Javascript bodies
        if response.content_type == 'text/html':
            try:
                body = await response.text()
            except UnicodeDecodeError:
                raw_body = await response.read()
                body = raw_body.decode('utf-8')
            body = self.strip_html_body(body, remote_ip, host)
        elif response.content_type == 'application/javascript' or response.content_type == 'text/css':
            body = self.strip_text(await response.text(), remote_ip, host)
        else:
            body = await response.read()
            # response.release()

        headers = strip_headers(dict(response.headers), 'response')

        # strip secure URL from location header
        try:
            location = headers['Location']
            headers['Location'] = location.replace('https://', 'http://')
            self.cache.add_url(remote_ip, location)
        except KeyError:
            pass

        stripped_response = aiohttp.web.Response(body=body,
                                                 status=response.status,
                                                 headers=headers)

        # remove secure flag from cookies
        for name, value, directives in self.strip_cookies(response.cookies,
                                                          remote_ip,
                                                          response.url.host):
            stripped_response.set_cookie(name, value, **directives)

        return stripped_response

    def unstrip_query_params(self, query_params, remote_ip):
        unstripped_params = query_params.copy()
        for key, value in query_params.items():
            # unstrip URLs in path params
            if regex.UNSECURE_URL.fullmatch(value):
                parsed_url = urllib.parse.urlsplit(value)
                if self.cache.has_url(remote_ip,
                                      parsed_url.netloc,
                                      urllib.parse.urlunsplit(('',
                                                               '',
                                                               parsed_url.path,
                                                               parsed_url.query,
                                                               parsed_url.fragment))):
                    unstripped_params.update({key: parsed_url._replace(scheme='https').geturl()})

        return unstripped_params

    def strip_html_body(self, body, remote_ip, host):
        soup = bs4.BeautifulSoup(body, 'html.parser')
        secure_url_attrs = []

        def has_secure_url_attr(tag):
            found = False
            url_attrs = []
            for attr_name, attr_value in tag.attrs.items():
                if isinstance(attr_value, list):
                    attr_value = ' '.join(attr_value)

                if regex.SECURE_URL.fullmatch(attr_value):
                    url_attrs.append(attr_name)
                    self.cache.add_url(remote_ip, attr_value)
                    found = True
                elif regex.RELATIVE_URL.fullmatch(attr_value):
                    url_attrs.append(attr_name)
                    self.cache.add_url(remote_ip, attr_value, host=host)
                    found = True

            if url_attrs:
                secure_url_attrs.append(url_attrs)

            return found

        secure_tags = soup.find_all(has_secure_url_attr)

        for i, tag in enumerate(secure_tags):
            for attr in secure_url_attrs[i]:
                secure_url = tag[attr]
                if secure_url.startswith('/'):
                    tag[attr] = 'http://{}{}'.format(host, secure_url)
                else:
                    parsed_url = urllib.parse.urlsplit(secure_url)
                    tag[attr] = urllib.parse.urlunsplit(parsed_url._replace(scheme='http'))

        # strip secure URLs from <style> and <script> blocks
        css_or_script_tags = soup.find_all(regex.CSS_OR_SCRIPT)
        for tag in css_or_script_tags:
            if tag.string:
                tag.string = self.strip_text(tag.string, remote_ip, host)

        return str(soup)

    def strip_text(self, body, remote_ip, host):
        def generate_unsecure_replacement(secure_url):
            self.cache.add_url(remote_ip, secure_url.group(0))
            return 'http' + secure_url.group(1)

        def relative2absolute_url(relative_url):
            self.cache.add_url(remote_ip, relative_url.group(0), host)
            return 'http://{}{}'.format(host, relative_url)

        canonicalized_text = regex.RELATIVE_URL.sub(relative2absolute_url,
                                                    body)
        return regex.SECURE_URL.sub(generate_unsecure_replacement,
                                    canonicalized_text)

    def strip_cookies(self, cookies, remote_ip, host):
        for cookie_name, cookie_directives in cookies.items():
            # cache newly-set cookies
            self.cache.add_cookie(remote_ip,
                                  host,
                                  cookie_name)

            # remove "secure" directive
            cookie_directives.pop('secure', None)

            # aiohttp.web.Response.set_cookie doesn't allow "comment" directive
            # as a kwarg
            cookie_directives.pop('comment', None)

            stripped_directives = {}
            for directive_name, directive_value in cookie_directives.items():
                if directive_value and cookie_directives.isReservedKey(directive_name):
                    stripped_directives[directive_name.replace('-', '_')] = directive_value

            yield cookie_name, cookie_directives.value, stripped_directives

    def filter_incoming_cookies(self, cookies, remote_ip, host):
        for name, value in cookies.items():
            if self.cache.has_cookie(remote_ip,
                                     host,
                                     name):
                yield '{}={}'.format(name, value)


async def main():
    # HTTP is a special case because it uses aiohttp
    # rather than raw asyncio. As such, it differs in two ways
    #    1. It has a seperate, individual port/handler
    #    2. It uses loop.create_server instead of start_server,
    #       in order to adhere to the aiohttp documentation

    server = await asyncio\
        .get_running_loop()\
        .create_server(aiohttp.web.Server(StopTLSProxy().strip),
                       port=8080)
    print("======= Serving HTTP on 127.0.0.1:8080 ======")

    async with server:
        await server.serve_forever()