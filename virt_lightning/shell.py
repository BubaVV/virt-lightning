#!/usr/bin/env python3

import argparse
import getpass
import glob
import pathlib
import re
import sys
import time
import yaml

import virt_lightning as vl
CURSOR_UP_ONE = '\x1b[1A'
ERASE_LINE = '\x1b[2K'


configuration = {
    "libvirt_uri": "qemu:///session",
    "bridge": "virbr0",
    "username": getpass.getuser(),
}


def up(virt_lightning_yaml_path, context):
    fd = open(virt_lightning_yaml_path, "r")
    host_definitions = yaml.load(fd)
    hv = vl.LibvirtHypervisor(configuration)
    print("Starting:")
    status_line = ""
    for host in host_definitions:
        if "name" not in host:
            host["name"] = re.sub(r"\W+", "", host["distro"])
        status_line += "🗲%s " % host["name"]
        print(status_line)
        domain = hv.create_domain()
        domain.context(context)
        domain.name(host["name"])
        domain.ssh_key_file(
            configuration.get("ssh_key_file", "~/.ssh/id_rsa.pub")
        )
        domain.username(configuration.get("username", getpass.getuser()))
        domain.vcpus(host.get("vcpus"))
        domain.memory(host.get("memory", 768))
        domain.add_root_disk(host["distro"])
        domain.add_swap_disk(host.get("swap_size", 1))
        domain.attachBridge(configuration["bridge"])
        domain.start()
        sys.stdout.write(CURSOR_UP_ONE)
        sys.stdout.write(ERASE_LINE)
    print(status_line)
    print("Done! You can now follow the deployment. To get the live status:")
    print("  vl status")
    print("")
    print("You can also access the serial console of the VM:")
    print("  virsh console $vm_name")

def ansible_inventory(context):
    hv = vl.LibvirtHypervisor(configuration)
    for domain in hv.list_domains():
        if domain.context() == context:
            print(
                "%s ansible_host=%s ansible_username=%s"
                % (domain.name(), domain.get_ipv4(), domain.username())
            )


def status(context=None, live=False):
    hv = vl.LibvirtHypervisor(configuration)
    results = {}

    def iconify(v):
        if isinstance(v, str):
            return v
        elif(v):
            return "✔"
        else:
            return "✕"

    while True:
        for domain in hv.list_domains():
            if context and context != domain.context():
                continue
            name = domain.name()
            results[name] = {
                "name": name,
                "ipv4": domain.get_ipv4() or "waiting",
                "context": domain.context(),
                "username": domain.username(),
                "ssh_ping": iconify(domain.ssh_ping())}

        for _ in range(0, len(results) + 1):
            sys.stdout.write(CURSOR_UP_ONE)
            sys.stdout.write(ERASE_LINE)

        print("[host]        [username@IP]")
        for _, v in sorted(results.items()):
            print("{name:<13} {username}@{ipv4:>5} {ssh_ping}".format(**v))
        if not live:
            break
        time.sleep(.5)


def down(context):
    hv = vl.LibvirtHypervisor(configuration)
    for domain in hv.list_domains():
        if context and domain.context() != context:
            continue
        domain.clean_up()


def list_distro():
    path = "%s/.local/share/libvirt/images/upstream" % (pathlib.Path.home())
    for path in glob.glob(path + "/*.qcow2"):
        distro = pathlib.Path(path).stem
        if "no-cloud-init" not in distro:
            print("- distro: %s" % distro)


def main():

    example = """
    Example:

      # We export the list of the distro in the virt-lightning.yaml file.
      $ vl distro > virt-lightning.yaml

      # For each line, virt-lightning will start a VM with the associated distro.
      $ vl up

      # Once the VM are up, we can generate an Ansible inventory file:
      $ vl ansible_inventory

      # The file is ready to be used by Ansible:
      $ ansible all -m ping -i inventory


    """

    parser = argparse.ArgumentParser(
        description="virt-lightning",
        epilog=example,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "action",
        choices=[
            "up",
            "down",
            "status",
            "ansible_inventory",
            "distro",
            "clean_all",
            "status_all",
            "status_live",
        ],
        help="The action to call.",
    )
    parser.add_argument(
        "--virt-lightning-yaml",
        default="virt-lightning.yaml",
        help="point on an alternative virt-lightning.yaml file (default: %(default)s)",
    )
    parser.add_argument(
        "--context",
        default="default",
        help="change the name of the context (default: %(default)s)",
    )

    args = parser.parse_args()


    if args.action == "up":
        up(args.virt_lightning_yaml, args.context)
    elif args.action == "down":
        down(args.context)
    elif args.action == "ansible_inventory":
        ansible_inventory(args.context)
    elif args.action == "distro":
        list_distro()
    elif args.action == "clean_all":
        down()
    elif args.action == "status_all":
        status()
    elif args.action == "status":
        status(args.context)
    elif args.action == "status_live":
        status(args.context, True)
