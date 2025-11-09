#!/usr/bin/env python3

"""
vpcctl - A tool to build and manage Linux Virtual Private Clouds (VPCs)
"""

import argparse
import subprocess
import sys
import logging
import os

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
        subprocess.run(cmd_list, check=check, capture_output=True, text=True)
    except subprocess.CalledProcessError as e:
        log.error(f"Failed to run: {cmd_str}")
        log.error(f"STDOUT: {e.stdout}")
        log.error(f"STDERR: {e.stderr}")
        sys.exit(1)

def get_bridge_name(vpc_name):
    """Uses a strict naming convention to get the bridge name."""
    return f"br-{vpc_name}"

## Core VPC Functions

def create_vpc(vpc_name, cidr_block):
    """
    Creates a new VPC.
    - Creates a Linux bridge.
    - Sets up host-level iptables rules for isolation.
    
    Assumption: The host's net.ipv4.ip_forward is already 1.
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

        # 4. Set up base iptables rules for VPC isolation
        
        # 4a. Allow traffic between subnets within the VPC
        run_cmd([
            "iptables", "-A", "FORWARD",
            "-i", bridge_name,
            "-o", bridge_name,
            "-j", "ACCEPT"
        ], check=False) # May fail if rule exists

        # 4b. Block traffic from the VPC to other interfaces (by default)
        run_cmd([
            "iptables", "-A", "FORWARD",
            "-i", bridge_name,
            "-j", "DROP"
        ], check=False)

        # 4c. Block traffic to the VPC from other interfaces (by default)
        run_cmd([
            "iptables", "-A", "FORWARD",
            "-o", bridge_name,
            "-j", "DROP"
        ], check=False)

        log.info(f" Successfully created VPC '{vpc_name}'.")
        log.info(f"   Bridge: {bridge_name}")
        log.info(f"   Isolation rules applied.")

    except Exception as e:
        log.error(f"An error occurred during VPC creation: {e}")
        log.error("Attempting to clean up...")
        # Simple cleanup on failure
        run_cmd(["ip", "link", "set", "dev", bridge_name, "down"], check=False)
        # 2. Delete the bridge using 'ip'
        run_cmd(["ip", "link", "delete", "dev", bridge_name, "type", "bridge"], check=False)
        log.error("Cleanup attempted. Please check system state.")
        sys.exit(1)

# Main CLI Parser

def main():
    """
    Main function to parse arguments and call the appropriate function.
    """
    parser = argparse.ArgumentParser(
        description="vpcctl - The VPC management tool on Linux",
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
        help="Unique name for the VPC (e.g., 'vpc-dev')",
    )
    parser_create_vpc.add_argument(
        "--cidr",
        type=str,
        required=True,
        help="Base CIDR block for the VPC (e.g., '10.0.0.0/16')",
    )

    # Parse args
    args = parser.parse_args()

    # Command Dispatcher
    if args.command == "create-vpc":
        # It checks and enables host IP forwarding here if not already enabled.
        log.info("Checking host IP forwarding...")
        try:
            # Check if global IP forwarding is enabled
            with open("/proc/sys/net/ipv4/ip_forward") as f:
                if f.read().strip() != "1":
                    log.warning("Host IP forwarding is OFF. Attempting to enable...")
                    run_cmd(["sysctl", "-w", "net.ipv4.ip_forward=1"])
                else:
                    log.info("Host IP forwarding is ON.")
        except Exception as e:
            log.error(f"Could not check/enable host IP forwarding: {e}")
            log.error("Please run 'sudo sysctl -w net.ipv4.ip_forward=1' manually.")
            sys.exit(1)
            
        create_vpc(args.name, args.cidr)

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