import asyncio
import aiohttp.web
import bs4
import urllib.parse
import re


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

# SECURE_URL = re.compile('^https://.+', flags=re.IGNORECASE)
SECURE_URL = re.compile('(https)(:\/\/[a-zA-z0-9.\/?\-#=&;%:~_$@+()]+)',
                        flags=re.IGNORECASE)
COOKIE_SECURE_FLAG = re.compile('Secure;?',
                                flags=re.IGNORECASE)

url_tracker = {}


def strip_headers(headers, type_):
    for header in HEADER_BLACKLIST[type_]:
        headers.pop(header, None)

    return headers


class Handler(object):
    def __init__(self):
        self._tcp_connector = aiohttp.TCPConnector(ttl_dns_cache=None)
        self.session = aiohttp.ClientSession(connector=self._tcp_connector,
                                             cookie_jar=aiohttp.DummyCookieJar())
        self.cache = {}

    async def strip(self, request):
        request['remote_socket'] = '{}:{}'.format(*request.transport.get_extra_info('peername'))
        response = await self.proxy_request(request)
        return await self.strip_response(response, request['remote_socket'])

    async def proxy_request(self, request):
        # check if URL was previously stripped and cached
        try:
            if (request.rel_url.human_repr()) in self.cache[request['remote_socket']][request.host]['rel_urls']:
                scheme = 'https'
        except KeyError:
            scheme = request.scheme
        else:
            scheme = request.scheme

        # Kill sesssions
        for cookie in request.cookies.keys():
            try:
                if cookie in self.cache[request['remote_socket']][request.host]['cookies']:
                    continue

                del request.cookies[cookie]
            except KeyError:
                del request.cookies[cookie]

        # potentially also remove certain types of auth (e.g. Authentication: Bearer)

        url = urllib.parse.urlunsplit((scheme,
                                       request.host,
                                       request.path,
                                       request.query_string,
                                       request.url.fragment))
        method = request.method.lower()
        data = request.content if request.body_exists else None

        orig_headers = dict(request.headers)
        headers = strip_headers(orig_headers, 'request')

        return await self.session.request(method,
                                          url,
                                          data=data,
                                          headers=headers,
                                          max_redirects=100)
                                          # allow_redirects=False)  # potentially set this to False to prevent auto-redirection)

    async def strip_response(self, response, remote_socket):
        # strip secure URLs from HTML and Javascript bodies
        if response.content_type == 'text/html':
            body = self.strip_html_body(await response.text(), remote_socket)
        elif response.content_type == 'application/javascript':
            body = self.strip_javascript_body(await response.text(), remote_socket)
        else:
            body = await response.read()
            # response.release()

        # strip secure URL from location header
        headers = strip_headers(dict(response.headers), 'response')
        try:
            location = headers['Location']
            headers['Location'] = location.replace('https://', 'http://')
            parsed_url = urllib.parse.urlsplit(location)
            rel_url = ''.join(parsed_url[2:4])
            self.cache.setdefault(remote_socket, {}).setdefault(parsed_url[1], {}).setdefault('rel_urls', set([])).add(rel_url)
        except KeyError:
            pass

        stripped_response = aiohttp.web.Response(body=body,
                                                 status=response.status,
                                                 headers=headers)

        # remove secure flag from cookies
        for cookie_name, cookie_directives in response.cookies.items():
            # cache newly-set cookies
            self.cache.setdefault(remote_socket, {}).setdefault(response.url.host, {}).setdefault('cookies', set([])).add(cookie_name)

            # remove "secure" directive
            cookie_directives.pop('secure', None)

            # aiohttp.web.Response.set_cookie doesn't allow "comment" directive
            cookie_directives.pop('comment', None)

            stripped_directives = {}
            for directive_name, directive_value in cookie_directives.items():
                if directive_value and cookie_directives.isReservedKey(directive_name):
                    stripped_directives[directive_name.replace('-', '_')] = directive_value

            stripped_response.set_cookie(cookie_name,
                                         cookie_directives.value,
                                         **stripped_directives)
        return stripped_response

    def strip_html_body(self, body, remote_socket):
        soup = bs4.BeautifulSoup(body, 'html.parser')
        secure_url_attrs = []

        def has_secure_url_attr(tag):
            found = False
            for attr_name, attr_value in tag.attrs.items():
                if isinstance(attr_value, list):
                    attr_value = ' '.join(attr_value)

                if SECURE_URL.fullmatch(attr_value):
                    secure_url_attrs.append(attr_name)
                    parsed_url = urllib.parse.urlsplit(attr_value)
                    rel_url = ''.join(parsed_url[2:4])
                    self.cache.setdefault(remote_socket, {}).setdefault(parsed_url[1], {}).setdefault('rel_urls', set([])).add(rel_url)
                    found = True

            return found

        secure_tags = soup.find_all(has_secure_url_attr)

        for index, tag in enumerate(secure_tags):
            secure_url = tag[secure_url_attrs[index]]
            tag[secure_url_attrs[index]] = secure_url.replace('https://', 'http://')

        # strip secure URLs from <script> blocks
        

        return str(soup)

    def strip_javascript_body(self, body, remote_socket):
        def generate_unsecure_replacement(secure_url):
            parsed_url = urllib.parse.urlsplit(secure_url.group(0))
            rel_url = ''.join(parsed_url[2:4])
            self.cache.setdefault(remote_socket, {}).setdefault(parsed_url[1], {}).setdefault('rel_urls', set([])).add(rel_url)
            return 'http' + secure_url.group(2)

        return SECURE_URL.sub(generate_unsecure_replacement, body)


async def web_main():
    # HTTP is a special case because it uses aiohttp
    # rather than raw asyncio. As such, it differs in two ways
    #    1. It has a seperate, individual port/handler
    #    2. It uses loop.start_server instead of create_server,
    #       in order to adhere to the aiohttp documentation

    handler = Handler()
    server = await asyncio.get_running_loop().create_server(aiohttp.web.Server(handler.strip),
                                                            None,
                                                            8080)
    print("======= Serving HTTP on 127.0.0.1:8080 ======")

    async with server:
        await server.serve_forever()


async def generic_tcp_main():
    server = await asyncio.start_server(lambda x: 1, '127.0.0.1', 8081)

    print("======= Serving generic TCP on 127.0.0.1:8081 ======")

    async with server:
        await server.serve_forever()


if __name__ == '__main__':
    loop = asyncio.get_event_loop()
    servers = asyncio.gather(web_main(), generic_tcp_main())
    # loop.run_until_complete(asyncio.wait([asyncio.ensure_future(web_main()), asyncio.ensure_future(generic_tcp_main())], return_when=asyncio.FIRST_EXCEPTION))
    loop.run_until_complete(servers)
