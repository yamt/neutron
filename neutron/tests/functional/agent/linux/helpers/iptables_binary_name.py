#! /usr/bin/env python

from neutron.agent.linux import iptables_manager


def check_binary_name(expected_name):
    assert expected_name == iptables_manager.binary_name

if __name__ == "__main__":
    check_binary_name(argv[1])
