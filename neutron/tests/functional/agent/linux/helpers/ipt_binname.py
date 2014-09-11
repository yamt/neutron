#! /usr/bin/env python

from __future__ import print_function

from neutron.agent.linux import iptables_manager


def print_binary_name():
    print(iptables_manager.binary_name)

if __name__ == "__main__":
    print_binary_name()
