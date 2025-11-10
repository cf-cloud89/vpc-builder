# Makefile for vpcctl: Linux VPC from Scratch
#
# Usage:
#   sudo make setup     (Creates a default demo VPC)
#   sudo make cleanup   (Deletes the default demo VPC)
#
# Note: You must run this with sudo.

# Configuration
# !!! IMPORTANT: Change IFACE to your host's internet interface !!!
IFACE          = enX0

# Demo VPC settings
VPC_NAME       = vpc-demo
VPC_CIDR       = 10.100.0.0/16
PUBLIC_SUBNET  = 10.100.1.0/24
PRIVATE_SUBNET = 10.100.2.0/24

# CLI command
PYTHON_CMD     = ./vpcctl.py

# Automation Targets

.PHONY: setup cleanup test-public test-private test-isolation

setup:
	@echo "Provisioning VPC: $(VPC_NAME)"
	sudo $(PYTHON_CMD) create-vpc --name $(VPC_NAME) --cidr $(VPC_CIDR)
	sudo $(PYTHON_CMD) create-subnet --vpc $(VPC_NAME) --name public \
		--cidr $(PUBLIC_SUBNET) --type public --internet-iface $(IFACE)
	sudo $(PYTHON_CMD) create-subnet --vpc $(VPC_NAME) --name private \
		--cidr $(PRIVATE_SUBNET) --type private
	@echo "---"
	@echo "Setup Complete"
	@echo "---"
	@echo "Test with:"
	@echo "   sudo ip netns exec ns-$(VPC_NAME)-public ping -c 3 8.8.8.8"
	@echo "   sudo ip netns exec ns-$(VPC_NAME)-private ping -c 3 10.100.1.2"

cleanup:
	@echo "Cleaning up VPC: $(VPC_NAME)"
	sudo $(PYTHON_CMD) delete-subnet --vpc $(VPC_NAME) --name public \
		--cidr $(PUBLIC_SUBNET) --internet-iface $(IFACE)
	sudo $(PYTHON_CMD) delete-subnet --vpc $(VPC_NAME) --name private \
		--cidr $(PRIVATE_SUBNET)
	sudo $(PYTHON_CMD) delete-vpc --name $(VPC_NAME)
	@echo "---"
	@echo "Cleanup Complete"
	@echo "---"