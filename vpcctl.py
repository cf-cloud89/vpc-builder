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
import json

# Set up logging to print clearly
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)


# Core Utility Function

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


# Naming and Resource Function
def get_bridge_name(vpc_name):
    return f"br-{vpc_name}"

def get_namespace_name(vpc_name, subnet_name):
    return f"ns-{vpc_name}-{subnet_name}"

def get_veth_pair_names(vpc_name, subnet_name):
    ns_side = f"veth-{subnet_name}-ns"
    br_side = f"veth-{subnet_name}-br"
    return (ns_side, br_side)

def get_gateway_ip(cidr):
    try:
        network = ipaddress.ip_network(cidr)
        gateway_ip = str(network[1])
        interface_ip = str(network[2])
        gateway_ip_with_prefix = f"{gateway_ip}/{network.prefixlen}"
        interface_ip_with_prefix = f"{interface_ip}/{network.prefixlen}"
        return (gateway_ip, interface_ip, gateway_ip_with_prefix, interface_ip_with_prefix)
    except Exception as e:
        log.error(f"Invalid CIDR '{cidr}': {e}")
        sys.exit(1)

def find_subnets_for_vpc(vpc_name):
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


# Core VPC Function
def create_vpc(vpc_name, cidr_block):
    log.info(f"--- Creating VPC '{vpc_name}' ({cidr_block}) ---")
    bridge_name = get_bridge_name(vpc_name)
    stdout, _ = run_cmd(["ip", "-br", "link", "show", "dev", bridge_name], check=False, capture=True)
    if bridge_name in stdout:
        log.warning(f"VPC '{vpc_name}' (bridge '{bridge_name}') already exists. Skipping creation.")
        return
    try:
        run_cmd(["ip", "link", "add", "name", bridge_name, "type", "bridge"])
        run_cmd(["ip", "link", "set", "dev", bridge_name, "up"])
        run_cmd(["sysctl", "-w", f"net.ipv4.conf.{bridge_name}.forwarding=1"])
        run_cmd(["sysctl", "-w", "net.ipv4.ip_forward=1"])
        run_cmd(["iptables", "-I", "FORWARD", "-i", bridge_name, "-o", bridge_name, "-j", "ACCEPT"])
        run_cmd(["iptables", "-I", "FORWARD", "-i", bridge_name, "!", "-o", bridge_name, "-j", "DROP"])
        run_cmd(["iptables", "-I", "FORWARD", "-o", bridge_name, "!", "-i", bridge_name, "-j", "DROP"])
        log.info(f" Successfully created VPC '{vpc_name}'.")
    except Exception as e:
        log.error(f"An error occurred during VPC creation: {e}")
        log.error("Attempting to clean up...")
        run_cmd(["ip", "link", "set", "dev", bridge_name, "down"], check=False)
        run_cmd(["ip", "link","delete","dev", bridge_name, "type", "bridge"], check=False)
        log.error("Cleanup attempted. Please check system state.")
        sys.exit(1)

# Subnet & Routing Function
def create_subnet(vpc_name, subnet_name, cidr, subnet_type, internet_iface=None):
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
        stdout, _ = run_cmd(["ip", "addr", "show", "dev", bridge_name])
        if gateway_ip not in stdout:
            log.info(f"   Adding Gateway IP {gateway_ip_with_prefix} to bridge {bridge_name}")
            run_cmd(["ip", "addr", "add", gateway_ip_with_prefix, "dev", bridge_name])
        else:
            log.info(f"   Gateway IP {gateway_ip} already present on {bridge_name}.")
        run_cmd(["ip", "netns", "exec", namespace_name, "ip", "link", "set", "dev", "lo", "up"])
        run_cmd(["ip", "netns", "exec", namespace_name, "ip", "addr", "add", interface_ip_with_prefix, "dev", veth_ns])
        run_cmd(["ip", "netns", "exec", namespace_name, "ip", "link", "set", "dev", veth_ns, "up"])
        run_cmd(["ip", "netns", "exec", namespace_name, "ip", "route", "add", "default", "via", gateway_ip])
        
        # Add default stateful rules to namespace
        log.info("   Applying default stateful firewall rules to namespace...")
        # 1. Set default policy to DROP all incoming traffic
        run_cmd(["ip", "netns", "exec", namespace_name, "iptables", "-P", "INPUT", "DROP"])
        # 2. Allow loopback traffic (important for many services)
        run_cmd(["ip", "netns", "exec", namespace_name, "iptables", "-A", "INPUT", "-i", "lo", "-j", "ACCEPT"])
        # 3. Allow established connections (makes firewall stateful)
        run_cmd(["ip", "netns", "exec", namespace_name, "iptables", "-A", "INPUT", "-m", "state", "--state", "RELATED,ESTABLISHED", "-j", "ACCEPT"])

        if subnet_type == "public":
            log.info(f"   Configuring as 'public' subnet using interface '{internet_iface}'")
            run_cmd(["iptables", "-t", "nat", "-I", "POSTROUTING", "-s", cidr, "-o", internet_iface, "-j", "MASQUERADE"])
            run_cmd(["iptables", "-I", "FORWARD", "1", "-i", bridge_name, "-o", internet_iface, "-s", cidr, "-j", "ACCEPT"])
            run_cmd(["iptables", "-I", "FORWARD", "1", "-i", internet_iface, "-o", bridge_name, "-d", cidr, "-m", "state", "--state", "RELATED,ESTABLISHED", "-j", "ACCEPT"])
        log.info(f" Successfully created Subnet '{subnet_name}'.")
    except Exception as e:
        log.error(f"An error occurred during subnet creation: {e}")
        log.error("Attempting to clean up...")
        run_cmd(["ip", "netns", "delete", namespace_name], check=False)
        run_cmd(["ip", "link", "delete", "dev", veth_br], check=False)
        log.error("Cleanup attempted. Please check system state.")
        sys.exit(1)


# Firewall / Security Group Function

def apply_rules(policy_file):
    """
    Applies firewall rules to a subnet from a JSON file.
    """
    log.info(f"--- Applying Security Group rules from '{policy_file}' ---")
    
    try:
        with open(policy_file, 'r') as f:
            policy = json.load(f)
    except FileNotFoundError:
        log.error(f"Policy file not found: {policy_file}")
        sys.exit(1)
    except json.JSONDecodeError:
        log.error(f"Could not parse policy file. Invalid JSON: {policy_file}")
        sys.exit(1)
    except Exception as e:
        log.error(f"Failed to read policy file: {e}")
        sys.exit(1)

    try:
        vpc_name = policy['vpc']
        subnet_name = policy['subnet']
        ingress_rules = policy.get('ingress', []) # Default to empty list if not present
    except KeyError as e:
        log.error(f"Invalid policy file: Missing required key: {e}")
        log.error("Policy must contain 'vpc', 'subnet', and 'ingress' keys.")
        sys.exit(1)

    namespace_name = get_namespace_name(vpc_name, subnet_name)
    log.info(f"   Targeting namespace: {namespace_name}")

    # First, flush old rules (optional, but good for idempotency)
    # It won't flush the default rules (lo, ESTABLISHED)
    # A better way is to create a custom chain, but this is simpler.
    
    # To make this idempotent, it needs to check if the rule already exists, which is very complex.
    # For this project, let's assume rules are applied once.

    for rule in ingress_rules:
        try:
            port = rule['port']
            protocol = rule['protocol'].lower()
            action = rule['action'].upper() # e.g., "ACCEPT" or "DENY"
            
            if action not in ["ACCEPT", "DENY"]:
                log.warning(f"   Invalid action '{action}'. Skipping rule.")
                continue

            log.info(f"   Applying rule: {action} {protocol} port {port}")
            
            # We insert rules at the top of the INPUT chain
            # This makes sure they are evaluated before the final DROP
            run_cmd([
                "ip", "netns", "exec", namespace_name,
                "iptables", "-I", "INPUT", "3", # Insert after lo and ESTABLISHED
                "-p", protocol,
                "--dport", str(port),
                "-j", action
            ])
        
        except KeyError as e:
            log.warning(f"   Skipping invalid rule, missing key: {e}. Rule: {rule}")
        except Exception as e:
            log.error(f"   Failed to apply rule: {rule}. Error: {e}")

    log.info(f" Successfully applied rules to '{subnet_name}'.")


# Cleanup Function
def delete_subnet(vpc_name, subnet_name, subnet_cidr, internet_iface=None):
    log.info(f"--- Deleting Subnet '{subnet_name}' from VPC '{vpc_name}' ---")
    bridge_name = get_bridge_name(vpc_name)
    namespace_name = get_namespace_name(vpc_name, subnet_name)
    if not subnet_cidr:
        log.error(f"   Cannot delete subnet {subnet_name}: Unknown CIDR. Deleting namespace only.")
        run_cmd(["ip", "netns", "delete", namespace_name], check=False)
        return
    (gateway_ip, _, gateway_ip_with_prefix, _) = get_gateway_ip(subnet_cidr)
    if internet_iface:
        log.info(f"   Deleting public subnet rules...")
        run_cmd(["iptables", "-t", "nat", "-D", "POSTROUTING", "-s", subnet_cidr, "-o", internet_iface, "-j", "MASQUERADE"], check=False)
        run_cmd(["iptables", "-D", "FORWARD", "-i", bridge_name, "-o", internet_iface, "-s", subnet_cidr, "-j", "ACCEPT"], check=False)
        run_cmd(["iptables", "-D", "FORWARD", "-i", internet_iface, "-o", bridge_name, "-d", subnet_cidr, "-m", "state", "--state", "RELATED,ESTABLISHED", "-j", "ACCEPT"], check=False)
    run_cmd(["ip", "addr", "del", gateway_ip_with_prefix, "dev", bridge_name], check=False)
    log.info(f"   Removed Gateway IP {gateway_ip_with_prefix} from {bridge_name}")
    run_cmd(["ip", "netns", "delete", namespace_name], check=False)
    log.info(f"   Deleted namespace {namespace_name}.")
    log.info(f" Successfully deleted Subnet '{subnet_name}'.")

def delete_vpc(vpc_name, internet_iface=None):
    log.info(f"--- Deleting VPC '{vpc_name}' ---")
    bridge_name = get_bridge_name(vpc_name)
    log.info("   Finding associated subnets (namespaces)...")
    subnets = find_subnets_for_vpc(vpc_name)
    if not subnets:
        log.info("   No subnets found to delete.")
    for subnet in subnets:
        subnet_name = subnet['name']
        ns_name = subnet['ns_name']
        log.warning(f"   Deleting namespace {ns_name}. F firewall/IP rules may remain.")
        log.warning("   (To fix this, `delete-subnet` should be called first with full details)")
        run_cmd(["ip", "netns", "delete", ns_name], check=False)
    log.info(f"   Deleting VPC isolation rules for {bridge_name}...")
    run_cmd(["iptables", "-D", "FORWARD", "-i", bridge_name, "-o", bridge_name, "-j", "ACCEPT"], check=False)
    run_cmd(["iptables", "-D", "FORWARD", "-i", bridge_name, "!", "-o", bridge_name, "-j", "DROP"], check=False)
    run_cmd(["iptables", "-D", "FORWARD", "-o", bridge_name, "!", "-i", bridge_name, "-j", "DROP"], check=False)
    log.info(f"   Cleaning up any orphaned peering rules for {bridge_name}...")
    delete_all_peering_for_vpc(bridge_name)
    run_cmd(["ip", "link", "set", "dev", bridge_name, "down"], check=False)
    run_cmd(["ip", "link", "delete", "dev", bridge_name, "type", "bridge"], check=False)
    log.info(f" Successfully deleted VPC '{vpc_name}'.")

# Peering Function
def peer_vpc(vpc_a_name, vpc_b_name):
    log.info(f"--- Establishing Peering between '{vpc_a_name}' and '{vpc_b_name}' ---")
    bridge_a = get_bridge_name(vpc_a_name)
    bridge_b = get_bridge_name(vpc_b_name)
    try:
        run_cmd(["iptables", "-I", "FORWARD", "1", "-i", bridge_a, "-o", bridge_b, "-j", "ACCEPT"])
        run_cmd(["iptables", "-I", "FORWARD", "1", "-i", bridge_b, "-o", bridge_a, "-j", "ACCEPT"])
        log.info(f" Successfully peered '{vpc_a_name}' and '{vpc_b_name}'.")
    except Exception as e:
        log.error(f"An error occurred during peering: {e}")
        log.error("Attempting to clean up...")
        delete_peering(vpc_a_name, vpc_b_name)
        sys.exit(1)

def delete_peering(vpc_a_name, vpc_b_name):
    log.info(f"--- Deleting Peering between '{vpc_a_name}' and '{vpc_b_name}' ---")
    bridge_a = get_bridge_name(vpc_a_name)
    bridge_b = get_bridge_name(vpc_b_name)
    try:
        run_cmd(["iptables", "-D", "FORWARD", "-i", bridge_a, "-o", bridge_b, "-j", "ACCEPT"], check=False)
        run_cmd(["iptables", "-D", "FORWARD", "-i", bridge_b, "-o", bridge_a, "-j", "ACCEPT"], check=False)
        log.info(f" Successfully removed peering for '{vpc_a_name}' and '{vpc_b_name}'.")
    except Exception as e:
        log.error(f"An error occurred during peering deletion: {e}")
        sys.exit(1)

def delete_all_peering_for_vpc(bridge_name):
    log.warning(f"   Peering rules for {bridge_name} may need to be manually cleaned.")
    pass


# Main CLI Parser

def main():
    parser = argparse.ArgumentParser(
        description="vpcctl - The Linux VPC management tool",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    # (create-vpc, create-subnet, delete-vpc, delete-subnet)
    parser_create_vpc = subparsers.add_parser("create-vpc", help="Create a new Virtual Private Cloud (VPC)")
    parser_create_vpc.add_argument("--name", type=str, required=True, help="Unique name for the VPC")
    parser_create_vpc.add_argument("--cidr", type=str, required=True, help="Base CIDR block (e.g., '10.0.0.0/16')")
    
    parser_create_subnet = subparsers.add_parser("create-subnet", help="Create a new Subnet within a VPC")
    parser_create_subnet.add_argument("--vpc", type=str, required=True, help="Name of the parent VPC")
    parser_create_subnet.add_argument("--name", type=str, required=True, help="Unique name for the subnet")
    parser_create_subnet.add_argument("--cidr", type=str, required=True, help="CIDR block for the subnet (e.g., '10.0.1.0/24')")
    parser_create_subnet.add_argument("--type", type=str, required=True, choices=["public", "private"], help="Type of subnet")
    parser_create_subnet.add_argument("--internet-iface", type=str, help="Host's internet-facing interface. Required for 'public' subnets.")
    
    parser_delete_vpc = subparsers.add_parser("delete-vpc", help="Delete a VPC and all its resources")
    parser_delete_vpc.add_argument("--name", type=str, required=True, help="Name of the VPC to delete")
    
    parser_delete_subnet = subparsers.add_parser("delete-subnet", help="Delete a specific subnet")
    parser_delete_subnet.add_argument("--vpc", type=str, required=True, help="Name of the parent VPC")
    parser_delete_subnet.add_argument("--name", type=str, required=True, help="Name of the subnet to delete")
    parser_delete_subnet.add_argument("--cidr", type=str, required=True, help="CIDR block of the subnet (for rule cleanup)")
    parser_delete_subnet.add_argument("--internet-iface", type=str, help="Host's internet-facing interface (if it was 'public')")

    # peer-vpc
    parser_peer_vpc = subparsers.add_parser("peer-vpc", help="Establish peering between two VPCs")
    parser_peer_vpc.add_argument("--vpc-a", type=str, required=True, help="Name of the first VPC")
    parser_peer_vpc.add_argument("--vpc-b", type=str, required=True, help="Name of the second VPC")
    
    # delete-peering
    parser_delete_peering = subparsers.add_parser("delete-peering", help="Remove peering between two VPCs")
    parser_delete_peering.add_argument("--vpc-a", type=str, required=True, help="Name of the first VPC")
    parser_delete_peering.add_argument("--vpc-b", type=str, required=True, help="Name of the second VPC")

    # apply-rules
    parser_apply_rules = subparsers.add_parser(
        "apply-rules", help="Apply a JSON security policy to a subnet"
    )
    parser_apply_rules.add_argument(
        "--policy",
        type=str,
        required=True,
        help="Path to the JSON policy file"
    )

    # Parse args
    args = parser.parse_args()

    # Command Dispatcher
    if args.command == "create-vpc":
        create_vpc(args.name, args.cidr)
    elif args.command == "create-subnet":
        create_subnet(args.vpc, args.name, args.cidr, args.type, args.internet_iface)
    elif args.command == "delete-vpc":
        delete_vpc(args.name)
    elif args.command == "delete-subnet":
        delete_subnet(args.vpc, args.name, args.cidr, args.internet_iface)
    elif args.command == "peer-vpc":
        peer_vpc(args.vpc_a, args.vpc_b)
    elif args.command == "delete-peering":
        delete_peering(args.vpc_a, args.vpc_b)
        
    # Command handler for applying rules
    elif args.command == "apply-rules":
        apply_rules(args.policy)

    else:
        log.error(f"Unknown command: {args.command}")
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    if os.geteuid() != 0:
        log.error("This script must be run as root (or with sudo).")
        sys.exit(1)
    main()