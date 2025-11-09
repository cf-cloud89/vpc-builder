#!/usr/bin/env python3

"""
vpcctl - A tool to build and manage Virtual Private Clouds (VPCs) on Linux hosts.
"""

import argparse
import subprocess
import sys
import logging
import os
import ipaddress 
import re      

# Set up logging to print clearly
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)


# Core Utility Functions

def run_cmd(cmd_list, check=True, capture=True):
    """
    Runs a shell command, logs it, and handles errors.
    """
    cmd_str = " ".join(cmd_list)
    log.info(f"Running: {cmd_str}")
    try:
        process = subprocess.Popen(
            cmd_list,
            stdout=subprocess.PIPE if capture else None,
            stderr=subprocess.PIPE if capture else None,
            text=True
        )
        stdout, stderr = process.communicate()
        
        if check and process.returncode != 0:
            log.error(f"Failed to run: {cmd_str}")
            log.error(f"Return Code: {process.returncode}")
            if stdout: log.error(f"STDOUT: {stdout.strip()}")
            if stderr: log.error(f"STDERR: {stderr.strip()}")
            sys.exit(1)
        
        if stdout:
            log.debug(f"STDOUT: {stdout.strip()}")
        if stderr and process.returncode == 0:
            log.warning(f"STDERR: {stderr.strip()}")
        elif stderr and process.returncode != 0:
            log.error(f"STDERR: {stderr.strip()}")

        return stdout, stderr

    except FileNotFoundError as e:
        log.error(f"Command not found: {cmd_list[0]}. Please ensure it is installed.")
        log.error(f"Error: {e}")
        sys.exit(1)
    except Exception as e:
        log.error(f"An unexpected error occurred with: {cmd_str}")
        log.error(f"Error: {e}")
        sys.exit(1)


# Naming and Resource Functions

def get_bridge_name(vpc_name):
    """Uses a strict naming convention to get the bridge name."""
    return f"br-{vpc_name}"

def get_namespace_name(vpc_name, subnet_name):
    """Gets the network namespace name."""
    return f"ns-{vpc_name}-{subnet_name}"

def get_veth_pair_names(vpc_name, subnet_name):
    """Gets the names for the veth pair."""
    ns_side = f"veth-{subnet_name}-ns"
    br_side = f"veth-{subnet_name}-br"
    return (ns_side, br_side)

def get_gateway_ip(cidr):
    """Calculates gateway and interface IPs from a CIDR."""
    try:
        network = ipaddress.ip_network(cidr)
        gateway_ip = str(network[1])   # e.g., 10.0.1.1
        interface_ip = str(network[2]) # e.g., 10.0.1.2
        gateway_ip_with_prefix = f"{gateway_ip}/{network.prefixlen}" # 10.0.1.1/24
        interface_ip_with_prefix = f"{interface_ip}/{network.prefixlen}" # 10.0.1.2/24
        return (gateway_ip, interface_ip, gateway_ip_with_prefix, interface_ip_with_prefix)
    except Exception as e:
        log.error(f"Invalid CIDR '{cidr}': {e}")
        sys.exit(1)

def find_vpcs():
    """Finds all existing VPC bridges by name."""
    stdout, _ = run_cmd(["ip", "-br", "link", "show", "type", "bridge"])
    bridges = []
    if stdout:
        for line in stdout.splitlines():
            if line.startswith("br-"):
                bridge_name = line.split()[0]
                vpc_name = bridge_name[3:]
                bridges.append(vpc_name)
    return bridges

def find_subnets_for_vpc(vpc_name):
    """Finds all existing subnets (namespaces) for a given VPC."""
    stdout, _ = run_cmd(["ip", "netns", "list"])
    subnets = []
    if stdout:
        ns_prefix = f"ns-{vpc_name}-"
        for line in stdout.splitlines():
            ns_name = line.split()[0]
            if ns_name.startswith(ns_prefix):
                subnet_name = ns_name[len(ns_prefix):]
                # To find the subnet's CIDR to delete IPs/rules, check the IP of its veth peer
                bridge_name = get_bridge_name(vpc_name)
                stdout_ip, _ = run_cmd(["ip", "-br", "addr", "show", "dev", bridge_name])
                cidr = None
                if stdout_ip:
                    # Look for an IP on the bridge that's a gateway
                    for ip_line in stdout_ip.splitlines():
                        if bridge_name in ip_line:
                            parts = ip_line.split()
                            if len(parts) > 2:
                                # Find an IP, convert it to a network (e.g. 10.0.1.1/24 -> 10.0.1.0/24)
                                try:
                                    ip_if = ipaddress.ip_interface(parts[2])
                                    potential_cidr = str(ip_if.network)
                                    # Can't perfectly know which subnet this was,
                                    # so store the gateway IP to remove it.
                                    cidr = str(ip_if) # e.g., 10.0.1.1/24
                                    subnets.append({"name": subnet_name, "ns_name": ns_name, "cidr": cidr})
                                except:
                                    pass
                # Fallback if IP not found
                if not cidr:
                    subnets.append({"name": subnet_name, "ns_name": ns_name, "cidr": None})

    # Find by name and assume stateless cleanup
    subnets = []
    stdout, _ = run_cmd(["ip", "netns", "list"])
    if stdout:
        ns_prefix = f"ns-{vpc_name}-"
        for line in stdout.splitlines():
            ns_name = line.split()[0]
            if ns_name.startswith(ns_prefix):
                subnet_name = ns_name[len(ns_prefix):]
                subnets.append({"name": subnet_name, "ns_name": ns_name})
    return subnets


# ... Core VPC Function

def create_vpc(vpc_name, cidr_block):
    """Creates a new VPC bridge and isolation rules."""
    log.info(f"--- Creating VPC '{vpc_name}' ({cidr_block}) ---")
    bridge_name = get_bridge_name(vpc_name)

    # Check if bridge already exists
    stdout, _ = run_cmd(["ip", "-br", "link", "show", "dev", bridge_name], check=False, capture=True)
    if bridge_name in stdout:
        log.warning(f"VPC '{vpc_name}' (bridge '{bridge_name}') already exists. Skipping creation.")
        return

    try:
        run_cmd(["ip", "link", "add", "name", bridge_name, "type", "bridge"])
        run_cmd(["ip", "link", "set", "dev", bridge_name, "up"])
        run_cmd(["sysctl", "-w", f"net.ipv4.conf.{bridge_name}.forwarding=1"])
        run_cmd(["sysctl", "-w", "net.ipv4.ip_forward=1"])

        # Use -I to insert at the top, so they are matched before any general rules
        # This makes deletion cleaner later
        
        # 5a. Allow traffic between subnets within this VPC
        run_cmd([
            "iptables", "-I", "FORWARD",
            "-i", bridge_name,
            "-o", bridge_name,
            "-j", "ACCEPT"
        ])
        
        # 5b. Block traffic from this VPC to other interfaces
        run_cmd([
            "iptables", "-I", "FORWARD",
            "-i", bridge_name,
            "!", "-o", bridge_name,
            "-j", "DROP"
        ])

        # 5c. Block traffic to this VPC from other interfaces
        run_cmd([
            "iptables", "-I", "FORWARD",
            "-o", bridge_name,
            "!", "-i", bridge_name,
            "-j", "DROP"
        ])

        log.info(f" Successfully created VPC '{vpc_name}'.")
    except Exception as e:
        log.error(f"An error occurred during VPC creation: {e}")
        log.error("Attempting to clean up...")
        run_cmd(["ip", "link", "set", "dev", bridge_name, "down"], check=False)
        run_cmd(["ip", "link","delete","dev", bridge_name, "type", "bridge"], check=False)
        log.error("Cleanup attempted. Please check system state.")
        sys.exit(1)

# ... Subnet & Routing Function

def create_subnet(vpc_name, subnet_name, cidr, subnet_type, internet_iface=None):
    """Creates a new Subnet within a VPC."""
    log.info(f"--- Creating Subnet '{subnet_name}' in VPC '{vpc_name}' ({cidr}) ---")
    
    if subnet_type == "public" and not internet_iface:
        log.error("Missing argument: --internet-iface is required for 'public' subnets.")
        sys.exit(1)

    bridge_name = get_bridge_name(vpc_name)
    namespace_name = get_namespace_name(vpc_name, subnet_name)
    veth_ns, veth_br = get_veth_pair_names(vpc_name, subnet_name)
    
    (gateway_ip, interface_ip, gateway_ip_with_prefix, 
     interface_ip_with_prefix) = get_gateway_ip(cidr)

    log.info(f"   Gateway IP: {gateway_ip}")
    log.info(f"   Interface IP: {interface_ip}")

    # Check if namespace already exists
    stdout, _ = run_cmd(["ip", "netns", "list"], check=False, capture=True)
    if namespace_name in stdout:
        log.warning(f"Subnet '{subnet_name}' (namespace '{namespace_name}') already exists. Skipping creation.")
        return

    try:
        run_cmd(["ip", "netns", "add", namespace_name])
        run_cmd(["ip", "link", "add", veth_ns, "type", "veth", "peer", "name", veth_br])
        run_cmd(["ip", "link", "set", veth_ns, "netns", namespace_name])
        run_cmd(["ip", "link", "set", veth_br, "master", bridge_name])
        run_cmd(["ip", "link", "set", veth_br, "up"])

        # Check if gateway IP is already on bridge
        stdout, _ = run_cmd(["ip", "addr", "show", "dev", bridge_name])
        if gateway_ip not in stdout:
            log.info(f"   Adding Gateway IP {gateway_ip_with_prefix} to bridge {bridge_name}")
            run_cmd(["ip", "addr", "add", gateway_ip_with_prefix, "dev", bridge_name])
        else:
            log.info(f"   Gateway IP {gateway_ip} already present on {bridge_name}.")

        # == Configure inside the namespace ==
        run_cmd(["ip", "netns", "exec", namespace_name, "ip", "link", "set", "dev", "lo", "up"])
        run_cmd(["ip", "netns", "exec", namespace_name, "ip", "addr", "add", interface_ip_with_prefix, "dev", veth_ns])
        run_cmd(["ip", "netns", "exec", namespace_name, "ip", "link", "set", "dev", veth_ns, "up"])
        run_cmd(["ip", "netns", "exec", namespace_name, "ip", "route", "add", "default", "via", gateway_ip])

        if subnet_type == "public":
            log.info(f"   Configuring as 'public' subnet using interface '{internet_iface}'")
            
            # Add NAT rule
            run_cmd([
                "iptables", "-t", "nat",
                "-I", "POSTROUTING", # Insert at top
                "-s", cidr,
                "-o", internet_iface,
                "-j", "MASQUERADE"
            ])
            
            # Allow this subnet to talk to the internet
            run_cmd([
                "iptables", "-I", "FORWARD", "1", # Insert at position 1
                "-i", bridge_name,
                "-o", internet_iface,
                "-s", cidr,
                "-j", "ACCEPT"
            ])
            
            # Allow established connections back in
            run_cmd([
                "iptables", "-I", "FORWARD", "1", # Insert at position 1
                "-i", internet_iface,
                "-o", bridge_name,
                "-d", cidr,
                "-m", "state", "--state", "RELATED,ESTABLISHED",
                "-j", "ACCEPT"
            ])

        log.info(f" Successfully created Subnet '{subnet_name}'.")

    except Exception as e:
        log.error(f"An error occurred during subnet creation: {e}")
        log.error("Attempting to clean up...")
        run_cmd(["ip", "netns", "delete", namespace_name], check=False)
        run_cmd(["ip", "link", "delete", "dev", veth_br], check=False)
        log.error("Cleanup attempted. Please check system state.")
        sys.exit(1)


# ... Cleanup Function

def delete_subnet(vpc_name, subnet_name, subnet_cidr, internet_iface=None):
    """
    Deletes a single subnet.
    Finds and removes related IPs, NAT rules, and the namespace.
    """
    log.info(f"--- Deleting Subnet '{subnet_name}' from VPC '{vpc_name}' ---")
    bridge_name = get_bridge_name(vpc_name)
    namespace_name = get_namespace_name(vpc_name, subnet_name)
    
    # Check the CIDR to delete rules
    if not subnet_cidr:
        log.error(f"   Cannot delete subnet {subnet_name}: Unknown CIDR. Deleting namespace only.")
        run_cmd(["ip", "netns", "delete", namespace_name], check=False)
        return

    (gateway_ip, _, gateway_ip_with_prefix, _) = get_gateway_ip(subnet_cidr)

    # 1. Delete NAT and FORWARD rules (if public)
    # Guess if it was public based on internet_iface
    if internet_iface:
        log.info(f"   Deleting public subnet rules...")
        run_cmd([
            "iptables", "-t", "nat",
            "-D", "POSTROUTING",
            "-s", subnet_cidr,
            "-o", internet_iface,
            "-j", "MASQUERADE"
        ], check=False)
        
        run_cmd([
            "iptables", "-D", "FORWARD",
            "-i", bridge_name,
            "-o", internet_iface,
            "-s", subnet_cidr,
            "-j", "ACCEPT"
        ], check=False)
        
        run_cmd([
            "iptables", "-D", "FORWARD",
            "-i", internet_iface,
            "-o", bridge_name,
            "-d", subnet_cidr,
            "-m", "state", "--state", "RELATED,ESTABLISHED",
            "-j", "ACCEPT"
        ], check=False)

    # 2. Delete Gateway IP from bridge
    run_cmd(["ip", "addr", "del", gateway_ip_with_prefix, "dev", bridge_name], check=False)
    log.info(f"   Removed Gateway IP {gateway_ip_with_prefix} from {bridge_name}")

    # 3. Delete the network namespace
    # This automatically deletes the attached veth pair (veth_ns and veth_br)
    run_cmd(["ip", "netns", "delete", namespace_name], check=False)
    log.info(f"   Deleted namespace {namespace_name}.")
    log.info(f" Successfully deleted Subnet '{subnet_name}'.")


def delete_vpc(vpc_name, internet_iface=None):
    """
    Deletes an entire VPC and all its associated resources.
    """
    log.info(f"--- Deleting VPC '{vpc_name}' ---")
    bridge_name = get_bridge_name(vpc_name)
    
    # 1. Find and delete all associated subnets
    
    log.info("   Finding associated subnets (namespaces)...")
    subnets = find_subnets_for_vpc(vpc_name) # Finds namespaces like ns-vpc_name-*
    
    if not subnets:
        log.info("   No subnets found to delete.")
    
    for subnet in subnets:
        subnet_name = subnet['name']
        ns_name = subnet['ns_name']
        
        log.warning(f"   Deleting namespace {ns_name}. F firewall/IP rules may remain.")
        log.warning("   (To fix this, `delete-subnet` should be called first with full details)")
        run_cmd(["ip", "netns", "delete", ns_name], check=False)
        
    # 2. Delete VPC isolation rules
    log.info(f"   Deleting VPC isolation rules for {bridge_name}...")
    run_cmd([
        "iptables", "-D", "FORWARD",
        "-i", bridge_name,
        "-o", bridge_name,
        "-j", "ACCEPT"
    ], check=False)
    
    run_cmd([
        "iptables", "-D", "FORWARD",
        "-i", bridge_name,
        "!", "-o", bridge_name,
        "-j", "DROP"
    ], check=False)
    
    run_cmd([
        "iptables", "-D", "FORWARD",
        "-o", bridge_name,
        "!", "-i", bridge_name,
        "-j", "DROP"
    ], check=False)

    # 3. Take down and delete the bridge
    run_cmd(["ip", "link", "set", "dev", bridge_name, "down"], check=False)
    run_cmd(["ip", "link", "delete", "dev", bridge_name, "type", "bridge"], check=False)
    
    log.info(f" Successfully deleted VPC '{vpc_name}'.")


# Main CLI Parser

def main():
    """
    Main function to parse arguments and call the appropriate function.
    """
    parser = argparse.ArgumentParser(
        description="vpcctl - The Linux VPC management tool",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    # 'create-vpc' command
    parser_create_vpc = subparsers.add_parser(
        "create-vpc", help="Create a new Virtual Private Cloud (VPC)"
    )
    parser_create_vpc.add_argument("--name", type=str, required=True, help="Unique name for the VPC")
    parser_create_vpc.add_argument("--cidr", type=str, required=True, help="Base CIDR block (e.g., '10.0.0.0/16')")
    
    # 'create-subnet' command
    parser_create_subnet = subparsers.add_parser(
        "create-subnet", help="Create a new Subnet within a VPC"
    )
    parser_create_subnet.add_argument("--vpc", type=str, required=True, help="Name of the parent VPC")
    parser_create_subnet.add_argument("--name", type=str, required=True, help="Unique name for the subnet")
    parser_create_subnet.add_argument("--cidr", type=str, required=True, help="CIDR block for the subnet (e.g., '10.0.1.0/24')")
    parser_create_subnet.add_argument("--type", type=str, required=True, choices=["public", "private"], help="Type of subnet")
    parser_create_subnet.add_argument("--internet-iface", type=str, help="Host's internet-facing interface. Required for 'public' subnets.")
    
    # 'delete-vpc' command
    parser_delete_vpc = subparsers.add_parser(
        "delete-vpc", help="Delete a VPC and all its resources"
    )
    parser_delete_vpc.add_argument("--name", type=str, required=True, help="Name of the VPC to delete")
    
    # 'delete-subnet' command (manual)
    # This is a more robust way to clean up, as it has all the info
    parser_delete_subnet = subparsers.add_parser(
        "delete-subnet", help="Delete a specific subnet"
    )
    parser_delete_subnet.add_argument("--vpc", type=str, required=True, help="Name of the parent VPC")
    parser_delete_subnet.add_argument("--name", type=str, required=True, help="Name of the subnet to delete")
    parser_delete_subnet.add_argument("--cidr", type=str, required=True, help="CIDR block of the subnet (for rule cleanup)")
    parser_delete_subnet.add_argument("--internet-iface", type=str, help="Host's internet-facing interface (if it was 'public')")


    # Parse args
    args = parser.parse_args()

    # Command Dispatcher
    if args.command == "create-vpc":
        create_vpc(args.name, args.cidr)

    elif args.command == "create-subnet":
        create_subnet(
            args.vpc, args.name, args.cidr, args.type, args.internet_iface
        )
        
    elif args.command == "delete-vpc":
        delete_vpc(args.name)

    elif args.command == "delete-subnet":
        delete_subnet(
            args.vpc, args.name, args.cidr, args.internet_iface
        )

    else:
        log.error(f"Unknown command: {args.command}")
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    if os.geteuid() != 0:
        log.error("This script must be run as root (or with sudo).")
        sys.exit(1)
        
    main()