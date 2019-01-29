# Copyright 2012 Vaibhav Bajpai
# Copyright 2009 Shikhar Bhushan
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import os
import sys
import socket
import getpass
import threading
import base64
from binascii import hexlify

try:
    import selectors
except ImportError:
    import selectors2 as selectors

from ncclient.capabilities import Capabilities
from ncclient.logging_ import SessionLoggerAdapter

import paramiko

from ncclient.transport.errors import AuthenticationError, SessionCloseError, SSHError, SSHUnknownHostError, NetconfFramingError
from ncclient.transport.session import Session
from ncclient.transport.session import NetconfBase
from ncclient.transport.parser import DefaultXMLParser
from ncclient.transport.parser import SAXFilterXMLNotFoundError

import logging
logger = logging.getLogger("ncclient.transport.ssh")

PORT_NETCONF_DEFAULT = 830
PORT_SSH_DEFAULT = 22

BUF_SIZE = 4096
# v1.0: RFC 4742
MSG_DELIM = "]]>]]>"
# v1.1: RFC 6242
END_DELIM = '\n##\n'

TICK = 0.1


def default_unknown_host_cb(host, fingerprint):
    """An unknown host callback returns `True` if it finds the key acceptable, and `False` if not.

    This default callback always returns `False`, which would lead to :meth:`connect` raising a :exc:`SSHUnknownHost` exception.

    Supply another valid callback if you need to verify the host key programmatically.

    *host* is the hostname that needs to be verified

    *fingerprint* is a hex string representing the host key fingerprint, colon-delimited e.g. `"4b:69:6c:72:6f:79:20:77:61:73:20:68:65:72:65:21"`
    """
    return False


def _colonify(fp):
    fp = fp.decode('UTF-8')
    finga = fp[:2]
    for idx in range(2, len(fp), 2):
        finga += ":" + fp[idx:idx+2]
    return finga


if sys.version < '3':
    def textify(buf):
        return buf
else:
    def textify(buf):
        return buf.decode('UTF-8')

if sys.version < '3':
    from six import StringIO
else:
    from io import BytesIO as StringIO


class SSHSession(Session):

    "Implements a :rfc:`4742` NETCONF session over SSH."

    def __init__(self, device_handler):
        capabilities = Capabilities(device_handler.get_capabilities())
        Session.__init__(self, capabilities)
        self._host = None
        self._host_keys = paramiko.HostKeys()
        self._transport = None
        self._connected = False
        self._channel = None
        self._channel_id = None
        self._channel_name = None
        self._buffer = StringIO()
        self._device_handler = device_handler
        self._message_list = []
        self._closing = threading.Event()
        self.parser = DefaultXMLParser(self)  # SAX or DOM parser

        self.logger = SessionLoggerAdapter(logger, {'session': self})

    def _dispatch_message(self, raw):
        self.logger.info("Received:\n%s", raw)
        return super(SSHSession, self)._dispatch_message(raw)

    def _parse(self):
        "Messages ae delimited by MSG_DELIM. The buffer could have grown by a maximum of BUF_SIZE bytes everytime this method is called. Retains state across method calls and if a byte has been read it will not be considered again."
        return self.parser._parse10()

    def load_known_hosts(self, filename=None):

        """Load host keys from an openssh :file:`known_hosts`-style file. Can
        be called multiple times.

        If *filename* is not specified, looks in the default locations i.e. :file:`~/.ssh/known_hosts` and :file:`~/ssh/known_hosts` for Windows.
        """

        if filename is None:
            filename = os.path.expanduser('~/.ssh/known_hosts')
            try:
                self._host_keys.load(filename)
            except IOError:
                # for windows
                filename = os.path.expanduser('~/ssh/known_hosts')
                try:
                    self._host_keys.load(filename)
                except IOError:
                    pass
        else:
            self._host_keys.load(filename)

    def close(self):
        self._closing.set()
        if self._transport.is_active():
            self._transport.close()

        # Wait for the transport thread to close.
        while self.is_alive() and (self is not threading.current_thread()):
            self.join(10)

        if self._channel:
            self._channel.close()
        self._channel = None
        self._connected = False

    # REMEMBER to update transport.rst if sig. changes, since it is hardcoded there
    def connect(
            self,
            host,
            port                = PORT_NETCONF_DEFAULT,
            timeout             = None,
            unknown_host_cb     = default_unknown_host_cb,
            username            = None,
            password            = None,
            key_filename        = None,
            allow_agent         = True,
            hostkey_verify      = True,
            hostkey_b64         = None,
            look_for_keys       = True,
            ssh_config          = None,
            sock_fd             = None):

        """Connect via SSH and initialize the NETCONF session. First attempts the publickey authentication method and then password authentication.

        To disable attempting publickey authentication altogether, call with *allow_agent* and *look_for_keys* as `False`.

        *host* is the hostname or IP address to connect to

        *port* is by default 830 (PORT_NETCONF_DEFAULT), but some devices use the default SSH port of 22 (PORT_SSH_DEFAULT) so this may need to be specified

        *timeout* is an optional timeout for socket connect

        *unknown_host_cb* is called when the server host key is not recognized. It takes two arguments, the hostname and the fingerprint (see the signature of :func:`default_unknown_host_cb`)

        *username* is the username to use for SSH authentication

        *password* is the password used if using password authentication, or the passphrase to use for unlocking keys that require it

        *key_filename* is a filename where a the private key to be used can be found

        *allow_agent* enables querying SSH agent (if found) for keys

        *hostkey_verify* enables hostkey verification from ~/.ssh/known_hosts

        *hostkey_b64* only connect when server presents a public hostkey matching this (obtain from server /etc/ssh/ssh_host_*pub or ssh-keyscan)

        *look_for_keys* enables looking in the usual locations for ssh keys (e.g. :file:`~/.ssh/id_*`)

        *ssh_config* enables parsing of an OpenSSH configuration file, if set to its path, e.g. :file:`~/.ssh/config` or to True (in this case, use :file:`~/.ssh/config`).

        *sock_fd* is an already open socket which shall be used for this connection. Useful for NETCONF outbound ssh. Use host=None together with a valid sock_fd number
        """
        if not (host or sock_fd):
            raise SSHError("Missing host or socket fd")

        self._host = host

        # Optionally, parse .ssh/config
        config = {}
        if ssh_config is True:
            ssh_config = "~/.ssh/config" if sys.platform != "win32" else "~/ssh/config"
        if ssh_config is not None:
            config = paramiko.SSHConfig()
            config.parse(open(os.path.expanduser(ssh_config)))
            config = config.lookup(host)
            host = config.get("hostname", host)
            if username is None:
                username = config.get("user")
            if key_filename is None:
                key_filename = config.get("identityfile")
            if hostkey_verify:
                userknownhostsfile = config.get("userknownhostsfile")
                if userknownhostsfile:
                    self.load_known_hosts(os.path.expanduser(userknownhostsfile))

        if username is None:
            username = getpass.getuser()

        if sock_fd is None:
            if config.get("proxycommand"):
                sock = paramiko.proxy.ProxyCommand(config.get("proxycommand"))
            else:
                for res in socket.getaddrinfo(host, port, socket.AF_UNSPEC, socket.SOCK_STREAM):
                    af, socktype, proto, canonname, sa = res
                    try:
                        sock = socket.socket(af, socktype, proto)
                        sock.settimeout(timeout)
                    except socket.error:
                        continue
                    try:
                        sock.connect(sa)
                    except socket.error:
                        sock.close()
                        continue
                    break
                else:
                    raise SSHError("Could not open socket to %s:%s" % (host, port))
        else:
            if sys.version_info[0] < 3:
                s = socket.fromfd(int(sock_fd), socket.AF_INET, socket.SOCK_STREAM)
                sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM, _sock=s)
            else:
                sock = socket.fromfd(int(sock_fd), socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(timeout)

        self._transport = paramiko.Transport(sock)
        self._transport.set_log_channel(logger.name)
        if config.get("compression") == 'yes':
            self._transport.use_compression()

        if hostkey_b64:
            # If we need to connect with a specific hostkey, negotiate for only its type
            hostkey_obj = None
            for key_cls in [paramiko.DSSKey, paramiko.Ed25519Key, paramiko.RSAKey, paramiko.ECDSAKey]:
                try:
                    hostkey_obj = key_cls(data=base64.b64decode(hostkey_b64))
                except paramiko.SSHException:
                    # Not a key of this type - try the next
                    pass
            if not hostkey_obj:
                # We've tried all known host key types and haven't found a suitable one to use - bail
                raise SSHError("Couldn't find suitable paramiko key class for host key %s" % hostkey_b64)
            self._transport._preferred_keys = [hostkey_obj.get_name()]
        elif self._host_keys:
            # Else set preferred host keys to those we possess for the host
            # (avoids situation where known_hosts contains a valid key for the host, but that key type is not selected during negotiation)
            if port == PORT_SSH_DEFAULT:
                known_hosts_lookup = host
            else:
                known_hosts_lookup = '[%s]:%s' % (host, port)
            known_host_keys_for_this_host = self._host_keys.lookup(known_hosts_lookup)
            if known_host_keys_for_this_host:
                self._transport._preferred_keys = [x.key.get_name() for x in known_host_keys_for_this_host._entries]

        # Connect
        try:
            self._transport.start_client()
        except paramiko.SSHException as e:
            raise SSHError('Negotiation failed: %s' % e)

        server_key_obj = self._transport.get_remote_server_key()
        fingerprint = _colonify(hexlify(server_key_obj.get_fingerprint()))

        if hostkey_verify:
            is_known_host = False

            # For looking up entries for nonstandard (22) ssh ports in known_hosts
            # we enclose host in brackets and append port number
            if port == PORT_SSH_DEFAULT:
                known_hosts_lookup = host
            else:
                known_hosts_lookup = '[%s]:%s' % (host, port)

            if hostkey_b64:
                # If hostkey specified, remote host /must/ use that hostkey
                if(hostkey_obj.get_name() == server_key_obj.get_name() and hostkey_obj.asbytes() == server_key_obj.asbytes()):
                    is_known_host = True
            else:
                # Check known_hosts
                is_known_host = self._host_keys.check(known_hosts_lookup, server_key_obj)

            if not is_known_host and not unknown_host_cb(host, fingerprint):
                raise SSHUnknownHostError(known_hosts_lookup, fingerprint)

        # Authenticating with our private key/identity
        if key_filename is None:
            key_filenames = []
        elif isinstance(key_filename, (str, bytes)):
            key_filenames = [key_filename]
        else:
            key_filenames = key_filename

        self._auth(username, password, key_filenames, allow_agent, look_for_keys)

        self._connected = True      # there was no error authenticating
        self._closing.clear()

        # TODO: leopoul: Review, test, and if needed rewrite this part
        subsystem_names = self._device_handler.get_ssh_subsystem_names()
        for subname in subsystem_names:
            self._channel = self._transport.open_session()
            self._channel_id = self._channel.get_id()
            channel_name = "%s-subsystem-%s" % (subname, str(self._channel_id))
            self._channel.set_name(channel_name)
            try:
                self._channel.invoke_subsystem(subname)
            except paramiko.SSHException as e:
                self.logger.info("%s (subsystem request rejected)", e)
                handle_exception = self._device_handler.handle_connection_exceptions(self)
                # Ignore the exception, since we continue to try the different
                # subsystem names until we find one that can connect.
                # have to handle exception for each vendor here
                if not handle_exception:
                    continue
            self._channel_name = self._channel.get_name()
            self._post_connect()
            # for further upcoming RPC responses, vendor can chose their
            # choice of parser. Say DOM or SAX
            self.parser = self._device_handler.get_xml_parser(self)
            return
        raise SSHError("Could not open connection, possibly due to unacceptable"
                       " SSH subsystem name.")

    def _auth(self, username, password, key_filenames, allow_agent,
              look_for_keys):
        saved_exception = None

        for key_filename in key_filenames:
            for cls in (paramiko.RSAKey, paramiko.DSSKey, paramiko.ECDSAKey):
                try:
                    key = cls.from_private_key_file(key_filename, password)
                    self.logger.debug("Trying key %s from %s",
                                      hexlify(key.get_fingerprint()),
                                      key_filename)
                    self._transport.auth_publickey(username, key)
                    return
                except Exception as e:
                    saved_exception = e
                    self.logger.debug(e)

        if allow_agent:
            for key in paramiko.Agent().get_keys():
                try:
                    self.logger.debug("Trying SSH agent key %s",
                                      hexlify(key.get_fingerprint()))
                    self._transport.auth_publickey(username, key)
                    return
                except Exception as e:
                    saved_exception = e
                    self.logger.debug(e)

        keyfiles = []
        if look_for_keys:
            rsa_key = os.path.expanduser("~/.ssh/id_rsa")
            dsa_key = os.path.expanduser("~/.ssh/id_dsa")
            ecdsa_key = os.path.expanduser("~/.ssh/id_ecdsa")
            if os.path.isfile(rsa_key):
                keyfiles.append((paramiko.RSAKey, rsa_key))
            if os.path.isfile(dsa_key):
                keyfiles.append((paramiko.DSSKey, dsa_key))
            if os.path.isfile(ecdsa_key):
                keyfiles.append((paramiko.ECDSAKey, ecdsa_key))
            # look in ~/ssh/ for windows users:
            rsa_key = os.path.expanduser("~/ssh/id_rsa")
            dsa_key = os.path.expanduser("~/ssh/id_dsa")
            ecdsa_key = os.path.expanduser("~/ssh/id_ecdsa")
            if os.path.isfile(rsa_key):
                keyfiles.append((paramiko.RSAKey, rsa_key))
            if os.path.isfile(dsa_key):
                keyfiles.append((paramiko.DSSKey, dsa_key))
            if os.path.isfile(ecdsa_key):
                keyfiles.append((paramiko.ECDSAKey, ecdsa_key))

        for cls, filename in keyfiles:
            try:
                key = cls.from_private_key_file(filename, password)
                self.logger.debug("Trying discovered key %s in %s",
                                  hexlify(key.get_fingerprint()), filename)
                self._transport.auth_publickey(username, key)
                return
            except Exception as e:
                saved_exception = e
                self.logger.debug(e)

        if password is not None:
            try:
                self._transport.auth_password(username, password)
                return
            except Exception as e:
                saved_exception = e
                self.logger.debug(e)

        if saved_exception is not None:
            # need pep-3134 to do this right
            raise AuthenticationError(repr(saved_exception))

        raise AuthenticationError("No authentication methods available")

    def run(self):
        chan = self._channel
        q = self._q

        def start_delim(data_len): return '\n#%s\n' % (data_len)

        try:
            s = selectors.DefaultSelector()
            s.register(chan, selectors.EVENT_READ)
            self.logger.debug('selector type = %s', s.__class__.__name__)
            while True:

                # Will wakeup evey TICK seconds to check if something
                # to send, more quickly if something to read (due to
                # select returning chan in readable list).
                events = s.select(timeout=TICK)
                if events:
                    data = chan.recv(BUF_SIZE)
                    if data:
                        try:
                            self.parser.parse(data)
                        except SAXFilterXMLNotFoundError:
                            self.logger.debug('switching from sax to dom parsing')
                            self.parser = DefaultXMLParser(self)
                            self.parser.parse(data)
                    elif self._closing.is_set():
                        # End of session, expected
                        break
                    else:
                        # End of session, unexpected
                        raise SessionCloseError(self._buffer.getvalue())
                if not q.empty() and chan.send_ready():
                    self.logger.debug("Sending message")
                    data = q.get()
                    if self._base == NetconfBase.BASE_11:
                        data = "%s%s%s" % (start_delim(len(data)), data, END_DELIM)
                    else:
                        data = "%s%s" % (data, MSG_DELIM)
                    self.logger.info("Sending:\n%s", data)
                    while data:
                        n = chan.send(data)
                        if n <= 0:
                            raise SessionCloseError(self._buffer.getvalue(), data)
                        data = data[n:]
        except Exception as e:
            self.logger.debug("Broke out of main loop, error=%r", e)
            self._dispatch_error(e)
            self.close()

    @property
    def host(self):
        """Host this session is connected to, or None if not connected."""
        if hasattr(self, '_host'):
            return self._host
        return None

    @property
    def transport(self):
        "Underlying `paramiko.Transport <http://www.lag.net/paramiko/docs/paramiko.Transport-class.html>`_ object. This makes it possible to call methods like :meth:`~paramiko.Transport.set_keepalive` on it."
        return self._transport
