#!/usr/bin/env python3

"""
vpcctl - A tool to build and manage Virtual Private Clouds (VPCs) on Linux
"""

import argparse
import subprocess
import sys
import logging
import os
import ipaddress

# Set up logging to print clearly
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)


# Core Utility Functions

def run_cmd(cmd_list, check=True):
    """
    Runs a shell command, logs it, and exits on failure if check=True.
    """
    cmd_str = " ".join(cmd_list)
    log.info(f"Running: {cmd_str}")
    try:
        # Use Popen and communicate to handle commands that might produce a lot of output
        process = subprocess.Popen(cmd_list, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        stdout, stderr = process.communicate()
        
        if check and process.returncode != 0:
            log.error(f"Failed to run: {cmd_str}")
            log.error(f"Return Code: {process.returncode}")
            log.error(f"STDOUT: {stdout.strip()}")
            log.error(f"STDERR: {stderr.strip()}")
            sys.exit(1)
        
        if stdout:
            log.debug(f"STDOUT: {stdout.strip()}")
        if stderr:
            log.warning(f"STDERR: {stderr.strip()}")
            
    except FileNotFoundError as e:
        log.error(f"Command not found: {cmd_list[0]}. Please ensure it is installed.")
        log.error(f"Error: {e}")
        sys.exit(1)
    except Exception as e:
        log.error(f"An unexpected error occurred with: {cmd_str}")
        log.error(f"Error: {e}")
        sys.exit(1)


def get_bridge_name(vpc_name):
    """Uses the strict naming convention to get the bridge name."""
    return f"br-{vpc_name}"


def get_namespace_name(vpc_name, subnet_name):
    """Gets the network namespace name."""
    return f"ns-{vpc_name}-{subnet_name}"


def get_veth_pair_names(vpc_name, subnet_name):
    """Gets the names for the veth pair."""
    ns_side = f"veth-{subnet_name}-ns"
    br_side = f"veth-{subnet_name}-br"
    return (ns_side, br_side)


# ... Core VPC Functions

def create_vpc(vpc_name, cidr_block):
    """
    Creates a new VPC.
    - Creates a Linux bridge.
    - Sets up host-level iptables rules for isolation.
    """
    log.info(f"--- Creating VPC '{vpc_name}' ({cidr_block}) ---")
    bridge_name = get_bridge_name(vpc_name)

    try:
        # 1. Create the bridge using 'ip'
        run_cmd(["ip", "link", "add", "name", bridge_name, "type", "bridge"])

        # 2. Bring the bridge up
        run_cmd(["ip", "link", "set", "dev", bridge_name, "up"])

        # 3. Enable IP forwarding on the bridge (for routing between subnets)
        run_cmd(["sysctl", "-w", f"net.ipv4.conf.{bridge_name}.forwarding=1"])
        
        # 4. Enable IP forwarding globally (needed for routing and NAT)
        run_cmd(["sysctl", "-w", "net.ipv4.ip_forward=1"])

        # 5. Set up base iptables rules for VPC isolation
        
        # 5a. Allow traffic between subnets within this VPC
        run_cmd([
            "iptables", "-A", "FORWARD",
            "-i", bridge_name,
            "-o", bridge_name,
            "-j", "ACCEPT"
        ])

        # 5b. Block traffic from this VPC to other interfaces (by default)
        run_cmd([
            "iptables", "-I", "FORWARD", # Use -I to insert at the top
            "-i", bridge_name,
            "!", "-o", bridge_name, # Not going to another subnet in the same VPC
            "-j", "DROP"
        ])

        # 5c. Block traffic to this VPC from other interfaces (by default)
        run_cmd([
            "iptables", "-I", "FORWARD", # Use -I to insert at the top
            "-o", bridge_name,
            "!", "-i", bridge_name, # Not coming from another subnet in the same VPC
            "-j", "DROP"
        ])

        log.info(f" Successfully created VPC '{vpc_name}'.")
        log.info(f"   Bridge: {bridge_name}")
        log.info(f"   Isolation rules applied.")

    except Exception as e:
        log.error(f"An error occurred during VPC creation: {e}")
        log.error("Attempting to clean up...")
        # Simple cleanup on failure
        run_cmd(["ip", "link", "set", "dev", bridge_name, "down"], check=False)
        run_cmd(["ip", "link","delete","dev", bridge_name, "type", "bridge"], check=False)
        # Add iptables cleanup
        log.error("Cleanup attempted. Please check system state.")
        sys.exit(1)

# ... Subnet & Routing Functions

def create_subnet(vpc_name, subnet_name, cidr, subnet_type, internet_iface=None):
    """
    Creates a new Subnet within a VPC.
    - Creates a Network Namespace.
    - Creates a veth pair to connect the namespace to the VPC bridge.
    - Configures IP, gateway, and routes inside the namespace.
    - If 'public', configures NAT rules.
    """
    log.info(f"--- Creating Subnet '{subnet_name}' in VPC '{vpc_name}' ({cidr}) ---")
    
    if subnet_type == "public" and not internet_iface:
        log.error("Missing argument: --internet-iface is required for 'public' subnets.")
        sys.exit(1)

    # 1. Derive all names
    bridge_name = get_bridge_name(vpc_name)
    namespace_name = get_namespace_name(vpc_name, subnet_name)
    veth_ns, veth_br = get_veth_pair_names(vpc_name, subnet_name)
    
    # 2. Calculate IPs
    try:
        network = ipaddress.ip_network(cidr)
        gateway_ip = str(network[1])   # e.g., 10.0.1.1
        interface_ip = str(network[2]) # e.g., 10.0.1.2
        
        gateway_ip_with_prefix = f"{gateway_ip}/{network.prefixlen}" # 10.0.1.1/24
        interface_ip_with_prefix = f"{interface_ip}/{network.prefixlen}" # 10.0.1.2/24
    except Exception as e:
        log.error(f"Invalid CIDR '{cidr}': {e}")
        sys.exit(1)

    log.info(f"   Gateway IP: {gateway_ip}")
    log.info(f"   Interface IP: {interface_ip}")

    try:
        # 3. Create Namespace
        run_cmd(["ip", "netns", "add", namespace_name])

        # 4. Create veth pair
        run_cmd(["ip", "link", "add", veth_ns, "type", "veth", "peer", "name", veth_br])

        # 5. Move veth (namespace side) into the namespace
        run_cmd(["ip", "link", "set", veth_ns, "netns", namespace_name])

        # 6. Attach veth (bridge side) to the bridge
        run_cmd(["ip", "link", "set", veth_br, "master", bridge_name])
        
        # 7. Bring up the bridge side of the veth
        run_cmd(["ip", "link", "set", veth_br, "up"])

        # 8. Assign the Gateway IP to the bridge interface
        # This allows the host to be the router for this subnet
        run_cmd(["ip", "addr", "add", gateway_ip_with_prefix, "dev", bridge_name])

        # 9. == Configure inside the namespace ==
        # Use 'ip netns exec <name> ...' to run commands inside it
        
        # 9a. Bring up the loopback interface
        run_cmd(["ip", "netns", "exec", namespace_name, "ip", "link", "set", "dev", "lo", "up"])
        
        # 9b. Assign the Interface IP to the namespace veth
        run_cmd(["ip", "netns", "exec", namespace_name, "ip", "addr", "add", interface_ip_with_prefix, "dev", veth_ns])
        
        # 9c. Bring up the namespace veth
        run_cmd(["ip", "netns", "exec", namespace_name, "ip", "link", "set", "dev", veth_ns, "up"])
        
        # 9d. Set the default route (gateway) inside the namespace
        run_cmd(["ip", "netns", "exec", namespace_name, "ip", "route", "add", "default", "via", gateway_ip])

        # 10. Handle "Public" Subnet (NAT)
        if subnet_type == "public":
            log.info(f"   Configuring as 'public' subnet using interface '{internet_iface}'")
            
            # 10a. Add NAT rule
            run_cmd([
                "iptables", "-t", "nat",
                "-A", "POSTROUTING",
                "-s", cidr,
                "-o", internet_iface,
                "-j", "MASQUERADE"
            ])
            
            # 10b. Adjust FORWARD rule to allow this subnet to talk to the internet
            run_cmd([
                "iptables", "-I", "FORWARD", "1", # Insert at position 1
                "-i", bridge_name,
                "-o", internet_iface,
                "-s", cidr,
                "-j", "ACCEPT"
            ])
            
            # 10c. Allow established connections back in
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
        # Add full cleanup logic
        run_cmd(["ip", "netns", "delete", namespace_name], check=False)
        run_cmd(["ip", "link", "delete", "dev", veth_br], check=False)
        log.error("Cleanup attempted. Please check system state.")
        sys.exit(1)


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
    parser_create_vpc.add_argument(
        "--name",
        type=str,
        required=True,
        help="Unique name for the VPC (e.g., 'vpc-prod')",
    )
    parser_create_vpc.add_argument(
        "--cidr",
        type=str,
        required=True,
        help="Base CIDR block for the VPC (e.g., '10.0.0.0/16')",
    )
    
    # 'create-subnet' command
    parser_create_subnet = subparsers.add_parser(
        "create-subnet", help="Create a new Subnet within a VPC"
    )
    parser_create_subnet.add_argument(
        "--vpc",
        type=str,
        required=True,
        help="Name of the parent VPC",
    )
    parser_create_subnet.add_argument(
        "--name",
        type=str,
        required=True,
        help="Unique name for the subnet (e.g., 'public' or 'private')",
    )
    parser_create_subnet.add_argument(
        "--cidr",
        type=str,
        required=True,
        help="CIDR block for the subnet (e.g., '10.0.1.0/24')",
    )
    parser_create_subnet.add_argument(
        "--type",
        type=str,
        required=True,
        choices=["public", "private"],
        help="Type of the subnet (public or private)",
    )
    parser_create_subnet.add_argument(
        "--internet-iface",
        type=str,
        help="Host's internet-facing interface (e.g., 'eth0'). Required for 'public' subnets.",
    )

    # Parse args
    args = parser.parse_args()

    # Command Dispatcher
    if args.command == "create-vpc":
        create_vpc(args.name, args.cidr)

    elif args.command == "create-subnet":
        create_subnet(
            args.vpc,
            args.name,
            args.cidr,
            args.type,
            args.internet_iface
        )

    else:
        log.error(f"Unknown command: {args.command}")
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    # This script requires root privileges to manipulate network interfaces
    if os.geteuid() != 0:
        log.error("This script must be run as root (or with sudo).")
        sys.exit(1)
        
    main()