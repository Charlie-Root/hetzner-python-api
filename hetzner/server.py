import logging
import os
import random
import re
import string
import subprocess
import warnings
from datetime import datetime
from functools import reduce
from tempfile import mkdtemp

try:
    from urllib import urlencode
except ImportError:
    from urllib.parse import urlencode

from hetzner import RobotError, WebRobotError
from hetzner.rdns import ReverseDNS, ReverseDNSManager
from hetzner.reset import Reset
from hetzner.util import addr, scraping

__all__ = [
    "AdminAccount",
    "IpAddress",
    "RescueSystem",
    "Server",
    "Subnet",
    "IpManager",
    "SubnetManager",
]


class SSHAskPassHelper:
    """
    This creates a temporary SSH askpass helper script, which just passes the
    provided password.
    """

    def __init__(self, passwd):
        self.passwd = passwd
        self.tempdir = None
        self.script = None

    def __enter__(self):
        self.tempdir = mkdtemp()
        script = os.path.join(self.tempdir, "askpass")
        fd = os.open(script, os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW, 0o700)
        self.script = script
        esc_passwd = self.passwd.replace("'", r"'\''")
        askpass = f"#!/bin/sh\necho -n '{esc_passwd}'".encode("ascii")
        os.write(fd, askpass)
        os.close(fd)
        return script

    def __exit__(self, type, value, traceback):
        if self.script is not None:
            os.unlink(self.script)
        if self.tempdir is not None:
            os.rmdir(self.tempdir)


class RescueSystem:
    def __init__(self, server):
        self.server = server
        self.conn = server.conn

        self._active = None
        self._password = None
        self._authorized_keys = None

    def _update_status(self, data=None):
        if data is None:
            path = f"/boot/{self.server.number}/rescue"
            data = self.conn.get(path)
        rescue = data["rescue"]
        self._active = rescue["active"]
        self._password = rescue["password"]
        self._authorized_keys = rescue["authorized_key"]

    @property
    def active(self):
        if self._active is not None:
            return self._active
        self._update_status()
        return self._active

    @property
    def password(self):
        if self._password is not None:
            return self._password
        self._update_status()
        return self._password

    @property
    def authorized_keys(self):
        if self._authorized_keys is not None:
            return self._authorized_keys
        self._update_status()
        return self._authorized_keys

    def _rescue_action(self, method, opts=None):
        reply = self.conn.request(method, f"/boot/{self.server.number}/rescue", opts)

        self._update_status(reply)

    def activate(self, bits=64, os="linux", authorized_keys=None):
        """
        Activate the rescue system if necessary.

        Note that 'authorized_keys', if passed, must be a list of fingerprints,
        e.g. ['a3:14:62:38:d1:45:35:6c:de:ad:ec:12:be:93:24:ef'] of the keys
        that should have been added to robot already.
        """
        if not self.active:
            opts = {"os": os, "arch": bits}
            if authorized_keys is not None:
                opts["authorized_key"] = list(authorized_keys)
            return self._rescue_action("post", opts)

    def deactivate(self):
        """
        Deactivate the rescue system if necessary.
        """
        if self.active:
            return self._rescue_action("delete")

    def observed_activate(self, *args, **kwargs):
        """
        Activate the rescue system and reboot into it.
        Look at Server.observed_reboot() for options.
        """
        self.activate(authorized_keys=kwargs.pop("authorized_keys", None))
        self.server.observed_reboot(*args, **kwargs)

    def observed_deactivate(self, *args, **kwargs):
        """
        Deactivate the rescue system and reboot into normal system.
        Look at Server.observed_reboot() for options.
        """
        self.deactivate()
        self.server.observed_reboot(*args, **kwargs)

    def shell(self, *args, **kwargs):
        """
        Reboot into rescue system, spawn a shell and after the shell is
        closed, reboot back into the normal system.

        Look at Server.observed_reboot() for further options.
        """
        msg = (
            "The RescueSystem.shell() method will be removed from the API"
            " in version 1.0.0, please do not use it! See"
            " https://github.com/aszlig/hetzner/issues/13"
            " for details."
        )
        warnings.warn(msg, FutureWarning)
        self.observed_activate(*args, **kwargs)

        with SSHAskPassHelper(self.password) as askpass:
            ssh_options = [
                "CheckHostIP=no",
                "GlobalKnownHostsFile=/dev/null",
                "UserKnownHostsFile=/dev/null",
                "StrictHostKeyChecking=no",
                "LogLevel=quiet",
            ]
            ssh_args = reduce(lambda acc, opt: acc + ["-o", opt], ssh_options, [])
            cmd = ["ssh"] + ssh_args + [f"root@{self.server.ip}"]
            env = dict(os.environ)
            env["DISPLAY"] = ":666"
            env["SSH_ASKPASS"] = askpass
            subprocess.check_call(cmd, env=env, preexec_fn=os.setsid)

        self.observed_deactivate(*args, **kwargs)


class AdminAccount:
    def __init__(self, server):
        # XXX: This is preliminary, because we don't have such functionality in
        #      the official API yet.
        self._scraper = server.conn.scraper
        self._serverid = server.number
        self.exists = False
        self.login = None
        self.passwd = None
        self.update_info()

    def update_info(self):
        """
        Get information about currently active admin login.
        """
        self._scraper.login()
        login_re = re.compile(r'"label_req">Login.*?"element">([^<]+)', re.DOTALL)

        path = f"/server/admin/id/{self._serverid}"
        response = self._scraper.request(path)
        assert response.status == 200
        match = login_re.search(response.read().decode("utf-8"))
        if match is None:
            self.exists = False
        else:
            self.exists = True
            self.login = match.group(1)

    def _genpasswd(self):
        random.seed(os.urandom(512))
        chars = string.ascii_letters + string.digits + "/()-=+_,;.^~#*@"
        length = random.randint(20, 40)
        return "".join(random.choice(chars) for i in range(length))

    def create(self, passwd=None):
        """
        Create a new admin account if missing. If passwd is supplied, use it
        instead of generating a random one.
        """
        if passwd is None:
            passwd = self._genpasswd()

        form_path = f"/server/admin/id/{self._serverid}"
        form_response = self._scraper.request(form_path, method="POST")

        parser = scraping.CSRFParser("password[_csrf_token]")
        parser.feed(form_response.read().decode("utf-8"))
        assert parser.csrf_token is not None

        data = {
            "password[new_password]": passwd,
            "password[new_password_repeat]": passwd,
            "password[_csrf_token]": parser.csrf_token,
        }

        if not self.exists:
            failmsg = "Unable to create admin account"
            path = f"/server/adminCreate/id/{self._serverid}"
        else:
            failmsg = "Unable to update admin account password"
            path = "/server/adminUpdate"
            data["id"] = self._serverid

        response = self._scraper.request(path, data)
        data = response.read().decode("utf-8")
        if "msgbox_success" not in data:
            ul_re = re.compile(r'<ul\s+class="error_list">(.*?)</ul>', re.DOTALL)
            li_re = re.compile(r"<li>\s*([^<]*?)\s*</li>")
            ul_match = ul_re.search(data)
            if ul_match is not None:
                errors = [error.group(1) for error in li_re.finditer(ul_match.group(0))]
                msg = failmsg + ": " + ", ".join(errors)
                raise WebRobotError(msg)
            raise WebRobotError(failmsg)
        self.update_info()
        self.passwd = passwd
        return self.login, self.passwd

    def delete(self):
        """
        Remove the admin account.
        """
        if not self.exists:
            return
        path = f"/server/adminDelete/id/{self._serverid}"
        assert "msgbox_success" in self._scraper.request(path).read().decode("utf-8")
        self.update_info()

    def __repr__(self):
        if self.exists:
            return f"<AdminAccount login: {self.login}>"
        else:
            return "<AdminAccount missing>"


class IpAddress:
    def __init__(self, conn, result, subnet_ip=None):
        self.conn = conn
        self.subnet_ip = subnet_ip
        self.update_info(result)
        self._rdns = None

    @property
    def rdns(self):
        """
        Get or set reverse DNS PTRs.
        """
        if self._rdns is None:
            self._rdns = ReverseDNS(self.conn, self.ip)
        return self._rdns

    def update_info(self, result=None):
        """
        Update the information of the current IP address and all related
        information such as traffic warnings. If result is omitted, a new
        request is sent to the robot to gather the information.
        """
        if self.subnet_ip is not None:
            if result is None:
                result = self.conn.get(f"/subnet/{self._subnet_addr}")
            data = result["subnet"]
            self._subnet_addr = data["ip"]
            data["ip"] = self.subnet_ip
            # Does not exist in subnets
            data["separate_mac"] = None
        else:
            if result is None:
                result = self.conn.get(f"/ip/{self.ip}")
            data = result["ip"]

        self.ip = data["ip"]
        self.server_ip = data["server_ip"]
        self.locked = data["locked"]
        self.separate_mac = data["separate_mac"]
        self.traffic_warnings = data["traffic_warnings"]
        self.traffic_hourly = data["traffic_hourly"]
        self.traffic_daily = data["traffic_daily"]
        self.traffic_monthly = data["traffic_monthly"]

    def __repr__(self):
        return f"<IpAddress {self.ip}>"


class IpManager:
    def __init__(self, conn, main_ip):
        self.conn = conn
        self.main_ip = main_ip

    def get(self, ip):
        """
        Get a specific IP address of a server.
        """
        return IpAddress(self.conn, self.conn.get(f"/ip/{ip}"))

    def __iter__(self):
        data = urlencode({"server_ip": self.main_ip})
        
        try:
            result = self.conn.get(f"/ip?{data}")
        except RobotError as error:
            result = []
            
        return iter([IpAddress(self.conn, ip) for ip in result])


class Subnet:
    def __init__(self, conn, result):
        self.conn = conn
        self.update_info(result)

    def update_info(self, result=None):
        """
        Update the information of the subnet. If result is omitted, a new
        request is sent to the robot to gather the information.
        """
        if result is None:
            result = self.conn.get(f"/subnet/{self.net_ip}")

        data = result["subnet"]

        self.net_ip = data["ip"]
        self.mask = data["mask"]
        self.gateway = data["gateway"]
        self.server_ip = data["server_ip"]
        self.failover = data["failover"]
        self.locked = data["locked"]
        self.traffic_warnings = data["traffic_warnings"]
        self.traffic_hourly = data["traffic_hourly"]
        self.traffic_daily = data["traffic_daily"]
        self.traffic_monthly = data["traffic_monthly"]

        self.is_ipv6, self.numeric_net_ip = addr.parse_ipaddr(self.net_ip)
        self.numeric_gateway = addr.parse_ipaddr(self.gateway, self.is_ipv6)
        getrange = addr.get_ipv6_range if self.is_ipv6 else addr.get_ipv4_range
        self.numeric_range = getrange(self.numeric_net_ip, self.mask)

    def get_ip_range(self):
        """
        Return the smallest and biggest possible IP address of the current
        subnet.
        """
        convert = addr.ipv6_bin2addr if self.is_ipv6 else addr.ipv4_bin2addr
        return convert(self.numeric_range[0]), convert(self.numeric_range[1])

    def __contains__(self, addr):
        """
        Check whether a specific IP address is within the current subnet.
        """
        numeric_addr = addr.parse_ipaddr(addr, self.is_ipv6)
        return self.numeric_range[0] <= numeric_addr <= self.numeric_range[1]

    def get_ip(self, addr):
        """
        Return an IpAddress object for the specified IPv4 or IPv6 address or
        None if the IP address doesn't exist in the current subnet.
        """
        if addr in self:
            result = self.conn.get(f"/subnet/{self.net_ip}")
            return IpAddress(self.conn, result, addr)
        else:
            return None

    def __repr__(self):
        return "<Subnet {}/{} (Gateway: {})>".format(
            self.net_ip, self.mask, self.gateway
        )


class SubnetManager:
    def __init__(self, conn, main_ip):
        self.conn = conn
        self.main_ip = main_ip

    def get(self, net_ip):
        """
        Get a specific subnet of a server.
        """
        return Subnet(self.conn, self.conn.get(f"/subnet/{net_ip}"))

    def __iter__(self):
        data = urlencode({"server_ip": self.main_ip})
        try:
            result = self.conn.get(f"/subnet?{data}")
        except RobotError as err:
            # If there are no subnets a 404 is returned rather than just an
            # empty list.
            if err.status == 404:
                result = []
        return iter([Subnet(self.conn, net) for net in result])


class Server:
    def __init__(self, conn, result):
        self.conn = conn
        self.update_info(result)
        self.rescue = RescueSystem(self)
        self.reset = Reset(self)
        self.ips = IpManager(self.conn, self.ip)
        #self.subnets = SubnetManager(self.conn, self.ip)
        self.rdns = ReverseDNSManager(self.conn, self.ip)
        self._admin_account = None
        self.logger = logging.getLogger(f"Server #{self.number}")

    @property
    def admin(self):
        """
        Update, create and delete admin accounts.
        """
        if self._admin_account is None:
            self._admin_account = AdminAccount(self)
        return self._admin_account

    def update_info(self, result=None):
        """
        Updates the information of the current Server instance either by
        sending a new GET request or by parsing the response given by result.
        """
        if result is None:
            result = self.conn.get(f"/server/{self.ip}")

        data = result["server"]

        if data["server_ip"]:
            self.ip = data["server_ip"]
        elif data["server_ip"] is not None:
            self.ip = data["server_ip"]
        elif data["server_ipv6_net"] and data["server_ipv6_net"] is not None:
            self.ip = data["server_ipv6_net"] + "2"
        else:
            self.ip = None
          
        self.number = data["server_number"]
        self.name = data["server_name"]
        self.product = data["product"]
        self.datacenter = data["dc"]
        self.traffic = data["traffic"]
        self.status = data["status"]
        self.subnets = data["subnet"]
        self.cancelled = data["cancelled"]
        self.paid_until = datetime.strptime(data["paid_until"], "%Y-%m-%d")
        self.linked_storagebox = data["linked_storagebox"]

    def observed_reboot(self, *args, **kwargs):
        msg = (
            "Server.observed_reboot() is deprecated. Please use"
            " Server.reset.observed_reboot() instead."
        )
        warnings.warn(msg, DeprecationWarning)
        return self.reset.observed_reboot(*args, **kwargs)

    def reboot(self, *args, **kwargs):
        msg = (
            "Server.reboot() is deprecated. Please use"
            " Server.reset.reboot() instead."
        )
        warnings.warn(msg, DeprecationWarning)
        return self.reset.reboot(*args, **kwargs)

    def set_name(self, name):
        result = self.conn.post(f"/server/{self.ip}", {"server_name": name})
        self.update_info(result)

    def __repr__(self):
        return f"<{self.ip} (#{self.number} {self.product})>"
