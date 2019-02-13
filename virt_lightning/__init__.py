#!/usr/bin/env python3

import ipaddress
import os
import pathlib
import re
import string
import subprocess
import tempfile
import time
import uuid
import xml.etree.ElementTree as ET

import libvirt

import yaml


from .templates import BRIDGE_XML, CLOUD_INIT_ENI, DISK_XML, DOMAIN_XML


def libvirt_callback(userdata, err):
    pass


libvirt.registerErrorHandler(f=libvirt_callback, ctx=None)


class LibvirtHypervisor:
    def __init__(self, configuration, uri="qemu:///session"):
        conn = libvirt.open(configuration.get("libvirt_uri", uri))
        if conn is None:
            print("Failed to open connection to {uri}".format(uri=uri))
            exit(1)
        self.conn = conn
        self.configuration = configuration
        self.network = ipaddress.ip_network(
            self.configuration.get("network", "192.168.122.0/24")
        )
        self.gateway = ipaddress.IPv4Interface(
            self.configuration.get("gateway", "192.168.122.1/24")
        )
        self._last_free_ipv4 = None

    def create_domain(self):
        domain = LibvirtDomain.new(self.conn)
        domain.cloud_init = {
            "resize_rootfs": True,
            "disable_root": 0,
            "bootcmd": [],
            "runcmd": [],
        }
        return domain

    def list_domains(self):
        for i in self.conn.listAllDomains():
            yield LibvirtDomain(i)

    def get_domain_by_name(self, name):
        try:
            dom = self.conn.lookupByName(name)
            return LibvirtDomain(dom)
        except libvirt.libvirtError as e:
            if e.get_error_code() == libvirt.VIR_ERR_NO_DOMAIN:
                return None
            else:
                raise (e)

    def get_free_ipv4(self):
        # TODO: extend the list with ARP table
        used_ips = [self.gateway]
        for dom in self.list_domains():
            ipstr = dom.get_metadata("ipv4")
            if not ipstr:
                continue
            interface = ipaddress.ip_interface(ipstr)
            used_ips.append(interface)

        for ip in self.network:
            cidr_ip = "{ip}/24".format(ip=ip)
            interface = ipaddress.IPv4Interface(cidr_ip)
            if int(interface.ip.exploded.split(".")[3]) < 5:
                continue
            if self._last_free_ipv4 and self._last_free_ipv4 >= interface:
                continue
            if interface not in used_ips:
                self._last_free_ipv4 = interface
                return interface


class LibvirtDomain:
    def __init__(self, dom):
        self.dom = dom
        self.cloud_init = []
        self.meta_data = (
            "dsmode: local\n" "instance-id: iid-{name}\n" "local-hostname: {name}\n"
        )
        self._username = None
        self.ssh_key = None
        self.wait_for = []
        self.distro = None

    def new(conn):
        root = ET.fromstring(DOMAIN_XML)
        e = root.findall("./name")[0]
        e.text = str(uuid.uuid4())[0:10]
        dom = conn.defineXML(ET.tostring(root).decode())
        return LibvirtDomain(dom)

    def root_password(self, root_password=None):
        if root_password:
            self.cloud_init["chpassd"] = {
                "list": "root:{root_password}".format(root_password=root_password),
                "expire": False,
            }
        chpassd = self.cloud_init.get("chpassd")
        if chpassd:
            return chpassd["list"].split(":")[1]

    def ssh_key_file(self, ssh_key_file):
        try:
            with open(os.path.expanduser(ssh_key_file), "r") as fd:
                self.ssh_key = fd.read()
        except IOError:
            print("Can not read {filename}".format(filename=ssh_key_file))

        if self.ssh_key and len(self.ssh_key) > 0:
            self.cloud_init["ssh_authorized_keys"] = [self.ssh_key]
            if "users" in self.cloud_init:
                self.cloud_init["users"][0]["ssh_authorized_keys"] = [self.ssh_key]

    def username(self, username=None):
        if username:
            if not re.match("[a-z_][a-z0-9_-]{1,32}$", username):
                raise Exception("Invalid username: ", username)

            self._username = username
            self.cloud_init["users"] = [
                {
                    "name": username,
                    "gecos": "virt-bootstrap user",
                    "sudo": "ALL=(ALL) NOPASSWD:ALL",
                    "ssh_authorized_keys": self.cloud_init.get("ssh_authorized_keys"),
                }
            ]

            self.record_metadata("username", username)
        elif self._username:
            return self._username
        else:
            return self.get_metadata("username")

    def name(self, name=None):
        if name:
            self.dom.rename(name, 0)
        return self.dom.name()

    def vcpus(self, value=None):
        if value:
            self.dom.setVcpusFlags(value, libvirt.VIR_DOMAIN_AFFECT_CONFIG)

    def memory(self, value=None):
        if value:
            if value < 256:
                print(
                    "Warning: low memory {value} for VM {name}".format(
                        value=value, name=self.name
                    )
                )
            value *= 1024
            self.dom.setMemoryFlags(
                value, libvirt.VIR_DOMAIN_AFFECT_CONFIG | libvirt.VIR_DOMAIN_MEM_MAXIMUM
            )

    def getNextBlckDevice(self):
        if not hasattr(self, "blockdev"):
            self.blockdev = list(string.ascii_lowercase)
            self.blockdev.reverse()
        return "vd{block}".format(block=self.blockdev.pop())

    def record_metadata(self, k, v):
        meta = "<{k} name='{v}' />".format(k=k, v=v)
        self.dom.setMetadata(
            libvirt.VIR_DOMAIN_METADATA_ELEMENT,
            meta,
            "vl",
            k,
            libvirt.VIR_DOMAIN_AFFECT_CONFIG,
        )

    def get_metadata(self, k):
        try:
            xml = self.dom.metadata(libvirt.VIR_DOMAIN_METADATA_ELEMENT, k)
        except libvirt.libvirtError as e:
            if e.get_error_code() == libvirt.VIR_ERR_NO_DOMAIN_METADATA:
                return None
            raise (e)
        elt = ET.fromstring(xml)
        return elt.attrib["name"]

    def context(self, context=None):
        if context:
            self.record_metadata("context", context)
        return self.get_metadata("context")

    def attachDisk(self, path, device="disk", disk_type="qcow2"):
        device_name = self.getNextBlckDevice()
        disk_root = ET.fromstring(DISK_XML)
        disk_root.attrib["device"] = device
        disk_root.findall("./driver")[0].attrib = {"name": "qemu", "type": disk_type}
        disk_root.findall("./source")[0].attrib = {"file": path}
        disk_root.findall("./target")[0].attrib = {"dev": device_name}
        xml = ET.tostring(disk_root).decode()
        self.dom.attachDeviceFlags(xml, libvirt.VIR_DOMAIN_AFFECT_CONFIG)
        return device_name

    def attachBridge(self, bridge):
        disk_root = ET.fromstring(BRIDGE_XML)
        disk_root.findall("./source")[0].attrib = {"bridge": bridge}
        xml = ET.tostring(disk_root).decode()
        self.dom.attachDeviceFlags(xml, libvirt.VIR_DOMAIN_AFFECT_CONFIG)

    def add_root_disk(self, distro, size=20):
        self.distro = distro
        base_image_path_template = (
            "{path}/.local/share/libvirt/" "images/upstream/{distro}.qcow2"
        )
        base_image_path = base_image_path_template.format(
            path=pathlib.Path.home(), distro=distro
        )
        image_path = "{path}/.local/share/libvirt/images/{name}.qcow2".format(
            path=pathlib.Path.home(), name=self.name()
        )
        proc = subprocess.Popen(
            [
                "qemu-img",
                "create",
                "-f",
                "qcow2",
                "-b",
                base_image_path,
                image_path,
                "{size}G".format(size=size),
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        self.wait_for.append(proc)
        self.attachDisk(image_path)

    def add_swap_disk(self, size=1):
        swap_path = "{path}/.local/share/libvirt/images/{name}-swap.qcow2".format(
            path=pathlib.Path.home(), name=self.name()
        )
        proc = subprocess.Popen(
            [
                "qemu-img",
                "create",
                "-f",
                "qcow2",
                swap_path,
                "{size}G".format(size=size),
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        self.wait_for.append(proc)
        device_name = self.attachDisk(swap_path)
        self.cloud_init["mounts"] = [device_name, "none", "swap", "sw", 0, 0]
        self.cloud_init["bootcmd"].append("mkswap /dev/vdb")
        self.cloud_init["bootcmd"].append("swapon /dev/vdb")

    def dump(self):
        ET.dump(self.root)

    def set_ip(self, ipv4, gateway, dns):
        self.ipv4 = ipv4
        self.gateway = gateway
        self.dns = dns
        self.record_metadata("ipv4", ipv4)

        primary_mac_addr = self.get_mac_addresses()[0]
        self._network_meta = {"config": "disabled"}
        if "ubuntu-18.04" in self.distro:
            self._network_meta = {
                "version": 2,
                "ethernets": {
                    "interface0": {
                        "match": {"macaddress": primary_mac_addr},
                        "set-name": "interface0",
                        "addresses": [str(self.ipv4)],
                        "gateway4": self.gateway,
                        "nameservers": {"addresses": [self.dns]},
                    }
                },
            }
        else:
            self.meta_data += CLOUD_INIT_ENI
            self._network_meta = {
                "version": 1,
                "config": [
                    {
                        "type": "physical",
                        "name": "eth0",
                        "mac_address": primary_mac_addr,
                        "subnets": [
                            {
                                "type": "static",
                                "address": str(self.ipv4),
                                "gateway": self.gateway,
                                "dns_nameservers": [self.dns],
                            }
                        ],
                    }
                ],
            }

        nm_filter = "(centos|fedora|rhel)"
        if re.match(nm_filter, self.distro):
            nmcli_call = (
                "nmcli c add type ethernet con-name eth0 ifname eth0 ip4 {ipv4} "
                "ipv4.gateway {gateway} ipv4.dns {dns} ipv4.method manual"
            )
            self.cloud_init["runcmd"].append("nmcli -g UUID c|xargs -n 1 nmcli con del")
            self.cloud_init["runcmd"].append(
                nmcli_call.format(ipv4=self.ipv4, gateway=self.gateway, dns=self.dns)
            )
            # Without that NM, initialize eth0 with a DHCP IP
            self.cloud_init["bootcmd"].append(
                'echo "[main]" > /etc/NetworkManager/conf.d/no-auto-default.conf'
            )
            self.cloud_init["bootcmd"].append(
                (
                    'echo "no-auto-default=eth0" >> '
                    "/etc/NetworkManager/conf.d/no-auto-default.conf"
                )
            )

    def get_mac_addresses(self):
        xml = self.dom.XMLDesc(0)
        root = ET.fromstring(xml)
        ifaces = root.findall("./devices/interface/mac")
        return [iface.attrib["address"] for iface in ifaces]

    def prepare_meta_data(self):
        cidata_path = "{path}/.local/share/libvirt/images/{name}-cidata.iso".format(
            path=pathlib.Path.home(), name=self.name()
        )

        with tempfile.TemporaryDirectory() as temp_dir:
            with open(temp_dir + "/user-data", "w") as fd:
                fd.write("#cloud-config\n")
                fd.write(yaml.dump(self.cloud_init, Dumper=yaml.Dumper))
            with open(temp_dir + "/meta-data", "w") as fd:
                fd.write(
                    self.meta_data.format(
                        name=self.name(), ipv4=self.ipv4.ip, gateway=self.gateway
                    )
                )
            with open(temp_dir + "/network-config", "w") as fd:
                fd.write(yaml.dump(self._network_meta, Dumper=yaml.Dumper))

            proc = subprocess.Popen(
                [
                    "genisoimage",
                    "-output",
                    cidata_path,
                    "-volid",
                    "cidata",
                    "-joliet",
                    "-r",
                    "user-data",
                    "meta-data",
                    "network-config",
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                cwd=temp_dir,
            )
            proc.wait()
            self.wait_for.append(proc)
        self.attachDisk(cidata_path, device="cdrom", disk_type="raw")

    def start(self):
        self.prepare_meta_data()
        for proc in self.wait_for:
            outs, errs = proc.communicate()
            if proc.returncode != 0:
                raise Exception("A command has failed: ", outs, errs)

        self.dom.create()

    def get_ipv4(self):
        if not self.dom.isActive():
            return
        try:
            ifaces = self.dom.interfaceAddresses(
                libvirt.VIR_DOMAIN_INTERFACE_ADDRESSES_SRC_AGENT, 0
            )
            for (_, val) in ifaces.items():
                for addr in val["addrs"]:
                    if addr["type"] != 0:  # 1 == IPv6
                        continue
                    if addr["addr"].startswith("127."):
                        continue
                    return addr["addr"]
        except (KeyError, TypeError):
            pass
        except libvirt.libvirtError as e:
            if e.get_error_code() == libvirt.VIR_ERR_AGENT_UNRESPONSIVE:
                pass
            else:
                print(e.get_error_code())
                raise (e)

    def set_user_password(self, user, password):
        return self.dom.setUserPassword(user, password)

    def clean_up(self):
        state, _ = self.dom.state()
        if state != libvirt.VIR_DOMAIN_SHUTOFF:
            self.dom.destroy()
        self.dom.undefine()

    def ssh_ping(self):
        if hasattr(self, "_ssh_ping"):
            return self._ssh_ping
        ipv4 = self.get_ipv4()
        if not ipv4:
            return

        proc = subprocess.Popen(
            [
                "ssh",
                "-o",
                "StrictHostKeyChecking=no",
                "-o",
                "UserKnownHostsFile=/dev/null",
                "-o",
                "ConnectTimeout=1",
                "{username}@{ipv4}".format(username=self.username(), ipv4=ipv4),
                "hostname",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        time.sleep(1)
        status = proc.poll()
        proc.kill()
        if status == 0:
            self._ssh_ping = True
            return True
        else:
            return False
