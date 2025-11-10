# Makefile for vpcctl: Build Linux VPCs with Network Namespaces

# Primary Targets:
#   sudo make setup           (Creates vpc-demo)
#   sudo make test-firewall   (Creates vpc-demo, starts web server, applies rules)
#   sudo make setup-peering   (Creates vpc-demo, vpc-dev, and peers them)

# Cleanup Targets:
#   sudo make cleanup         (Deletes vpc-demo only)
#   sudo make cleanup-peering (Deletes BOTH vpc-demo and vpc-dev)

# Note: You must run this with sudo.

# Configuration (VPC-A / Demo)
# !!! IMPORTANT: Change IFACE to your host's internet interface !!!
IFACE          = enX0

VPC_NAME       = vpc-demo
VPC_CIDR       = 10.100.0.0/16
PUBLIC_SUBNET  = 10.100.1.0/24
PRIVATE_SUBNET = 10.100.2.0/24

# IPs for testing (must match the subnets above)
PUBLIC_SUBNET_GATEWAY = 10.100.1.1 # The first usable IP in public subnet (gateway)
PRIVATE_SUBNET_IP = 10.100.2.2 # The internal IP for the server

# Configuration (VPC-B / Peering)
VPC_B_NAME       = vpc-dev
VPC_B_CIDR       = 10.200.0.0/16
VPC_B_SUBNET     = 10.200.1.0/24
VPC_B_GATEWAY    = 10.200.1.1 # This is the test ping target

# CLI Command
PYTHON_CMD     = ./vpcctl.py

# Target Definitions
.PHONY: setup cleanup test-firewall setup-peering cleanup-peering

setup:
	@echo "Provisioning VPC: $(VPC_NAME)....."
	sudo $(PYTHON_CMD) create-vpc --name $(VPC_NAME) --cidr $(VPC_CIDR)
	sudo $(PYTHON_CMD) create-subnet --vpc $(VPC_NAME) --name public \
		--cidr $(PUBLIC_SUBNET) --type public --internet-iface $(IFACE)
	sudo $(PYTHON_CMD) create-subnet --vpc $(VPC_NAME) --name private \
		--cidr $(PRIVATE_SUBNET) --type private
	@echo "---"
	@echo "Setup Complete"
	@echo "---"
	@echo "Run Validation Test 1: No 2 (see README)"

cleanup:
	@echo "Cleaning up VPC: $(VPC_NAME)....."
	# Stop test server, if running
	-sudo pkill -f http.server
	# Delete subnets
	sudo $(PYTHON_CMD) delete-subnet --vpc $(VPC_NAME) --name public \
		--cidr $(PUBLIC_SUBNET) --internet-iface $(IFACE)
	sudo $(PYTHON_CMD) delete-subnet --vpc $(VPC_NAME) --name private \
		--cidr $(PRIVATE_SUBNET)
	# Delete VPC
	sudo $(PYTHON_CMD) delete-vpc --name $(VPC_NAME)
	# Delete policy file
	-rm -f policy.json
	@echo "--Cleanup Complete--"

test-firewall: setup
	@echo "Setting up Firewall Test....."
	# 1. Create policy file
	@echo '{"vpc": "$(VPC_NAME)", "subnet": "private", "ingress": [{"port": 80, "protocol": "tcp", "action": "accept"}]}' > policy.json
	# 2. Start server in private subnet
	sudo ip netns exec ns-$(VPC_NAME)-private python3 -m http.server 80 &
	@echo "   (Web server started in background)"
	# 3. Apply rules
	sudo $(PYTHON_CMD) apply-rules --policy ./policy.json
	@echo "---"
	@echo "Firewall test setup complete."
	@echo "---"
	@echo "Run Validation Test 2: No 2 (see README)"

setup-peering: setup
	@echo "Setting up VPC-B for Peering....."
	sudo $(PYTHON_CMD) create-vpc --name $(VPC_B_NAME) --cidr $(VPC_B_CIDR)
	sudo $(PYTHON_CMD) create-subnet --vpc $(VPC_B_NAME) --name private \
		--cidr $(VPC_B_SUBNET) --type private
	
	@echo "--Establishing Peering--"
	sudo $(PYTHON_CMD) peer-vpc --vpc-a $(VPC_NAME) --vpc-b $(VPC_B_NAME)
	@echo "---"
	@echo "Peering test setup complete."
	@echo "---"
	@echo "Run Validation Test 3: No 3 (see README)"

cleanup-peering:
	@echo "Cleaning up Peering Test (VPC-A & VPC-B)....."
	# 1. Delete peering rules (best effort)
	sudo $(PYTHON_CMD) delete-peering --vpc-a $(VPC_NAME) --vpc-b $(VPC_B_NAME)
	# 2. Delete VPC-B
	sudo $(PYTHON_CMD) delete-subnet --vpc $(VPC_B_NAME) --name private --cidr $(VPC_B_SUBNET)
	sudo $(PYTHON_CMD) delete-vpc --name $(VPC_B_NAME)
	# 3. Run the main cleanup for VPC-A
	sudo make cleanup
	@echo "--Peering Test Cleanup Complete--"