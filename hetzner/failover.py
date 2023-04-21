from hetzner import RobotError

__all__ = ["Failover", "FailoverManager"]


class Failover:
    ip = None
    server_ip = None
    server_number = None
    active_server_ip = None

    def __repr__(self):
        return f"{self.ip} (destination: {self.active_server_ip}, booked on {self.server_number} ({self.server_ip}))"

    def __init__(self, data):
        for attr, value in data.items():
            if hasattr(self, attr):
                setattr(self, attr, value)


class FailoverManager:
    def __init__(self, conn, servers):
        self.conn = conn
        self.servers = servers

    def list(self):
        failovers = {}
        try:
            ips = self.conn.get("/failover")
        except RobotError as err:
            if err.status == 404:
                return failovers
            else:
                raise
        for ip in ips:
            failover = Failover(ip.get("failover"))
            failovers[failover.ip] = failover
        return failovers

    def set(self, ip, new_destination):
        failovers = self.list()
        if ip not in failovers.keys():
            raise RobotError(
                f"Invalid IP address '{ip}'. Failover IP addresses are {failovers.keys()}"
            )
        failover = failovers.get(ip)
        if new_destination == failover.active_server_ip:
            raise RobotError(
                f"{new_destination} is already the active destination of failover IP {ip}"
            )
        available_dests = [s.ip for s in list(self.servers)]
        if new_destination not in available_dests:
            raise RobotError(
                "Invalid destination '%s'. "
                "The destination is not in your server list: %s"
                % (new_destination, available_dests)
            )
        result = self.conn.post(
            f"/failover/{ip}", {"active_server_ip": new_destination}
        )
        return Failover(result.get("failover"))
