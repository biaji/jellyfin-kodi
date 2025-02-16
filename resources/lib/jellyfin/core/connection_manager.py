# -*- coding: utf-8 -*-

#################################################################################################

import json
import logging
import hashlib
import socket
import time
from datetime import datetime
from distutils.version import LooseVersion

from credentials import Credentials
from http import HTTP

#################################################################################################

LOG = logging.getLogger('JELLYFIN.'+__name__)
CONNECTION_STATE = {
    'Unavailable': 0,
    'ServerSelection': 1,
    'ServerSignIn': 2,
    'SignedIn': 3,
    'ServerUpdateNeeded': 5
}
CONNECTION_MODE = {
    'Local': 0,
    'Remote': 1,
    'Manual': 2
}

#################################################################################################

def get_server_address(server, mode):

    modes = {
        CONNECTION_MODE['Local']: server.get('LocalAddress'),
        CONNECTION_MODE['Remote']: server.get('RemoteAddress'),
        CONNECTION_MODE['Manual']: server.get('ManualAddress')
    }
    return modes.get(mode) or server.get('ManualAddress', server.get('LocalAddress', server.get('RemoteAddress')))


class ConnectionManager(object):

    min_server_version = "10.1.0"
    server_version = min_server_version
    user = {}
    server_id = None
    timeout = 10

    def __init__(self, client):

        LOG.debug("ConnectionManager initializing...")

        self.client = client
        self.config = client.config
        self.credentials = Credentials()

        self.http = HTTP(client)

    def __shortcuts__(self, key):
        LOG.debug("__shortcuts__(%r)", key)

        if key == "clear":
            return self.clear_data
        elif key == "servers":
            return self.get_available_servers()
        elif key in ("reconnect", "refresh"):
            return self.connect
        elif key == "login":
            return self.login
        elif key == "server":
            return self.get_server_info(self.server_id)
        elif key == "server-id":
            return self.server_id
        elif key == "server-version":
            return self.server_version
        elif key == "user-id":
            return self.jellyfin_user_id()
        elif key == "public-users":
            return self.get_public_users()
        elif key == "token":
            return self.jellyfin_token()
        elif key == "manual-server":
            return self.connect_to_address
        elif key == "connect-to-server":
            return self.connect_to_server
        elif key == "server-address":
            server = self.get_server_info(self.server_id)
            return get_server_address(server, server['LastConnectionMode'])
        elif key == "revoke-token":
            return self.revoke_token()
        elif key == "server-mode":
            server = self.get_server_info(self.server_id)
            return server['LastConnectionMode']

        return

    def __getitem__(self, key):
        LOG.debug("__getitem__(%r)", key)
        return self.__shortcuts__(key)

    def clear_data(self):

        LOG.info("connection manager clearing data")

        self.user = None
        credentials = self.credentials.get_credentials()
        credentials['Servers'] = list()
        self.credentials.get_credentials(credentials)

        self.config.auth(None, None)

    def revoke_token(self):

        LOG.info("revoking token")

        self['server']['AccessToken'] = None
        self.credentials.get_credentials(self.credentials.get_credentials())

        self.config['auth.token'] = None

    def get_available_servers(self):

        LOG.info("Begin getAvailableServers")

        # Clone the credentials
        credentials = self.credentials.get_credentials()
        found_servers = self._find_servers(self._server_discovery())

        if not found_servers and not credentials['Servers']: # back out right away, no point in continuing
            LOG.info("Found no servers")
            return list()

        servers = list(credentials['Servers'])
        self._merge_servers(servers, found_servers)

        try:
            servers.sort(key=lambda x: datetime.strptime(x['DateLastAccessed'], "%Y-%m-%dT%H:%M:%SZ"), reverse=True)
        except TypeError:
            servers.sort(key=lambda x: datetime(*(time.strptime(x['DateLastAccessed'], "%Y-%m-%dT%H:%M:%SZ")[0:6])), reverse=True)

        credentials['Servers'] = servers
        self.credentials.get_credentials(credentials)

        return servers

    def login(self, server, username, password=None, clear=True, options={}):

        if not username:
            raise AttributeError("username cannot be empty")

        if not server:
            raise AttributeError("server cannot be empty")

        try:
            request = {
                'type': "POST",
                'url': self.get_jellyfin_url(server, "Users/AuthenticateByName"),
                'json': {
                    'Username': username,
                    'Pw': password or ""
                }
            }

            result = self._request_url(request, False)
        except Exception as error:  # Failed to login
            LOG.exception(error)
            return False
        else:
            self._on_authenticated(result, options)

        return result

    def connect_to_address(self, address, options={}):

        if not address:
            return False

        address = self._normalize_address(address)

        def _on_fail():
            LOG.error("connectToAddress %s failed", address)
            return self._resolve_failure()

        try:
            public_info = self._try_connect(address, options=options)
        except Exception as error:
            LOG.exception(error)
            return _on_fail()
        else:
            LOG.info("connectToAddress %s succeeded", address)
            server = {
                'ManualAddress': address,
                'LastConnectionMode': CONNECTION_MODE['Manual']
            }
            self._update_server_info(server, public_info)
            server = self.connect_to_server(server, options)
            if server is False:
                return _on_fail()

            return server

    def connect_to_server(self, server, options={}):

        LOG.info("begin connectToServer")

        tests = []

        if server.get('LastConnectionMode') is not None and server.get('AccessToken'):
            tests.append(server['LastConnectionMode'])

        if CONNECTION_MODE['Manual'] not in tests:
            tests.append(CONNECTION_MODE['Manual'])
        if CONNECTION_MODE['Local'] not in tests:
            tests.append(CONNECTION_MODE['Local'])
        if CONNECTION_MODE['Remote'] not in tests:
            tests.append(CONNECTION_MODE['Remote'])

        # TODO: begin to wake server

        LOG.info("beginning connection tests")
        return self._test_next_connection_mode(tests, 0, server, options)

    def connect(self, options={}):

        LOG.info("Begin connect")
        return self._connect_to_servers(self.get_available_servers(), options)

    def jellyfin_user_id(self):
        return self.get_server_info(self.server_id)['UserId']

    def jellyfin_token(self):
        return self.get_server_info(self.server_id)['AccessToken']

    def get_server_info(self, server_id):

        if server_id is None:
            LOG.info("server_id is empty")
            return {}

        servers = self.credentials.get_credentials()['Servers']

        for server in servers:
            if server['Id'] == server_id:
                return server

    def get_public_users(self):
        return self.client.jellyfin.get_public_users()

    def get_jellyfin_url(self, base, handler):
        return "%s/emby/%s" % (base, handler)

    def _request_url(self, request, headers=True):

        request['timeout'] = request.get('timeout') or self.timeout
        if headers:
            self._get_headers(request)

        try:
            return self.http.request(request)
        except Exception as error:
            LOG.exception(error)
            raise

    def _add_app_info(self):
        return "%s/%s" % (self.config['app.name'], self.config['app.version'])

    def _get_headers(self, request):

        headers = request.setdefault('headers', {})

        if request.get('dataType') == "json":
            headers['Accept'] = "application/json"
            request.pop('dataType')

        headers['X-Application'] = self._add_app_info()
        headers['Content-type'] = request.get('contentType',
            'application/x-www-form-urlencoded; charset=UTF-8')

    def _connect_to_servers(self, servers, options):

        LOG.info("Begin connectToServers, with %s servers", len(servers))
        result = {}

        if len(servers) == 1:
            result = self.connect_to_server(servers[0], options)
            LOG.debug("resolving connectToServers with result['State']: %s", result)

            return result

        first_server = self._get_last_used_server()
        # See if we have any saved credentials and can auto sign in
        if first_server is not None and first_server['DateLastAccessed'] != "2001-01-01T00:00:00Z":
            result = self.connect_to_server(first_server, options)

            if result['State'] in (CONNECTION_STATE['SignedIn'], CONNECTION_STATE['Unavailable']):
                return result

        # Return loaded credentials if exists
        credentials = self.credentials.get_credentials()

        return {
            'Servers': servers,
            'State': result.get('State') or CONNECTION_STATE['ServerSelection'],
        }

    def _try_connect(self, url, timeout=None, options={}):

        url = self.get_jellyfin_url(url, "system/info/public")
        LOG.info("tryConnect url: %s", url)

        return self._request_url({
            'type': "GET",
            'url': url,
            'dataType': "json",
            'timeout': timeout,
            'verify': options.get('ssl'),
            'retry': False
        })

    def _test_next_connection_mode(self, tests, index, server, options):

        if index >= len(tests):
            LOG.info("Tested all connection modes. Failing server connection.")
            return self._resolve_failure()

        mode = tests[index]
        address = get_server_address(server, mode)
        enable_retry = False
        skip_test = False
        timeout = self.timeout

        LOG.info("testing connection mode %s with server %s", mode, server.get('Name'))

        if mode == CONNECTION_MODE['Local']:
            enable_retry = True
            timeout = 8

            if self._string_equals_ignore_case(address, server.get('ManualAddress')):
                LOG.info("skipping LocalAddress test because it is the same as ManualAddress")
                skip_test = True

        elif mode == CONNECTION_MODE['Manual']:
            if self._string_equals_ignore_case(address, server.get('LocalAddress')):
                enable_retry = True
                timeout = 8

        if skip_test or not address:
            LOG.info("skipping test at index: %s", index)
            return self._test_next_connection_mode(tests, index + 1, server, options)

        try:
            result = self._try_connect(address, timeout, options)

        except Exception:
            LOG.exception("test failed for connection mode %s with server %s", mode, server.get('Name'))

            if enable_retry:
                # TODO: wake on lan and retry
                return self._test_next_connection_mode(tests, index + 1, server, options)
            else:
                return self._test_next_connection_mode(tests, index + 1, server, options)
        else:
            if self._compare_versions(self._get_min_server_version(), result['Version']) == 1:
                LOG.warn("minServerVersion requirement not met. Server version: %s", result['Version'])
                return {
                    'State': CONNECTION_STATE['ServerUpdateNeeded'],
                    'Servers': [server]
                }
            else:
                LOG.info("calling onSuccessfulConnection with connection mode %s with server %s", mode, server.get('Name'))
                return self._on_successful_connection(server, result, mode, options)

    def _on_successful_connection(self, server, system_info, connection_mode, options):

        credentials = self.credentials.get_credentials()
        return self._after_connect_validated(server, credentials, system_info, connection_mode, True, options)

    def _resolve_failure(self):
        return {
            'State': CONNECTION_STATE['Unavailable']
        }

    def _get_min_server_version(self, val=None):

        if val is not None:
            self.min_server_version = val

        return self.min_server_version

    def _compare_versions(self, a, b):

        ''' -1 a is smaller
            1 a is larger
            0 equal
        '''
        a = LooseVersion(a)
        b = LooseVersion(b)

        if a < b:
            return -1

        if a > b:
            return 1

        return 0

    def _string_equals_ignore_case(self, str1, str2):
        return (str1 or "").lower() == (str2 or "").lower()

    def _server_discovery(self):

        MULTI_GROUP = ("<broadcast>", 7359)
        MESSAGE = "who is JellyfinServer?"

        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.settimeout(1.0) # This controls the socket.timeout exception

        sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 20)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        sock.setsockopt(socket.SOL_IP, socket.IP_MULTICAST_LOOP, 1)
        sock.setsockopt(socket.IPPROTO_IP, socket.SO_REUSEADDR, 1)

        LOG.debug("MultiGroup      : %s", str(MULTI_GROUP))
        LOG.debug("Sending UDP Data: %s", MESSAGE)

        servers = []

        try:
            sock.sendto(MESSAGE, MULTI_GROUP)
        except Exception as error:
            LOG.exception(error)
            return servers

        while True:
            try:
                data, addr = sock.recvfrom(1024) # buffer size
                servers.append(json.loads(data))

            except socket.timeout:
                LOG.info("Found Servers: %s", servers)
                return servers

            except Exception as e:
                LOG.exception("Error trying to find servers: %s", e)
                return servers

    def _get_last_used_server(self):

        servers = self.credentials.get_credentials()['Servers']

        if not len(servers):
            return

        try:
            servers.sort(key=lambda x: datetime.strptime(x['DateLastAccessed'], "%Y-%m-%dT%H:%M:%SZ"), reverse=True)
        except TypeError:
            servers.sort(key=lambda x: datetime(*(time.strptime(x['DateLastAccessed'], "%Y-%m-%dT%H:%M:%SZ")[0:6])), reverse=True)

        return servers[0]

    def _merge_servers(self, list1, list2):

        for i in range(0, len(list2), 1):
            try:
                self.credentials.add_update_server(list1, list2[i])
            except KeyError:
                continue

        return list1

    def _find_servers(self, found_servers):

        servers = []

        for found_server in found_servers:

            server = self._convert_endpoint_address_to_manual_address(found_server)

            info = {
                'Id': found_server['Id'],
                'LocalAddress': server or found_server['Address'],
                'Name': found_server['Name']
            } #TODO
            info['LastConnectionMode'] = CONNECTION_MODE['Manual'] if info.get('ManualAddress') else CONNECTION_MODE['Local']

            servers.append(info)
        else:
            return servers

    def _convert_endpoint_address_to_manual_address(self, info):

        if info.get('Address') and info.get('EndpointAddress'):
            address = info['EndpointAddress'].split(':')[0]

            # Determine the port, if any
            parts = info['Address'].split(':')
            if len(parts) > 1:
                port_string = parts[len(parts)-1]

                try:
                    address += ":%s" % int(port_string)
                    return self._normalize_address(address)
                except ValueError:
                    pass

        return None

    def _normalize_address(self, address):
        # Attempt to correct bad input
        address = address.strip()
        address = address.lower()

        if 'http' not in address:
            address = "http://%s" % address

        return address

    def _save_user_info_into_credentials(self, server, user):

        info = {
            'Id': user['Id'],
            'IsSignedInOffline': True
        }
        self.credentials.add_update_user(server, info)

    def _after_connect_validated(self, server, credentials, system_info, connection_mode, verify_authentication, options):

        if options.get('enableAutoLogin') == False:

            self.config['auth.user_id'] = server.pop('UserId', None)
            self.config['auth.token'] = server.pop('AccessToken', None)

        elif verify_authentication and server.get('AccessToken'):

            if self._validate_authentication(server, connection_mode, options) is not False:

                self.config['auth.user_id'] = server['UserId']
                self.config['auth.token'] = server['AccessToken']
                return self._after_connect_validated(server, credentials, system_info, connection_mode, False, options)

            return self._resolve_failure()

        self._update_server_info(server, system_info)
        self.server_version = system_info['Version']
        server['LastConnectionMode'] = connection_mode

        if options.get('updateDateLastAccessed') is not False:
            server['DateLastAccessed'] = datetime.now().strftime('%Y-%m-%dT%H:%M:%SZ')

        self.credentials.add_update_server(credentials['Servers'], server)
        self.credentials.get_credentials(credentials)
        self.server_id = server['Id']

        # Update configs
        self.config['auth.server'] = get_server_address(server, connection_mode)
        self.config['auth.server-name'] = server['Name']
        self.config['auth.server=id'] = server['Id']
        self.config['auth.ssl'] = options.get('ssl', self.config['auth.ssl'])

        result = {
            'Servers': [server]
        }

        result['State'] = CONNECTION_STATE['SignedIn'] if server.get('AccessToken') else CONNECTION_STATE['ServerSignIn']
        # Connected
        return result

    def _validate_authentication(self, server, connection_mode, options={}):

        try:
            system_info = self._request_url({
                'type': "GET",
                'url': self.get_jellyfin_url(get_server_address(server, connection_mode), "System/Info"),
                'verify': options.get('ssl'),
                'dataType': "json",
                'headers': {
                    'X-MediaBrowser-Token': server['AccessToken']
                }
            })
            self._update_server_info(server, system_info)
        except Exception as error:
            LOG.exception(error)

            server['UserId'] = None
            server['AccessToken'] = None

            return False

    def _update_server_info(self, server, system_info):

        if server is None or system_info is None:
            return

        server['Name'] = system_info['ServerName']
        server['Id'] = system_info['Id']

        if system_info.get('LocalAddress'):
            server['LocalAddress'] = system_info['LocalAddress']
        if system_info.get('WanAddress'):
            server['RemoteAddress'] = system_info['WanAddress']
        if 'MacAddress' in system_info:
            server['WakeOnLanInfos'] = [{'MacAddress': system_info['MacAddress']}]

    def _on_authenticated(self, result, options={}):

        credentials = self.credentials.get_credentials()

        self.config['auth.user_id'] = result['User']['Id']
        self.config['auth.token'] = result['AccessToken']

        for server in credentials['Servers']:
            if server['Id'] == result['ServerId']:
                found_server = server
                break
        else: return # No server found

        if options.get('updateDateLastAccessed') is not False:
            found_server['DateLastAccessed'] = datetime.now().strftime('%Y-%m-%dT%H:%M:%SZ')

        found_server['UserId'] = result['User']['Id']
        found_server['AccessToken'] = result['AccessToken']

        self.credentials.add_update_server(credentials['Servers'], found_server)
        self._save_user_info_into_credentials(found_server, result['User'])
        self.credentials.get_credentials(credentials)
